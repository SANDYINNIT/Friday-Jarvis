"""Actual owner-lock admission and SQLite transactions on explicit synthetic stores."""
from copy import deepcopy
from dataclasses import asdict
import importlib.util
from pathlib import Path
import sqlite3

import pytest

from source.server import lab_state as lab
from source.server.world import Store, sim
from source.server.world.identity_install import apply
from source.server.world.identity_reader import ResidentLink
from source.server.lab_memory import LabMemory
from test_identity_reader import setup_reader, linked, config
from test_world_store import TestBoundedRetention as _RetentionCases


@pytest.fixture
def installed_setup(setup_reader, tmp_path, monkeypatch):
    catalog, shadow, world, reader, rows, ids = setup_reader
    store = Store(tmp_path / "simulation.db")
    assert store.save(world)["ok"]
    monkeypatch.setattr(lab, "_store", store)
    monkeypatch.setattr(lab, "_load_world", lambda: (lab._world, None))
    emitted = []
    monkeypatch.setattr(lab, "_emit", lambda kind, event: emitted.append((kind, event)))
    try:
        yield catalog, store, reader, rows, ids, emitted
    finally:
        store.close()


def proposal(reader, ids):
    return reader.snapshot(links=[linked(ids)])["installation_proposal"]


def logical_rows(store):
    return {row[0]: [tuple(record) for record in store._conn.execute('SELECT * FROM "'+row[0]+'" ORDER BY rowid')]
            for row in store._conn.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")}


def test_install_preserves_legacy_and_creates_npc_ids_once(installed_setup):
    catalog, store, reader, _, ids, emitted = installed_setup
    original = deepcopy(lab._world)
    planned = proposal(reader, ids)
    result = lab.install_catalog_residents(catalog, reader, planned, links=[linked(ids)])
    assert result["ok"] and result["created"] == 2
    assert [asdict(c) for c in lab._world.characters[:4]] == [asdict(c) for c in original.characters]
    created = lab._world.characters[4:]
    assert {c.id for c in created} == {ids["fallback"], ids["local"]}
    assert all(not c.memory and not c.task_ref and c.needs == dict.fromkeys(sim.NEEDS,20.0) for c in created)
    assert lab._world.model_budget == lab._world.planner_calls == 0
    assert reader.snapshot()["installation_proposal"]["new_residents"] == []
    assert store.load().identity_links == lab._world.identity_links
    assert len(store.identity_links()) == 3
    before = logical_rows(store)
    duplicate = lab.install_catalog_residents(catalog, reader, planned, links=[linked(ids)])
    assert duplicate["status"] == "duplicate" and logical_rows(store) == before
    assert len(lab._world.characters) == 6 and not emitted


@pytest.mark.parametrize("change", ["world", "catalog", "mapping", "placement", "capacity", "gate", "capture"])
def test_revalidation_rejects_without_partial_changes(installed_setup, change):
    catalog, store, reader, rows, ids, _ = installed_setup
    planned = proposal(reader, ids)
    links = [linked(ids)]
    if change == "world":
        lab._world.characters[0].needs["energy"] += 1
    elif change == "catalog":
        catalog.reconcile(rows + [config("new", "brain")], complete=True)
    elif change == "mapping":
        links = [linked(ids, verified=False)]
    elif change == "placement":
        planned["new_residents"][0]["cell"] = [10,6]
    elif change == "capacity":
        lab._world.walkable = {(c.x,c.y) for c in lab._world.characters}
        planned = proposal(reader, ids)
    elif change == "capture":
        stale = deepcopy(lab._world)
        reader.capture_world = lambda: stale
        lab._world.characters[0].memory.append("New owner state")
    else:
        reader.enabled = False
    before, runtime = logical_rows(store), deepcopy(lab._world)
    result = lab.install_catalog_residents(catalog, reader, planned, links=links)
    assert not result["ok"]
    if change == "capacity":
        assert "capacity" in result["reason"]
    assert logical_rows(store) == before and lab._world == runtime


def test_transaction_failure_rolls_back_world_links_receipt_and_pending_journal(installed_setup):
    catalog, store, reader, _, ids, emitted = installed_setup
    sim._note(lab._world, lab._world.characters[0], "observation", "Preserve occurrence")
    planned = proposal(reader, ids)
    store._exec("CREATE TRIGGER fail_new_actor BEFORE INSERT ON characters "
                "WHEN NEW.id NOT IN ('friday','research','code','vision') "
                "BEGIN SELECT RAISE(ABORT, 'synthetic transaction failure'); END")
    before, runtime = logical_rows(store), deepcopy(lab._world)
    result = lab.install_catalog_residents(catalog, reader, planned, links=[linked(ids)])
    assert not result["ok"] and logical_rows(store) == before and lab._world == runtime
    assert not emitted and not store.events()
    store._exec("DROP TRIGGER fail_new_actor")
    assert lab.install_catalog_residents(catalog, reader, planned, links=[linked(ids)])["ok"]
    assert len(emitted) == 1 and len(store.events()) == 1
    assert not lab._world.pending_events
    assert lab.install_catalog_residents(catalog, reader, planned, links=[linked(ids)])["status"] == "duplicate"
    assert len(emitted) == 1


def test_durable_recovery_before_publication_and_config_reappearance(installed_setup):
    catalog, store, reader, rows, ids, _ = installed_setup
    planned = proposal(reader, ids)
    # Simulate process loss after commit but before the owner publishes memory.
    with lab._lock:
        _, receipt = apply(catalog, store, lab._world, reader, planned, links=[linked(ids)])
    assert receipt["ok"] and len(lab._world.characters) == 4
    lab._world = store.load()
    assert len(lab._world.characters) == 6
    assert not reader.snapshot()["installation_proposal"]["new_residents"]
    before = {c.id: asdict(c) for c in lab._world.characters}
    catalog.reconcile([], complete=True)
    assert not reader.snapshot()["town"]["residents"]
    assert len(lab._world.characters) == 6
    catalog.reconcile(rows, complete=True)
    assert not reader.snapshot()["installation_proposal"]["new_residents"]
    fresh = reader.snapshot()["installation_proposal"]
    assert lab.install_catalog_residents(catalog, reader, fresh)["ok"]
    assert {c.id: asdict(c) for c in lab._world.characters} == before


def test_pristine_rollback_survives_reopen_and_keeps_catalog_reservations(installed_setup):
    catalog, store, reader, _, ids, _ = installed_setup
    original = deepcopy(lab._world)
    original.characters[1].path = [(8,6),(7,6)]
    lab._world = original
    result = lab.install_catalog_residents(catalog, reader, proposal(reader, ids), links=[linked(ids)])
    assert result["ok"]
    lab._world = store.load()
    restored = lab.rollback_catalog_installation(result["batch_id"])
    assert restored["ok"], restored
    assert lab._world == original
    assert store.load().characters[1].path == [(8,6),(7,6)]
    assert not store.identity_links()
    assert lab.rollback_catalog_installation(result["batch_id"])["status"] == "already_rolled_back"
    assert ids["local"] in {c["id"] for c in reader.snapshot()["roster"]["characters"]}


@pytest.mark.parametrize("activity", ["tick", "memory", "event", "task"])
def test_rollback_refuses_later_activity_without_erasing_history(installed_setup, activity):
    catalog, store, reader, _, ids, _ = installed_setup
    result = lab.install_catalog_residents(catalog, reader, proposal(reader, ids), links=[linked(ids)])
    if activity == "tick":
        assert lab._advance_world(lab._world, persist=False)["ok"]
    elif activity == "memory":
        lab._world.characters[-1].memory.append("Later actual history")
    elif activity == "event":
        assert store.append_event(1, ids["local"], "observation", "Later event")
    else:
        store._exec("INSERT INTO roster_tasks(id,owner,state) VALUES (?,?,?)", ("later",ids["local"],"queued"))
    before, runtime = logical_rows(store), deepcopy(lab._world)
    refused = lab.rollback_catalog_installation(result["batch_id"])
    assert not refused["ok"] and "post-install" in refused["reason"]
    assert logical_rows(store) == before and lab._world == runtime


def test_installation_requires_owner_lock_and_catalog_fence(installed_setup, monkeypatch):
    catalog, store, reader, _, ids, _ = installed_setup
    original = store._save_world_tx
    seen = []
    def checked(world, pending):
        assert lab._lock._is_owned() and store.in_transaction()
        other = sqlite3.connect(catalog.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()
        seen.append(True)
        return original(world,pending)
    monkeypatch.setattr(store,"_save_world_tx",checked)
    assert lab.install_catalog_residents(catalog,reader,proposal(reader,ids),links=[linked(ids)])["ok"]
    assert seen == [True]


def test_older_rollback_cannot_undo_a_newer_installation(installed_setup):
    catalog, store, reader, rows, ids, _ = installed_setup
    first = lab.install_catalog_residents(catalog,reader,proposal(reader,ids),links=[linked(ids)])
    catalog.reconcile(rows+[config("new","brain")],complete=True)
    second = lab.install_catalog_residents(catalog,reader,reader.snapshot()["installation_proposal"])
    assert first["ok"] and second["ok"]
    state = logical_rows(store)
    refused = lab.rollback_catalog_installation(first["batch_id"])
    assert not refused["ok"] and "newer installation" in refused["reason"]
    assert logical_rows(store) == state
    assert lab.rollback_catalog_installation(second["batch_id"])["ok"]
    assert lab.rollback_catalog_installation(first["batch_id"])["ok"]


def test_canonical_experiences_artifacts_and_original_owners_are_preserved(installed_setup,tmp_path):
    catalog, store, reader, _, ids, _ = installed_setup
    memory_path=tmp_path/"original-memory.db"
    memory=LabMemory(memory_path,world_db=store.path)
    assert memory.record_experience("friday",tick=1,kind="observation",text="Original experience")["created"]
    assert memory.grant_artifact("original-artifact",task_id="task-kept",char_id="friday",label="Original")["created"]
    store._exec("INSERT INTO roster_tasks(id,owner,state) VALUES (?,?,?)",("task-kept","friday","completed"))
    before=memory_path.read_bytes()
    assert lab.install_catalog_residents(catalog,reader,proposal(reader,ids),links=[linked(ids)])["ok"]
    for _ in range(60):
        assert lab._advance_world(lab._world,persist=False)["ok"]
    assert lab._save_world(lab._world)["ok"]
    lab._world=store.load()
    assert memory_path.read_bytes()==before
    assert memory.scrapbook("friday")["experiences"][0]["char_id"]=="friday"
    assert store._conn.execute("SELECT owner FROM roster_tasks WHERE id='task-kept'").fetchone()[0]=="friday"


def test_receipt_and_rollback_record_failures_cannot_partially_publish(installed_setup):
    catalog,store,reader,rows,ids,_=installed_setup
    first=lab.install_catalog_residents(catalog,reader,proposal(reader,ids),links=[linked(ids)])
    catalog.reconcile(rows+[config("new","brain")],complete=True)
    store._exec("CREATE TRIGGER fail_receipt BEFORE INSERT ON identity_installations "
                "BEGIN SELECT RAISE(ABORT, 'receipt failure'); END")
    before,runtime=logical_rows(store),deepcopy(lab._world)
    assert not lab.install_catalog_residents(catalog,reader,reader.snapshot()["installation_proposal"])["ok"]
    assert logical_rows(store)==before and lab._world==runtime
    store._exec("DROP TRIGGER fail_receipt")
    store._exec("CREATE TRIGGER fail_restore BEFORE UPDATE ON identity_installations "
                "BEGIN SELECT RAISE(ABORT, 'restore failure'); END")
    before=logical_rows(store)
    assert not lab.rollback_catalog_installation(first["batch_id"])["ok"]
    assert logical_rows(store)==before and lab._world==runtime


@pytest.mark.parametrize("case", [name for name in vars(_RetentionCases) if name.startswith("test_")])
def test_existing_retention_cases_with_bounded_synthetic_wal(tmp_path, case):
    store = Store(tmp_path / "world.db")
    # Test-only fixture choice. Actual append/prune/checkpoint code is unchanged.
    # This does not establish default-journal crash/power-loss durability.
    store._exec("PRAGMA journal_mode=WAL")
    store._exec("PRAGMA synchronous=NORMAL")
    try:
        getattr(_RetentionCases(), case)(store)
    finally:
        store.close()


@pytest.mark.parametrize("population", [4,12,16])
@pytest.mark.parametrize("seed", [7,19])
def test_installed_living_world_and_reopen(installed_setup, tmp_path, monkeypatch, population, seed):
    catalog, store, reader, _, ids, emitted = installed_setup
    world = sim.new_world(seed=seed)
    world.characters[0].memory = ["Preserve original owner"]
    world.characters[0].needs = dict.fromkeys(sim.NEEDS,95.0)
    lab._world = world
    rows = [config("primary","brain","vision")]+[config("new-%02d"%i,"brain") for i in range(population-4)]
    catalog.reconcile(rows,complete=True)
    assert lab.install_catalog_residents(catalog,reader,proposal(reader,ids),links=[linked(ids)])["ok"]
    assert len(lab._world.characters) == population
    initial_memory = {c.id:list(c.memory) for c in lab._world.characters}
    generated, delivered = set(), []
    monkeypatch.setattr(lab,"_emit",lambda kind,event:delivered.append(event["event_key"]) if kind=="world_event" else None)
    spec=importlib.util.spec_from_file_location("town_measure",Path(__file__).parent/"tools/town_review_probe_2026-11-07.py")
    probe=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    def persist(w):
        generated.update(e["event_key"] for e in w.pending_events)
        if w.tick%60==0:
            assert lab._save_world(w)["ok"]
        if w.tick==720:
            lab._world = store.load()
            return lab._world
    metrics=probe.measure(seed,population,"default",1440,world=lab._world,on_tick=persist)
    assert metrics["invalid_move_count"] == metrics["overlap_character_ticks"] == metrics["planner_calls"] == 0
    assert set(metrics["recovery_crossings"]) == set(sim.NEEDS)
    assert {"rest","eat","explore","social"} <= set(metrics["completed_routines"])
    # BLOCK_LIMIT bounds one goal attempt; choosing another goal resets that
    # attempt. Aggregate blocked state across goals has a separate travel bound.
    assert metrics["max_blocked_attempt_ticks"] <= sim.BLOCK_LIMIT
    assert metrics["max_consecutive_blocked_ticks"] <= sim.TRAVEL_LIMIT
    assert {c.id:c.memory for c in lab._world.characters} == initial_memory
    rows=store.events(limit=100000)
    keys={e["event_key"] for e in rows}
    assert generated==keys==set(delivered) and len(delivered)==len(keys)
    assert lab._save_world(lab._world)["ok"] and len(delivered)==len(keys)
    metrics.update(journal_occurrences=len(keys),journal_exactly_once=True,original_memory_preserved=True)
    (tmp_path/"measurements.json").write_text(__import__('json').dumps(metrics,indent=2),encoding="utf-8")
