"""Shared reader exercises real catalog, shadow, world capture and synthetic stores."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from source.server import lab_state
from source.server.lab_memory import LabMemory
from source.server.world import Store, sim
from source.server.world.identity_catalog import IdentityCatalog, Route, read_shadow_projection
from source.server.world.identity_reader import IdentityReader, ResidentLink, from_environment
from source.server.world.identity_shadow import DutyShadow


def config(model, *caps, kind="reasoning"):
    return {**Route("fixture", model, model).public(), "kind": kind, "capabilities": caps}


@pytest.fixture
def setup_reader(tmp_path, monkeypatch):
    catalog = IdentityCatalog(tmp_path / "catalog.db")
    rows = [config("primary", "brain", "vision"), config("fallback", "brain"),
            config("local", "brain"), config("listen", "listen", kind="service")]
    catalog.reconcile(rows, complete=True, legacy_refs=[("memory", "friday")])
    shadow = DutyShadow(catalog.path, tmp_path / "duty.db", enabled=True)
    world = sim.new_world(seed=19)
    world.characters[0].needs["energy"] = 63.25
    world.characters[0].state = "working"
    world.characters[0].task_ref = "task-kept"
    world.characters[0].activity_ticks = 4
    world.characters[0].memory = ["original experience"]
    monkeypatch.setattr(lab_state, "_world", world)
    reader = IdentityReader(shadow, lab_state.capture_identity_world, enabled=True)
    ids = {b["deployment"]: b["character_id"]
           for b in read_shadow_projection(catalog.path)["bindings"].values()}
    return catalog, shadow, world, reader, rows, ids


def linked(ids, source="friday", target="primary", **kwargs):
    return ResidentLink("town", source, ids[target], "verified-" + source, **{"verified": True, **kwargs})


def test_default_off_does_not_capture_initialize_or_observe(tmp_path):
    def forbidden():
        pytest.fail("disabled reader invoked its dependencies")
    shadow = DutyShadow(tmp_path / "catalog.db", tmp_path / "history.db")
    reader = from_environment(shadow, forbidden, env={})
    assert reader.snapshot(links=["invalid"]) == {"enabled": False, "reason": "identity reader gate disabled"}
    assert not list(tmp_path.iterdir())


def test_real_capture_is_detached_and_does_not_load_world(monkeypatch):
    monkeypatch.setattr(lab_state, "_world", None)
    monkeypatch.setattr(lab_state, "_load_world", lambda: pytest.fail("capture initialized a store"))
    assert lab_state.capture_identity_world() is None
    world = sim.new_world()
    monkeypatch.setattr(lab_state, "_world", world)
    captured = lab_state.capture_identity_world()
    assert captured is not world and captured.characters[0] is not world.characters[0]
    captured.characters[0].needs["energy"] = 100
    assert world.characters[0].needs["energy"] != 100


def test_deterministic_reachable_distinct_placement_and_selected_views(setup_reader):
    catalog, shadow, world, reader, rows, ids = setup_reader
    before = deepcopy(world)
    selected = ids["fallback"]
    first = reader.snapshot(selected, links=[linked(ids)])
    assert reader.snapshot(selected, links=[linked(ids)]) == first
    assert first["source_world"]["characters"] == [asdict(c) for c in world.characters]
    assert first["town"]["placement_only"]
    proposal = first["installation_proposal"]
    assert proposal["expected_world_snapshot_id"] == first["world_snapshot_id"]
    assert {r["character_id"] for r in proposal["new_residents"]} == {ids["fallback"], ids["local"]}
    assert proposal["existing_links"][0]["resident_id"] == "friday"
    for view in (first["roster"], first["town"]):
        for key in ("catalog_revision", "world_snapshot_id", "selected_character_id", "read_id"):
            assert view[key] == first[key]
    assert first["selected_character_id"] == selected
    positions = []
    for row in first["town"]["residents"]:
        cell = tuple(row["placement"]["cell"])
        positions.append(cell)
        assert cell in world.walkable
        assert sim.astar(world, tuple(first["town"]["anchor"]), cell) is not None
        if row["id"] != ids["primary"]:
            assert cell not in {(c.x, c.y) for c in world.characters}
            assert row["placement"]["simulation"] == "not_installed"
    assert len(positions) == len(set(positions))
    primary = next(c for c in first["roster"]["characters"] if c["id"] == ids["primary"])
    assert primary["source_state"] == asdict(world.characters[0])
    assert primary["source_state"]["id"] == "friday"
    assert primary["source_state"]["task_ref"] == "task-kept"
    assert primary["placement"]["simulation"] == "existing_source_resident"
    assert world == before
    assert catalog.reconcile(list(reversed(rows)), complete=True) == first["catalog_revision"]
    assert reader.snapshot(selected, links=[linked(ids)]) == first


def test_unverified_conflicting_wrong_domain_and_missing_links_never_guess(setup_reader):
    _, _, world, reader, _, ids = setup_reader
    links = [linked(ids, verified=False),
             ResidentLink("memory", "research", ids["fallback"], "wrong-domain", True),
             ResidentLink("town", "missing", ids["local"], "missing-source", True)]
    result = reader.snapshot(links=links)
    assert len(result["roster"]["mapping_issues"]) == 3
    assert all(c["resolution"] == "unresolved" for c in result["town"]["legacy_residents"])
    assert {c["source_resident_id"] for c in result["town"]["legacy_residents"]} == {c.id for c in world.characters}
    assert result["roster"]["unresolved"][0]["legacy_id"] == "friday"
    assert all(c["source_state"] is None for c in result["town"]["residents"])
    conflicting = [linked(ids), linked(ids, target="fallback")]
    result = reader.snapshot(links=conflicting)
    assert len(result["roster"]["mapping_issues"]) == 2
    assert not any(c["source_state"] for c in result["town"]["residents"])
    assert reader.snapshot("FRIDAY")["selection_status"] == "unavailable"
    with pytest.raises(ValueError):
        ResidentLink("town", "friday", "friday", "bad", True)


def test_capacity_disconnected_cells_and_bad_source_are_visible(setup_reader):
    _, _, world, reader, _, ids = setup_reader
    world.walkable = {(10, 12), (10, 13), (20, 20)}
    world.characters = [sim.Character("friday", "FRIDAY", x=10, y=12)]
    # Front is occupied, one reachable plain floor cell remains; disconnected
    # floor cannot be presented as spare placement capacity.
    result = reader.snapshot(links=[linked(ids)])
    staged = result["town"]["residents"]
    assert result["town"]["capacity"]["unplaced"] == 1
    assert sum(c["placement"]["status"] == "proposed" for c in staged) == 1
    unplaced = next(c for c in staged if c["placement"]["status"] == "unplaced")
    assert unplaced["placement"]["cell"] is None and "capacity" in unplaced["placement"]["reason"]
    assert len(result["roster"]["characters"]) == len(staged) == 3
    world.characters.append(sim.Character("research", "Research", x=10, y=12))
    again = reader.snapshot(links=[linked(ids)])
    assert again["town"]["source_issues"][0]["reason"] == "overlapping source residents"
    primary = next(c for c in again["town"]["residents"] if c["id"] == ids["primary"])
    assert primary["placement"]["status"] == "invalid_source_position"
    assert len(world.characters) == 2 and all(c.x == 10 and c.y == 12 for c in world.characters)


def test_archive_reappearance_and_reader_rollback_do_not_publish(setup_reader):
    catalog, shadow, world, reader, rows, ids = setup_reader
    first = reader.snapshot(ids["primary"], links=[linked(ids)])
    old_revision = first["catalog_revision"]
    catalog.reconcile(rows[1:] + [config("new", "vision")], complete=True)
    current = reader.snapshot(ids["primary"], links=[linked(ids)])
    primary = next(c for c in current["roster"]["characters"] if c["id"] == ids["primary"])
    assert primary["status"] == "archived" and primary["source_state"]["id"] == "friday"
    assert current["selected_character_id"] == ids["primary"]
    assert not any(c["id"] == ids["primary"] for c in current["town"]["residents"])
    before = catalog.path.read_bytes()
    rolled = reader.snapshot(ids["primary"], links=[linked(ids)], catalog_revision=old_revision)
    assert rolled == first
    assert catalog.path.read_bytes() == before
    assert reader.snapshot(ids["primary"], links=[linked(ids)]) == current
    catalog.reconcile(rows, complete=True)
    returned = reader.snapshot(ids["primary"], links=[linked(ids)])
    recovered = next(c for c in returned["town"]["residents"] if c["id"] == ids["primary"])
    assert recovered["source_state"] == primary["source_state"]
    assert recovered["placement"] == primary["placement"]
    with pytest.raises(ValueError, match="revision"):
        reader.snapshot(catalog_revision=999999)


def test_source_and_projection_mutation_cannot_cross_snapshot_boundaries(setup_reader):
    catalog, shadow, world, reader, rows, ids = setup_reader
    original = shadow.snapshot
    old_revision = original()["catalog_revision"]
    calls = []

    def interleaved(*args, **kwargs):
        calls.append(1)
        result = original(*args, **kwargs)
        catalog.reconcile(rows + [config("new", "brain")], complete=True)
        with lab_state._lock:
            world.tick = 44
            world.characters[0].needs["energy"] = 77
        return result

    shadow.snapshot = interleaved
    result = reader.snapshot(ids["primary"], links=[linked(ids)])
    assert calls == [1]
    assert result["roster"]["catalog_revision"] == result["town"]["catalog_revision"] == old_revision
    assert result["roster"]["world_tick"] == result["town"]["world_tick"] == 44
    result["town"]["residents"].clear()
    result["roster"]["characters"][0]["bindings"].clear()
    assert len(reader.snapshot()["town"]["residents"]) == 4
    assert len(world.characters) == 4


def test_synthetic_reopen_and_legacy_history_remain_untouched(setup_reader, tmp_path, monkeypatch):
    catalog, shadow, world, reader, _, ids = setup_reader
    path = tmp_path / "source.db"
    store = Store(path)
    world.pending_events.append({"tick": 1, "actor": "friday", "kind": "routine",
                                 "text": "Preserve occurrence reference", "event_key": "fixture-occurrence",
                                 "created_at": 1.0})
    assert store.save(world)["ok"]
    assert store.append_event(1, "friday", "routine", "Preserve original actor")
    with store.transaction():
        store._exec_tx("INSERT INTO roster_tasks (id,owner,state) VALUES (?,?,?)",
                       ("task-kept", "friday", "completed"))
    store.close()
    memory_path = tmp_path / "source-memory.db"
    memory = LabMemory(memory_path, world_db=path)
    assert memory.record_experience("friday", tick=1, kind="observation", text="Original experience")["created"]
    assert memory.grant_artifact("artifact-kept", task_id="task-kept", char_id="friday", label="Original")["created"]
    store = Store(path)
    loaded = store.load()
    store.close()
    monkeypatch.setattr(lab_state, "_world", loaded)
    first = reader.snapshot(ids["primary"], links=[linked(ids)])
    before = {p: p.read_bytes() for p in (path, memory_path, catalog.path, tmp_path / "duty.db")}
    for _ in range(5):
        assert reader.snapshot(ids["primary"], links=[linked(ids)]) == first
    assert {p: p.read_bytes() for p in before} == before
    store = Store(path)
    reloaded = store.load()
    store.close()
    monkeypatch.setattr(lab_state, "_world", reloaded)
    reopened = IdentityReader(shadow, lab_state.capture_identity_world, enabled=True)
    assert reopened.snapshot(ids["primary"], links=[linked(ids)]) == first
    assert all(e["actor"] == "friday" for e in first["source_world"]["last_events"])
    assert first["source_world"]["last_events"][0]["id"] == list(loaded.events)[0]["id"]
    assert memory.scrapbook("friday")["experiences"][0]["char_id"] == "friday"
    assert {c.id for c in reloaded.characters} == {c.id for c in world.characters}


def test_services_do_not_spawn_and_reading_never_ticks_or_plans(setup_reader):
    _, _, world, reader, _, _ = setup_reader
    before = asdict(world)
    for _ in range(10):
        result = reader.snapshot()
    assert len(result["town"]["residents"]) == 3
    assert len(result["town"]["services"]) == 1
    assert all("character_id" not in c for c in result["town"]["services"])
    assert all(c["representation"] == "procedural" for c in result["town"]["services"])
    assert asdict(world) == before and world.planner_calls == 0
    assert all(c["placement"]["simulation"] == "not_installed" for c in result["town"]["residents"])


def test_gate_on_unloaded_world_has_no_fake_snapshot(setup_reader, monkeypatch):
    _, _, _, reader, _, _ = setup_reader
    monkeypatch.setattr(lab_state, "_world", None)
    assert reader.snapshot() == {"enabled": True, "available": False, "reason": "source world not loaded"}


@pytest.mark.parametrize("population", [12, 16])
def test_staged_population_reserves_distinct_floor_without_installing(setup_reader, population):
    catalog, _, world, reader, _, _ = setup_reader
    original = deepcopy(world)
    catalog.reconcile([config("model-%02d" % i, "brain") for i in range(population)], complete=True)
    result = reader.snapshot()
    placed = [tuple(row["placement"]["cell"]) for row in result["town"]["residents"]]
    assert len(placed) == len(set(placed)) == population
    assert not result["town"]["capacity"]["unplaced"]
    for cell in placed:
        assert cell not in {(c.x, c.y) for c in world.characters}
        assert sim.astar(world, tuple(result["town"]["anchor"]), cell) is not None
    assert world == original and len(world.characters) == 4


def test_reader_rollback_retains_newer_identity_and_duty_provenance(setup_reader):
    catalog, shadow, world, reader, rows, _ = setup_reader
    old_revision = reader.snapshot()["catalog_revision"]
    route = Route("fixture", "new", "new")
    catalog.reconcile(rows + [config("new", "vision")], complete=True)
    request = shadow.begin_request("actual-synthetic-owner")
    attempt = shadow.observe_selected(request, "vision", 1, route)
    latest = reader.snapshot()
    cid = latest["duty"]["current"]["vision"]["character_id"]
    before = catalog.path.read_bytes()
    rolled = reader.snapshot(cid, catalog_revision=old_revision)
    assert rolled["selection_status"] == "unavailable"
    assert rolled["roster"]["selected_character_id"] is rolled["town"]["selected_character_id"] is None
    assert rolled["duty"]["current"]["vision"]["character_id"] is None
    assert rolled["duty"]["current"]["vision"]["admitted_revision"] == latest["catalog_revision"]
    assert shadow.finish(attempt, "success")
    assert reader.snapshot(cid)["selected_character_id"] == cid
    assert reader.snapshot()["duty"]["last_success"]["vision"]["character_id"] == cid
    assert catalog.path.read_bytes() == before
