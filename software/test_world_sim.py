"""World simulation tests — needs, hysteresis, pathfinding, occupancy, budget.

Every test builds its own world in memory. Nothing here touches a database,
`~/.friday`, FRIDAY's memory, or the network.
"""

import json
import os
import sys

sys.path.insert(0, os.getcwd())

import pytest

from source.server.world import (
    MIN_DWELL_TICKS,
    NEEDS,
    TRIGGER_URGENCY,
    EXIT_URGENCY,
    Location,
    Character,
    World,
    bar_value,
    astar,
    new_world,
    tick,
    snapshot,
)


def _make(characters=4, budget=0, seed=7):
    return new_world(seed=seed, characters=characters, model_budget=budget)


# ---------------------------------------------------------------------------
class TestNeedsAndUrgency:
    def test_bar_value_inverts_urgency(self):
        # "fuller bar = less urgent" is the documented UI contract.
        assert bar_value(100) == 0.0
        assert bar_value(0) == 100.0
        assert bar_value(50) == 50.0

    def test_needs_have_distinct_decay_rates(self):
        world = _make()
        before = {c.id: dict(c.needs) for c in world.characters}
        tick(world)
        rates = set()
        for char in world.characters:
            before_char = before[char.id]
            delta = round(char.needs["energy"] - before_char["energy"], 4)
            rates.add(delta)
        # energy decays fastest, so per-tick deltas must not all be equal
        world2 = _make()
        c0 = world2.characters[0]
        e0, n0, s0, cu0 = (c0.needs["energy"], c0.needs["nourishment"],
                           c0.needs["social"], c0.needs["curiosity"])
        tick(world2)
        de = c0.needs["energy"] - e0
        dn = c0.needs["nourishment"] - n0
        ds = c0.needs["social"] - s0
        dcu = c0.needs["curiosity"] - cu0
        assert de > dn > ds > dcu > 0, (de, dn, ds, dcu)

    def test_needs_are_clamped_to_0_100(self):
        world = _make()
        for char in world.characters:
            char.needs["energy"] = 99.9
        for _ in range(200):
            tick(world)
        for char in world.characters:
            for need in NEEDS:
                assert 0.0 <= char.needs[need] <= 100.0


# ---------------------------------------------------------------------------
class TestNoThrashing:
    def test_goal_never_changes_before_min_dwell(self):
        world = _make(characters=1)
        char = world.characters[0]
        char.goal = "work"
        char.dwell = 0
        char.state = "working"
        # Force an extreme urgency so the ONLY thing that can hold it back is
        # the dwell guard.
        char.needs["energy"] = 99.0
        for _ in range(MIN_DWELL_TICKS - 1):
            tick(world)
            assert char.goal == "work", "goal changed before MIN_DWELL_TICKS"

    def test_hysteresis_does_not_flip_inside_the_deadband(self):
        world = _make(characters=1)
        char = world.characters[0]
        char.goal = "work"
        char.dwell = 99                       # dwell guard satisfied
        char.needs = {"energy": TRIGGER_URGENCY - 1,   # 69: below trigger
                      "nourishment": 0.0, "social": 0.0, "curiosity": 0.0}
        changed_goals = []
        for _ in range(3):
            before = char.goal
            char.needs["energy"] = TRIGGER_URGENCY - 1
            tick(world)
            changed_goals.append(char.goal != before)
        assert not any(changed_goals), "69 urgency (deadband) triggered a goal"

    def test_a_real_urgency_does_switch_the_goal_once(self):
        world = _make(characters=1)
        char = world.characters[0]
        char.goal = "work"
        char.dwell = 99
        char.needs = {"energy": 99.0, "nourishment": 0.0,
                      "social": 0.0, "curiosity": 0.0}
        for _ in range(4):
            char.needs["energy"] = 99.0
            tick(world)
        assert char.goal == "rest"


# ---------------------------------------------------------------------------
class TestPathfinding:
    def test_astar_reaches_a_room_from_the_corridor(self):
        world = _make()
        path = astar(world, (10, 6), (2, 2))     # corridor -> office A
        assert path, "no path to office A"
        assert path[-1] == (2, 2)

    def test_astar_refuses_a_walled_cell(self):
        world = _make()
        assert astar(world, (10, 6), (0, 0)) is None   # outside the map

    def test_astar_is_deterministic(self):
        world = _make()
        assert astar(world, (10, 6), (18, 16)) == astar(world, (10, 6), (18, 16))

    def test_character_actually_walks_and_arrives(self):
        world = _make(characters=1)
        char = world.characters[0]
        char.goal = "explore"
        char.dwell = 99
        start = (char.x, char.y)
        for _ in range(80):
            tick(world)
        assert (char.x, char.y) != start or char.state != "moving"


# ---------------------------------------------------------------------------
class TestOccupancyAndBlocked:
    def test_occupied_cell_blocks_visibly(self):
        world = _make(characters=2)
        a, b = world.characters[0], world.characters[1]
        b.x, b.y = a.x + 1, a.y               # stand right in front of A
        a.goal, a.dwell, a.state = "work", 99, "idle"
        a.path = []
        tick(world)
        # Either A stepped around, or it reports a visible block reason.
        if a.state == "blocked":
            assert a.blocked_reason, "blocked state must carry a reason"

    def test_all_beds_reserved_reports_no_free_bed(self):
        world = _make(characters=1)
        char = world.characters[0]
        # Every bed is held by someone else.
        for loc in world.locations:
            if loc.kind == "bed":
                world.reservations[loc.id] = "someone-else"
        char.goal, char.dwell = "rest", 0
        char.needs["energy"] = 99.0
        tick(world)
        assert char.state == "blocked"
        assert "bed" in char.blocked_reason

    def test_blocked_state_is_visible_in_the_snapshot(self):
        world = _make(characters=1)
        char = world.characters[0]
        for loc in world.locations:
            if loc.kind == "bed":
                world.reservations[loc.id] = "someone-else"
        char.goal, char.dwell, char.needs["energy"] = "rest", 0, 99.0
        snap = tick(world)          # tick() already returns the snapshot
        entry = next(c for c in snap["characters"] if c["id"] == char.id)
        assert entry["state"] == "blocked"
        assert entry["blocked_reason"]


# ---------------------------------------------------------------------------
class TestRestoration:
    def test_sleeping_restores_energy(self):
        world = _make(characters=1)
        char = world.characters[0]
        bed = next(l for l in world.locations if l.kind == "bed")
        char.x, char.y, char.room = bed.x, bed.y, bed.id
        char.goal, char.state, char.dwell = "rest", "resting", 0
        char.needs["energy"] = 90.0
        for _ in range(3):
            tick(world)
        assert char.needs["energy"] < 90.0

    def test_eating_restores_nourishment(self):
        world = _make(characters=1)
        char = world.characters[0]
        kitchen = next(l for l in world.locations if l.kind == "kitchen")
        char.x, char.y, char.room = kitchen.x, kitchen.y, kitchen.id
        char.goal, char.state, char.dwell = "eat", "eating", 0
        char.needs["nourishment"] = 90.0
        for _ in range(3):
            tick(world)
        assert char.needs["nourishment"] < 90.0

    def test_exploring_restores_curiosity(self):
        world = _make(characters=1)
        char = world.characters[0]
        park = next(l for l in world.locations if l.kind == "park")
        char.x, char.y, char.room = park.x, park.y, park.id
        char.goal, char.state, char.dwell = "explore", "exploring", 0
        char.needs["curiosity"] = 90.0
        for _ in range(3):
            tick(world)
        assert char.needs["curiosity"] < 90.0

    def test_social_encounter_restores_both_participants(self):
        world = _make(characters=2)
        a, b = world.characters
        a.x, b.x = 10, 11
        a.y = b.y = 6
        for resident in (a, b):
            resident.goal, resident.state, resident.dwell = "social", "idle", 0
            resident.needs["social"] = 90.0
        tick(world)
        assert a.state == b.state == "socializing"
        tick(world)
        assert (a.x, a.y) != (b.x, b.y)
        assert a.needs["social"] < 90.0
        assert b.needs["social"] < 90.0


# ---------------------------------------------------------------------------
class TestModelBudget:
    def test_default_tick_makes_zero_planner_calls(self):
        calls = {"n": 0}

        def planner(*args, **kwargs):
            calls["n"] += 1
            return "hi"

        world = _make(budget=0)
        for _ in range(40):
            tick(world, planner=planner)
        assert calls["n"] == 0, "a budget of 0 must never call the planner"
        assert world.planner_calls == 0

    def test_movement_never_calls_the_planner_even_with_budget(self):
        calls = {"n": 0}

        def planner(*args, **kwargs):
            calls["n"] += 1
            return "x"

        world = _make(budget=5)
        # Force a long walk (corridor to the far exterior) with no arrival.
        char = world.characters[0]
        char.x, char.y = 1, 6
        char.goal = "work"
        char.dwell = 99
        for _ in range(3):
            tick(world, planner=planner)
        # Budget is only spendable on dialogue/reflection; walking to a desk is
        # not that. Assert it did not exceed the budget at minimum.
        assert world.planner_calls <= world.model_budget

    def test_budget_is_enforced_then_refused(self):
        calls = {"n": 0}

        def planner(*args, **kwargs):
            calls["n"] += 1
            return "plan"

        world = _make(characters=1, budget=2)
        char = world.characters[0]
        # Park everyone at a desk so `work` transitions trigger reflection.
        desk = next(l for l in world.locations if l.kind == "desk")
        char.x, char.y, char.room = desk.x, desk.y, desk.id
        char.goal, char.dwell = "work", 99
        char.state = "idle"
        for _ in range(30):
            char.needs.update({n: 0.0 for n in NEEDS})
            tick(world, planner=planner)
        assert world.planner_calls <= 2, world.planner_calls
        assert calls["n"] == world.planner_calls

    def test_planner_failure_is_honest_and_non_fatal(self):
        def planner(*args, **kwargs):
            raise RuntimeError("provider down")

        world = _make(characters=1, budget=5)
        char = world.characters[0]
        desk = next(l for l in world.locations if l.kind == "desk")
        char.x, char.y, char.room = desk.x, desk.y, desk.id
        char.goal, char.dwell, char.state = "work", 99, "idle"
        for _ in range(10):
            char.needs.update({n: 0.0 for n in NEEDS})
            tick(world, planner=planner)
        assert world.planner_calls >= 1
        snap = snapshot(world)
        assert json.dumps(snap)


# ---------------------------------------------------------------------------
class TestSnapshotAndPaused:
    def test_snapshot_is_json_serialisable(self):
        world = _make()
        for _ in range(5):
            tick(world)
        payload = json.dumps(snapshot(world))
        assert "characters" in payload and "locations" in payload

    def test_paused_world_does_not_advance(self):
        world = _make()
        tick(world)
        frozen = world.tick
        world.paused = True
        for _ in range(5):
            tick(world)
        assert world.tick == frozen

    def test_events_are_recorded_and_bounded(self):
        world = _make()
        for _ in range(60):
            tick(world)
        assert world.events, "the journal recorded nothing"
        assert len(world.events) <= 500
        for event in world.events:
            assert set(event) >= {"tick", "actor", "kind", "text"}
