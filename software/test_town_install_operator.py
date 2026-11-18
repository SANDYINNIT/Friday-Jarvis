"""Local desktop bridge, real catalog/world transactions and SQLite backup recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest

from source.server import lab_state
from source.server.identity_integration import IdentityIntegration
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world import Store, identity_backup, identity_install
from source.server.world.identity_catalog import AliasClaim, Route
from test_identity_install import logical_rows
from test_identity_reader import config, linked, setup_reader


@pytest.fixture
def operator(tmp_path, monkeypatch, setup_reader):
    catalog, shadow, world, reader, rows, ids = setup_reader
    world.paused = True
    store = Store(tmp_path / "world.db")
    assert store.save(world)["ok"]
    with closing(sqlite3.connect(catalog.path)) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    assert store._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    catalog.reconcile([rows[0]], complete=True)
    evidence = [linked(ids)]
    initial = reader.snapshot(links=evidence)["installation_proposal"]
    candidate, _ = identity_install.apply(catalog, store, world, reader, initial, links=evidence)
    catalog.reconcile(rows, complete=True)
    monkeypatch.setattr(lab_state, "_world", candidate)
    monkeypatch.setattr(lab_state, "_store", store)
    monkeypatch.setattr(lab_state, "_background_life", None)
    for name in ("FRIDAY_IDENTITY_READER", "FRIDAY_IDENTITY_INTEGRATION", "FRIDAY_TOWN_INSTALL_OPERATOR"):
        monkeypatch.setenv(name, "1")
    def forbidden(*_args, **_kwargs):
        pytest.fail("operator attempted to initialize or dispatch work")
    monkeypatch.setattr(lab_state, "ensure_started", forbidden)
    monkeypatch.setattr(lab_state, "_load_world", forbidden)
    monkeypatch.setattr(lab_state, "_service_background_life", forbidden)
    interpreter = SimpleNamespace(server_state={"identity_integration": IdentityIntegration(catalog, shadow, {})})
    bridge = WebviewBridge(StatusBus(), interpreter)
    try:
        yield SimpleNamespace(bridge=bridge, store=store, catalog=catalog, reader=reader, rows=rows, ids=ids,
                              backups=tmp_path / "town-install-backups")
    finally:
        store.close()


def test_preview_is_read_only_and_writes_are_opt_in(operator, monkeypatch):
    before = logical_rows(operator.store)
    state = deepcopy(lab_state._world)
    first = operator.bridge.get_town_install_preview()
    assert first == operator.bridge.get_town_install_preview()
    assert first["ok"] and first["confirmation"] and first["verified_links"][0]["resident"] == "friday"
    assert {row["id"] for row in first["unresolved"]} == {"research", "code", "vision"}
    assert len(first["placements"]) == 2 and first["catalog_revision"] and first["world_snapshot_id"]
    monkeypatch.delenv("FRIDAY_TOWN_INSTALL_OPERATOR")
    disabled = operator.bridge.get_town_install_preview()
    assert disabled["ok"] and disabled["confirmation"] is None
    assert not operator.bridge.confirm_town_installation(first["confirmation"])["ok"]
    monkeypatch.setattr(lab_state, "_world", None)
    assert operator.bridge.get_town_install_preview()["status"] == "unavailable"
    monkeypatch.setattr(lab_state, "_world", state)
    assert lab_state._world == state and logical_rows(operator.store) == before and not operator.backups.exists()


def test_concurrent_installation_preserves_owners_and_verifies_backup(operator, monkeypatch):
    store, bridge = operator.store, operator.bridge
    assert store._conn.execute("PRAGMA wal_autocheckpoint=0").fetchone() is not None
    store._exec("INSERT INTO roster_tasks(id,owner,state) VALUES (?,?,?)", ("task-kept", "friday", "completed"))
    before = deepcopy(lab_state._world)
    preview = bridge.get_town_install_preview()
    proposal = operator.reader.snapshot()["installation_proposal"]
    backup_calls = []
    def fenced_backup(catalog, store, proposal, batch_id):
        for source in (store.path, catalog.path):
            with closing(sqlite3.connect(source, timeout=0)) as connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("BEGIN IMMEDIATE")
        backup_calls.append(batch_id)
        return identity_backup.create_backup(catalog, store, proposal, batch_id)
    monkeypatch.setattr(identity_install, "create_backup", fenced_backup)
    barrier = Barrier(2)
    def submit():
        barrier.wait(timeout=4)
        return bridge.confirm_town_installation(preview["confirmation"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [executor.submit(submit) for _ in range(2)]
        results = [call.result(timeout=12) for call in calls]
    receipt = next(row for row in results if row["ok"])
    assert sorted(row["status"] for row in results) == ["conflict", "installed"]
    assert not bridge.confirm_town_installation(preview["confirmation"])["ok"]
    with lab_state._lock, pytest.raises(ValueError, match="already has a receipt"):
        identity_install.apply(operator.catalog, store, before, operator.reader, proposal,
                               confirmation=preview["confirmation"])
    assert len(list(operator.backups.iterdir())) == 1
    assert [asdict(char) for char in lab_state._world.characters[:4]] == [asdict(char) for char in before.characters]
    assert all(not c.memory and c.task_ref is None for c in lab_state._world.characters[4:])
    assert store._conn.execute("SELECT owner FROM roster_tasks WHERE id='task-kept'").fetchone()[0] == "friday"
    folder = operator.backups / receipt["backup_id"]
    manifest = identity_backup.verify_backup(folder)
    assert backup_calls == [receipt["batch_id"]]
    assert manifest["batch_id"] == receipt["batch_id"] and manifest["revision"] == preview["catalog_revision"]
    assert manifest["world_snapshot_id"] == preview["world_snapshot_id"]
    with closing(sqlite3.connect((folder / "world.db").as_uri() + "?immutable=1", uri=True)) as conn:
        assert conn.execute("SELECT id, owner FROM roster_tasks").fetchone() == ("task-kept", "friday")
        assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 4
        assert conn.execute("SELECT memory_json FROM characters WHERE id='friday'").fetchone()[0] == json.dumps(
            before.characters[0].memory)
    with (folder / "world.db").open("ab") as damaged:
        damaged.write(b"fixture")
    with pytest.raises(ValueError, match="checksum"):
        identity_backup.verify_backup(folder)


@pytest.mark.parametrize("change", ["world", "catalog", "history", "capacity", "alias", "mapping", "collision"])
def test_stale_and_blocked_previews_create_no_backup(operator, change):
    bridge, store = operator.bridge, operator.store
    token = bridge.get_town_install_preview()["confirmation"]
    world = lab_state._world
    if change == "world":
        world.characters[0].needs["energy"] += 1
    elif change == "catalog":
        operator.catalog.reconcile(operator.rows + [config("new", "brain")], complete=True)
    elif change == "history":
        store.append_event(world.tick, "friday", "observation", "later record")
    elif change == "capacity":
        world.walkable = {(char.x, char.y) for char in world.characters}
        assert store.save(world)["ok"]
    elif change == "alias":
        row = operator.rows[0]
        route = Route(row["provider"], row["deployment"], row["model"])
        operator.catalog.reconcile(operator.rows, complete=True,
            aliases=(AliasClaim(route, operator.ids["fallback"], "conflicting-alias", True),))
    elif change == "mapping":
        with store.transaction():
            identity_install._put_link(store, asdict(linked(operator.ids, source="research",
                target="fallback", verified=False)), "fixture-conflict")
        world.identity_links = store.identity_links()
    else:
        world.characters[1].id = operator.ids["fallback"]
        assert store.save(world)["ok"]
    before = logical_rows(store)
    preview = bridge.get_town_install_preview()
    if change in ("world", "capacity", "alias", "mapping", "collision"):
        assert preview["confirmation"] is None and preview["blocks"]
    if change == "capacity":
        assert preview["capacity"]["unplaced"] == len(preview["unplaced"]) > 0
    if change == "mapping":
        assert preview["conflicts"][0]["reason"] == "mapping is unverified"
    if change == "collision":
        assert any("UUID already belongs" in reason for reason in preview["blocks"])
    assert bridge.confirm_town_installation(token)["status"] == "conflict"
    assert logical_rows(store) == before and not operator.backups.exists()


def test_backup_failure_blocks_install_and_sanitizes_the_error(operator, monkeypatch):
    preview = operator.bridge.get_town_install_preview()
    before = logical_rows(operator.store)
    def fail(folder):
        raise OSError(f"synthetic private path {folder}")
    monkeypatch.setattr(identity_backup, "verify_backup", fail)
    result = operator.bridge.confirm_town_installation(preview["confirmation"])
    assert result == {"ok": False, "status": "backup_failed", "reason": "Pre-install backup could not be verified."}
    assert logical_rows(operator.store) == before and len(lab_state._world.characters) == 4


def test_transaction_failure_leaves_only_recoverable_orphan_backup(operator):
    store, bridge = operator.store, operator.bridge
    store._exec("CREATE TRIGGER fail_receipt BEFORE INSERT ON identity_installations "
                "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    preview = bridge.get_town_install_preview()
    before = logical_rows(store)
    assert bridge.confirm_town_installation(preview["confirmation"])["status"] == "error"
    assert logical_rows(store) == before and len(lab_state._world.characters) == 4
    backup, = operator.backups.iterdir()
    assert identity_backup.verify_backup(backup)["world_snapshot_id"] == preview["world_snapshot_id"]
    assert bridge.get_town_install_preview()["receipt"] is None


@pytest.mark.parametrize("later", [None, "history", "world"])
def test_rollback_after_reopen_and_refusal_after_later_activity(operator, monkeypatch, later):
    bridge = operator.bridge
    preview = bridge.get_town_install_preview()
    receipt = bridge.confirm_town_installation(preview["confirmation"])
    assert receipt["ok"]
    reopened = Store(operator.store.path)
    try:
        world = reopened.load()
        monkeypatch.setattr(lab_state, "_world", world)
        monkeypatch.setattr(lab_state, "_store", reopened)
        assert bridge.get_town_install_preview()["receipt"]["rollback_allowed"]
        if later == "history":
            reopened._exec("INSERT INTO roster_tasks(id,owner,state) VALUES (?,?,?)", ("new", "friday", "pending"))
        elif later == "world":
            world.characters[0].needs["energy"] += 1
        before = logical_rows(reopened)
        status = bridge.get_town_install_preview()["receipt"]
        assert status["rollback_allowed"] is (later is None)
        result = bridge.rollback_town_installation(receipt["batch_id"])
        if later is None:
            assert result["status"] == "rolled_back"
            assert len(reopened.load().characters) == 4 and len(reopened.identity_links()) == 1
            assert bridge.get_town_install_preview()["receipt"]["state"] == "rolled_back"
            assert not bridge.confirm_town_installation(preview["confirmation"])["ok"]
        else:
            assert result["status"] == "conflict"
            assert "activity or history" in status["rollback_block"]
            assert logical_rows(reopened) == before and len(lab_state._world.characters) == 6
    finally:
        reopened.close()
