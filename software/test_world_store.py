"""Persistence tests: save/load round-trip, checkpoints, ledger, replay."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

import pytest

from source.server.world import Store, new_world, tick, snapshot
from source.server.world.sim import Character


@pytest.fixture()
def tmp_store():
    directory = tempfile.mkdtemp(prefix="friday-world-")
    store = Store(os.path.join(directory, "world.db"))
    yield store
    store.close()


def test_save_load_round_trip_is_exact(tmp_store):
    world = new_world(characters=3, seed=11)
    for _ in range(7):
        tick(world)
    before = snapshot(world)

    other = Store(tmp_store.path)
    assert other.save(world)["ok"] is True
    loaded = other.load()
    after = snapshot(loaded)
    other.close()

    for key in ("clock", "tick", "speed", "paused", "seed"):
        assert before[key] == after[key], key
    assert before["stats"] == after["stats"]
    assert [c["id"] for c in before["characters"]] == [c["id"] for c in after["characters"]]
    for original, restored in zip(before["characters"], after["characters"]):
        assert original["needs"] == restored["needs"], original["id"]
        assert original["memory"] == restored["memory"], original["id"]
        assert original["room"] == restored["room"]


def test_load_into_a_fresh_instance_recovers_after_a_crash(tmp_store):
    world = new_world(characters=2)
    for _ in range(4):
        tick(world)
    tmp_store.save(world)
    tmp_store.close()

    revived = Store(tmp_store.path)          # "restart"
    loaded = revived.load()
    assert loaded.tick == world.tick
    assert loaded.clock == world.clock
    assert len(loaded.characters) == 2
    revived.close()


def test_empty_store_returns_a_usable_world(tmp_store):
    loaded = tmp_store.load()
    assert loaded.tick == 0
    assert isinstance(loaded.characters, list)
    assert json.dumps(snapshot(loaded))


def test_reservations_survive_reload(tmp_store):
    world = new_world(characters=1)
    bed = next(loc for loc in world.locations if loc.kind == "bed")
    world.reservations[bed.id] = world.characters[0].id
    tmp_store.save(world)
    loaded = tmp_store.load()
    assert loaded.reservations.get(bed.id) == world.characters[0].id


def test_event_ledger_appends_and_orders(tmp_store):
    for tick_no in (1, 2, 3):
        tmp_store.append_event(tick_no, "friday", "routine", "step %d" % tick_no)
    rows = tmp_store.events(limit=10)
    assert len(rows) == 3
    assert [row["tick"] for row in rows] == [3, 2, 1]      # newest first
    assert rows[0]["text"] == "step 3"


def test_checkpoint_and_replay(tmp_store):
    world = new_world(characters=1)
    for _ in range(3):
        tick(world)
    tmp_store.append_event(1, "friday", "routine", "alpha")
    tmp_store.append_event(3, "vision", "blocked", "beta")
    result = tmp_store.checkpoint(world)
    assert result["ok"] is True

    checkpoints = tmp_store.list_checkpoints()
    assert checkpoints and checkpoints[0]["tick"] == world.tick
    payload = tmp_store.checkpoint_snapshot(world.tick)
    assert payload and payload["tick"] == world.tick

    replay = tmp_store.replay(from_tick=0)
    assert [row["text"] for row in replay] == ["alpha", "beta"]
    later = tmp_store.replay(from_tick=3)
    assert [row["text"] for row in later] == ["beta"]


def test_load_ignores_unknown_characters_and_never_raises(tmp_store):
    tmp_store._exec("INSERT INTO characters (id,name,persona,x,y,room,state,"
                    "needs_json,goal,task_ref,blocked_reason,memory_json,dwell)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("x", "Broken", "", 1, 1, "hall", "idle",
                     "not json", "work", None, "", "[]", 0))
    loaded = tmp_store.load()          # corrupt row must not blow up
    assert isinstance(loaded.characters, list)
    for char in loaded.characters:
        for need in ("energy", "nourishment", "social", "curiosity"):
            assert need in char.needs


def test_store_never_touches_friday_home(tmp_store):
    """The production default lives under ~/.friday; tests must not use it."""
    assert str(tmp_store.path).startswith(tempfile.gettempdir()) or \
        "friday-world-" in str(tmp_store.path)


# ---------------------------------------------------------------------------
# Bounded retention for the LAB's own journal (never personal memory).
# ---------------------------------------------------------------------------
class TestBoundedRetention:
    def test_prune_keeps_the_newest_events(self, tmp_store):
        for tick_no in range(1, 401):
            tmp_store.append_event(tick_no, "char", "routine", "beat %d" % tick_no)
        removed = tmp_store.prune_events(keep=100)
        assert removed == 300
        rows = tmp_store.events(limit=1000)
        assert len(rows) == 100
        # Newest survive: the oldest row we kept must be beat 301.
        assert rows[0]["text"] == "beat 400"
        assert rows[-1]["text"] == "beat 301"

    def test_prune_is_a_noop_below_the_limit(self, tmp_store):
        for tick_no in range(1, 11):
            tmp_store.append_event(tick_no, "char", "routine", "x")
        assert tmp_store.prune_events(keep=100) == 0
        assert len(tmp_store.events(limit=1000)) == 10

    def test_retention_is_bounded_over_many_writes(self, tmp_store):
        for tick_no in range(1, 3001):
            tmp_store.append_event(tick_no, "char", "routine", "b%d" % tick_no)
        tmp_store.prune_events(keep=500)
        assert len(tmp_store.events(limit=10000)) == 500

    def test_checkpoint_retention_keeps_the_latest(self, tmp_store):
        from source.server.world import new_world
        for tick_no in (1, 2, 3, 4, 5):
            world = new_world()
            world.tick = tick_no
            tmp_store.checkpoint(world)
        removed = tmp_store.prune_checkpoints(keep=2)
        assert removed == 3
        assert [c["tick"] for c in tmp_store.list_checkpoints()] == [5, 4]

    def test_checkpoint_runs_maintenance_for_us(self, tmp_store):
        from source.server.world import new_world
        for tick_no in range(1, 51):
            tmp_store.append_event(tick_no, "char", "routine", "b")
        world = new_world()
        world.tick = 999
        result = tmp_store.checkpoint(world)
        assert result["ok"] is True
        assert "pruned" in result
        assert set(result["pruned"]) >= {"events_pruned", "checkpoints_pruned"}

    def test_maintenance_scope_is_limited_to_lab_tables(self, tmp_store):
        """It must never open, read or write FRIDAY's personal stores."""
        result = tmp_store.maintenance()
        assert set(result) >= {"events_pruned", "checkpoints_pruned",
                               "event_retention", "checkpoint_retention"}
        # The only tables this module owns.
        scope = str(tmp_store.path)
        assert "friday_memory" not in scope
        assert "friday_tasks" not in scope
        assert scope.endswith("world.db")

    def test_maintenance_never_raises_on_a_closed_store(self, tmp_store):
        tmp_store.close()
        assert isinstance(tmp_store.maintenance(), dict)
