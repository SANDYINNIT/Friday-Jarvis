"""Synthetic, offline catalog verification. The legacy stores stay separate."""
import json
import sqlite3

import pytest

from source.server.lab_memory import LabMemory
from source.server.world import Store, new_world
from source.server.world.identity_catalog import AliasClaim, IdentityCatalog, Route, read_shadow_projection


def route(provider="modal", deployment="primary", model="vendor/model", caps=("brain",), **fields):
    return {"kind": "reasoning", "provider": provider, "deployment": deployment,
            "model": model, "capabilities": caps, **fields}


def owner(view, deployment="primary", model="vendor/model", provider="modal"):
    bindings = [b for b in view["bindings"].values() if
                (b["provider"], b["deployment"], b["model"]) == (provider, deployment, model)]
    ids = {b["character_id"] for b in bindings}
    assert len(ids) == 1
    return view["characters"][ids.pop()]


@pytest.fixture
def catalog(tmp_path):
    return IdentityCatalog(tmp_path / "identity.db")


def read(catalog, **kwargs):
    return read_shadow_projection(catalog.path, **kwargs)


def test_order_duplicate_credentials_and_removing_keys_do_not_change_identity(catalog):
    a = route(credentials=["test-credential-1"])
    b = route(provider="other", credentials=["test-credential-2"])
    c = route(deployment="secondary")
    d = route(model="vendor/other-model")
    baseline = catalog.reconcile([d, a, b, c, {**a, "credentials": ["test-credential-3"]}],
                                 complete=True)
    view = read(catalog)
    assert len(view["characters"]) == 4
    assert len(view["bindings"]) == 4
    assert len({r["name"] for r in view["characters"].values()}) == 4
    for rows in ([b, d, c, a], [c, {**a, "credentials": []}, d, b], [a, b, c, d]):
        assert catalog.reconcile(rows, complete=True) == baseline
        assert read(catalog) == view


def test_capabilities_retire_individually_and_final_removal_archives(catalog):
    a = route()
    vision = route(caps=("vision",))
    catalog.reconcile([a, vision], complete=True)
    combined = read(catalog)
    cid = owner(combined)["id"]
    ids_by_cap = {b["capability"]: b["id"] for b in combined["bindings"].values()}
    assert len(combined["characters"]) == 1
    assert set(ids_by_cap) == {"brain", "vision"}

    catalog.reconcile([route(enabled=False)], complete=True)
    disabled = read(catalog)
    assert disabled["characters"][cid]["status"] == "configured"
    assert disabled["bindings"][ids_by_cap["brain"]]["status"] == "disabled"
    assert disabled["bindings"][ids_by_cap["vision"]]["status"] == "retired"

    catalog.reconcile([], complete=True)
    retired = read(catalog)
    assert retired["characters"][cid]["status"] == "archived"
    assert {b["status"] for b in retired["bindings"].values()} == {"retired"}

    reopened = IdentityCatalog(catalog.path)
    reopened.reconcile([route(caps=("vision", "brain"))], complete=True)
    returned = read(reopened)
    assert returned["characters"] == combined["characters"]
    assert returned["bindings"] == combined["bindings"]


def test_verified_alias_reuses_character_and_preserves_original_route(catalog):
    catalog.reconcile([route()], complete=True)
    old = owner(read(catalog))
    renamed = Route("modal", "primary", "vendor/renamed")
    tentative = AliasClaim(renamed, old["id"], "suggestion")
    catalog.reconcile([route(model=renamed.model)], complete=True, aliases=[tentative])
    unresolved = read(catalog)
    assert len(unresolved["characters"]) == 1
    assert unresolved["characters"][old["id"]]["status"] == "archived"
    assert unresolved["unresolved"][0]["decision_ids"] == ["suggestion"]
    assert catalog.reconcile([route(model=renamed.model)], complete=True) == unresolved["revision"]

    verified = AliasClaim(renamed, old["id"], "confirmed-rename", verified=True)
    catalog.reconcile([route(model=renamed.model, caps=("brain", "vision"))],
                      complete=True, aliases=[verified])
    view = read(catalog)
    assert owner(view, model=renamed.model) == old
    assert not view["unresolved"]
    assert owner(view) == old
    assert len(view["characters"]) == 1
    assert {b["status"] for b in view["bindings"].values() if b["model"] == "vendor/model"} == {"retired"}
    assert catalog.reconcile([route(model=renamed.model, caps=("vision", "brain"))],
                             complete=True) == view["revision"]


def test_conflicting_claims_do_not_create_or_merge_owners(catalog):
    a, b = route(), route(deployment="secondary")
    catalog.reconcile([a, b], complete=True)
    first = owner(read(catalog))["id"]
    second = owner(read(catalog), deployment="secondary")["id"]
    alias_route = Route("other", "third", "vendor/model")
    claims = [AliasClaim(alias_route, first, "claim-a", True),
              AliasClaim(alias_route, second, "claim-b", True),
              AliasClaim(Route("modal", "primary", "vendor/model"), second, "claim-c", True)]
    rows = [a, b, route(provider="other", deployment="third")]
    rev = catalog.reconcile(rows, complete=True, aliases=claims)
    view = read(catalog)
    assert owner(view)["id"] == first
    assert owner(view, deployment="secondary")["id"] == second
    assert len(view["characters"]) == len(view["bindings"]) == 2
    assert {tuple(x["decision_ids"]) for x in view["unresolved"]} == {("claim-a", "claim-b"), ("claim-c",)}
    assert IdentityCatalog(catalog.path).reconcile(rows, complete=True) == rev


def test_services_are_typed_procedural_and_never_character_owners(catalog):
    services = [
        {**route(provider="voice", model="whisper", caps=("listen",)), "kind": "service"},
        {**route(provider="voice", model="edge", caps=("voice_out",)), "kind": "service"},
        {**route(provider="modal", model="vision-marker", caps=("vision",)), "kind": "service"},
        {**route(provider="n8n", model="engine", caps=("workflow",)), "kind": "service"},
    ]
    catalog.reconcile(services, complete=True)
    initial = read(catalog)
    assert not initial["characters"] and not initial["bindings"]
    assert {s["capability"] for s in initial["services"].values()} == {"listen", "voice_out", "vision", "workflow"}
    assert all(s["representation"] == "procedural" and "character_id" not in s
               for s in initial["services"].values())
    catalog.reconcile([], complete=True)
    assert {s["status"] for s in read(catalog)["services"].values()} == {"retired"}
    catalog.reconcile(list(reversed(services)), complete=True)
    assert read(catalog)["services"] == initial["services"]


def test_rollback_replays_exact_view_and_reuses_ever_reserved_identity(catalog):
    a, b = route(), route(deployment="secondary")
    first = catalog.reconcile([a], complete=True)
    before = read(catalog)
    later = catalog.reconcile([a, b], complete=True)
    after = read(catalog)

    reopened = IdentityCatalog(catalog.path)
    reopened.activate_revision(first)
    assert read(reopened) == before
    assert read(reopened, revision=later) == after
    next_revision = reopened.reconcile([b, a], complete=True)
    assert next_revision != later
    assert read(reopened) == {**after, "revision": next_revision}
    reopened.activate_revision(later)
    assert read(reopened) == after
    with pytest.raises(ValueError, match="does not exist"):
        reopened.activate_revision(999999)
    assert read(reopened) == after


def test_incomplete_conflicting_or_partially_written_batches_are_atomic(catalog):
    catalog.reconcile([route()], complete=True)
    before = read(catalog)
    cid = owner(before)["id"]
    with pytest.raises(ValueError, match="complete"):
        catalog.reconcile([], complete=False)
    with pytest.raises(ValueError, match="conflicting"):
        catalog.reconcile([route(), route(enabled=False)], complete=True)
    with pytest.raises(ValueError, match="alias decision ID"):
        catalog.reconcile([], complete=True, aliases=[
            AliasClaim(Route("modal", "a", "model"), cid, "same-id", True),
            AliasClaim(Route("modal", "b", "model"), cid, "same-id", True)])
    assert read(catalog) == before
    with sqlite3.connect(catalog.path) as conn:
        assert conn.execute("SELECT count(*) FROM catalog_alias_claim").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM catalog_revision").fetchone()[0] == 1
        conn.execute("""
            CREATE TRIGGER fail_catalog_publish BEFORE INSERT ON catalog_revision
            BEGIN SELECT RAISE(ABORT, 'synthetic publish fault'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic publish fault"):
        catalog.reconcile([route(), route(deployment="new-route")], complete=True)
    assert read(catalog) == before
    with sqlite3.connect(catalog.path) as conn:
        assert conn.execute("SELECT count(*) FROM catalog_character").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM catalog_route_owner").fetchone()[0] == 1


def test_shadow_projection_is_read_only_and_transport_data_is_not_persisted(catalog, tmp_path):
    endpoint = "https://synthetic-private.invalid/v1"
    secret = "synthetic-credential-must-not-be-serialized"
    rev = catalog.reconcile([route(credentials=[secret], endpoint=endpoint,
                                   api_key=secret, account_id="fixture-private-account")],
                            complete=True)
    before = catalog.path.read_bytes()
    view = read(catalog)
    view["characters"].clear()
    assert read(catalog)["characters"]
    assert read(catalog)["revision"] == rev
    assert catalog.path.read_bytes() == before
    for value in (endpoint, secret, "fixture-private-account"):
        assert value not in json.dumps(read(catalog))
        assert value.encode() not in before
    for invalid in (endpoint, "synthetic-private.invalid"):
        with pytest.raises(ValueError, match="public configuration identifier"):
            catalog.reconcile([route(deployment=invalid)], complete=True)
    absent = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        read_shadow_projection(absent)
    assert not absent.exists()


def test_legacy_stores_remain_byte_preserved_and_ownership_unresolved(catalog, tmp_path):
    world_path = tmp_path / "legacy-world.db"
    memory_path = tmp_path / "legacy-memory.db"
    world_store = Store(world_path)
    try:
        assert world_store.save(new_world())["ok"]
        assert world_store.append_event(1, "friday", "routine", "Historical observation")
        with world_store.transaction():
            world_store._exec_tx("INSERT INTO roster_tasks (id,owner,state) VALUES (?,?,?)",
                                 ("task-fixture", "friday", "completed"))
    finally:
        world_store.close()
    memory = LabMemory(memory_path, world_db=world_path)
    memory.remember(key="fixture-fact", content="Canonical legacy fact")
    assert memory.record_experience("friday", tick=1, kind="observation",
                                    text="Historical experience")["created"]
    assert memory.grant_artifact("artifact-fixture", task_id="task-fixture",
                                 char_id="friday", label="Historical artifact")["created"]
    before = {p: p.read_bytes() for p in (world_path, memory_path)}
    with pytest.raises(ValueError, match="dedicated"):
        IdentityCatalog(world_path)
    with pytest.raises(ValueError, match="dedicated"):
        read_shadow_projection(memory_path)
    catalog.reconcile([route()], complete=True, legacy_refs=[
        ("town", "friday"), ("roster", "friday"), ("memory", "friday"), ("town", "friday")])
    view = read(catalog)
    assert len(view["characters"]) == 1
    assert {(row["namespace"], row["legacy_id"]) for row in view["unresolved"]} == {
        ("town", "friday"), ("roster", "friday"), ("memory", "friday")}
    assert all("character_id" not in row for row in view["unresolved"])
    assert catalog.reconcile([route()], complete=True) == view["revision"]
    assert {p: p.read_bytes() for p in before} == before

