"""Town regressions use disposable worlds and explicit synthetic SQLite paths."""
from datetime import datetime, timezone

from source.server import lab_map, lab_memory, lab_state
from source.server.world import Store, sim


def test_routines_complete_and_npc_history_is_preserved():
    world = sim.new_world()
    calls = []
    for c in world.characters:
        c.memory = ["historic %d" % i for i in range(45)]
        c.needs = dict.fromkeys(sim.NEEDS, 95.0)
    for _ in range(500):
        sim.tick(world, planner=lambda *args: calls.append(args))
    assert not calls and world.planner_calls == 0
    for c in world.characters:
        assert c.memory == ["historic %d" % i for i in range(45)]
        completed = {e["text"].rsplit(" ", 1)[-1] for e in world.pending_events
                     if e["kind"] == "completed" and e["actor"] == c.id}
        assert {"rest", "eat", "explore"} <= completed
    assert any(e["kind"] == "completed" and e["text"].endswith(" social")
               for e in world.pending_events)


def test_impossible_social_attempt_yields_to_other_needs():
    world = sim.new_world(characters=1)
    c = world.characters[0]
    c.goal, c.needs["social"], c.needs["energy"] = "social", 100, 90
    for _ in range(sim.BLOCK_LIMIT + 35):
        sim.tick(world)
    entries = world.pending_events
    assert sum(e["kind"] == "blocked" for e in entries) < 5
    assert any(e["kind"] == "abandoned" and "conversation partner" in e["text"]
               for e in entries)
    assert any(e["kind"] == "completed" and e["text"].endswith(" rest")
               for e in entries)


def test_world_journal_rollover_retry_step_catchup_and_reload(tmp_path, monkeypatch):
    store = Store(tmp_path / "world.db")
    world = sim.new_world()
    world.paused = True
    published = []
    monkeypatch.setattr(lab_state, "_world", world)
    monkeypatch.setattr(lab_state, "_store", store)
    monkeypatch.setattr(lab_state, "_emit", lambda kind, event: published.append((kind, event)))
    monkeypatch.setattr(lab_state, "db_path", lambda: str(store.path))
    try:
        for i in range(503):
            sim._note(world, world.characters[i % 4], "fixture", str(i))
        assert len(world.events) == 500
        first = world.pending_events[0]
        store._exec("CREATE TRIGGER fail_event BEFORE INSERT ON events "
                    "WHEN NEW.text = '502' BEGIN SELECT RAISE(ABORT, 'test fault'); END")
        assert not lab_state.command("step")["ok"]
        assert world.tick == 0 and len(world.pending_events) == 503
        assert not published and not store.events() and not store.load().characters
        store._exec("DROP TRIGGER fail_event")
        assert lab_state.command("step")["ok"]
        assert len(store.events(limit=1000)) == 503
        world.pending_events.append(first)  # retry of an already committed occurrence
        assert lab_state.command("save")["ok"]
        assert len(store.events(limit=1000)) == 503
        assert lab_state.catch_up(100)["ok"]
        rows = store.events(limit=1000)
        assert len(rows) > 503
        keys = {row["event_key"] for row in rows}
        assert len(keys) == len(rows)
        assert {e["event_key"] for k, e in published if k == "world_event"} == keys
        assert sum(k == "world_event" for k, e in published) == len(rows)
        assert lab_state.command("load")["ok"]
        assert lab_state.world_snapshot()["paused"]
        assert lab_state.world_snapshot()["last_events"] == list(reversed(rows[:40]))
        memory = lab_memory.LabMemory(tmp_path / "memory.db", store.path)
        today = datetime.now(timezone.utc).date().isoformat()
        journal = memory.journal_recap(today, char_id="friday")["events"]
        assert journal and all(row["actor"] == "friday" for row in journal)
        assert {row["id"] for row in journal} <= {row["id"] for row in rows}
        assert len(store.events(limit=1000)) == len(rows)
    finally:
        store.close()


def test_map_uses_all_live_residents_and_real_interaction_cells():
    world = sim.new_world()
    world.characters.append(sim.Character("fixture", "Fixture", x=15, y=6))
    payload = lab_map.snapshot(live=sim.snapshot(world))
    assert len(payload["characters"]) == 5
    seats = {(o["x"], o["y"]) for o in payload["objects"]
             if o["id"].startswith("kitchen-seat-")}
    kitchen = sim.LOC_BY_ID["kitchen"]
    assert seats == set(sim.interaction_cells(world, kitchen))
    assert len(seats) == kitchen.capacity and seats <= world.walkable
    assert all((x, y) not in world.walkable
               for fixtures in sim.ROOM_FIXTURES.values() for x, y, _ in fixtures)
