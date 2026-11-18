"""Procedural world tick â€” movement, pathfinding, occupancy, needs.

DESIGN CONTRACT
---------------
* **Zero model calls per tick.** Movement, decay and goal selection are pure
  Python. The optional `planner` callback is reachable ONLY from
  dialogue/reflection events and ONLY while `world.model_budget` has room, so a
  configured-out install (budget 0, the default) never spends a call.
* **Urgency semantics**: needs are 0..100 where *higher = more urgent*.
  `bar_value()` inverts it for UI bars ("fuller = better"), which is how
  open-sims presents the same idea.
* **No goal thrash.** Two guards: a minimum dwell (`MIN_DWELL_TICKS`) before any
  goal change, and hysteresis (trigger at `TRIGGER_URGENCY`, release only below
  `EXIT_URGENCY`). Without both, two needs near the same threshold flip the
  character every tick.
* **Unavailable destinations are visible.** A refused path or a fully reserved
  bed sets `state="blocked"` with a `blocked_reason` â€” never a silent no-op.
"""

from __future__ import annotations

import random
from heapq import heappop, heappush
from datetime import datetime, timezone
from uuid import uuid4
from collections import deque
from dataclasses import dataclass, field, asdict

NEEDS = ("energy", "nourishment", "social", "curiosity")

# Urgency thresholds (higher = more urgent).
TRIGGER_URGENCY = 70.0
EXIT_URGENCY = 55.0
MIN_DWELL_TICKS = 3
BLOCK_LIMIT = 12
TRAVEL_LIMIT = 64
RETRY_DELAY = 24
BLOCK_NOTICE_INTERVAL = 60
MAX_ACTIVITY_TICKS = 16

# Urgency gained per in-world second while performing the restoring activity.
RESTORE_RATE = {
    "resting": {"energy": 6.0},
    "eating": {"nourishment": 5.0},
    "socializing": {"social": 4.0},
    "exploring": {"curiosity": 3.0},
}

# Urgency accumulated per in-world second (distinct rates -> distinct rhythms).
DECAY_PER_SECOND = {
    "energy": 0.06,
    "nourishment": 0.04,
    "social": 0.03,
    "curiosity": 0.02,
}

GOAL_NEED = {
    "rest": "energy",
    "eat": "nourishment",
    "social": "social",
    "explore": "curiosity",
}
NEED_GOAL = {v: k for k, v in GOAL_NEED.items()}


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
@dataclass
class Location:
    id: str
    name: str
    kind: str          # office|desk|collaboration|library|kitchen|bedroom|bed
                        # |park|path|street|car|corridor|hall
    x: int
    y: int
    capacity: int = 1
    # Exact bounding box [x0, y0, x1, y1] for room-shaped locations. Added
    # 2026-11-04 so a renderer can draw REAL rooms instead of guessing extents
    # from a centre point. None for point locations (desks, beds, doors).
    rect: object = None

    def to_dict(self):
        data = asdict(self)
        if data.get("rect") is None:
            data.pop("rect", None)
        return data


def _rect(cells, x0, y0, x1, y1):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            cells.add((x, y))


def build_layout():
    """A coherent building plus exterior, as (walkable cells, locations).

    Building interior x1..20 / y1..11 with a corridor cross; rooms separated by
    wall rows/columns except where they meet the corridor (that meeting cell is
    the door). Exterior begins at y13 and joins through the front door (10,12).
    """
    walkable = set()

    # Corridors (the doors of the building).
    _rect(walkable, 1, 6, 20, 6)      # horizontal spine
    _rect(walkable, 10, 1, 10, 12)    # vertical spine + front door

    rooms = [
        ("office-a", "Office A", "office", 1, 1, 5, 5),
        ("office-b", "Office B", "office", 6, 1, 9, 5),
        ("collab", "Collaboration room", "collaboration", 11, 1, 15, 5),
        ("library", "Library", "library", 16, 1, 20, 5),
        ("kitchen", "Kitchen / cafe", "kitchen", 1, 7, 5, 11),
        ("bedroom", "Bedrooms", "bedroom", 6, 7, 9, 11),
        ("office-c", "Studio", "office", 11, 7, 15, 11),
        ("hall", "Hall", "hall", 16, 7, 20, 11),
    ]
    locations = []
    for lid, name, kind, x0, y0, x1, y1 in rooms:
        _rect(walkable, x0, y0, x1, y1)
        locations.append(Location(lid, name, kind, (x0 + x1) // 2, (y0 + y1) // 2,
                                  capacity=8 if kind in ("office", "collaboration") else 4,
                                  rect=(x0, y0, x1, y1)))

    # Desks inside the offices (single occupancy targets).
    for idx, (dx, dy) in enumerate([(2, 2), (3, 2), (7, 2), (8, 2), (12, 2), (13, 2),
                                    (12, 9), (13, 9)]):
        locations.append(Location("desk-%d" % idx, "Desk %d" % (idx + 1), "desk",
                                  dx, dy, capacity=1))

    # Beds: single occupancy.
    for idx, (bx, by) in enumerate([(7, 9), (8, 9), (7, 10), (8, 10)]):
        locations.append(Location("bed-%d" % idx, "Bed %d" % (idx + 1), "bed",
                                  bx, by, capacity=1))

    # Exterior: front door -> path -> park / street / cars.
    walkable.add((10, 12))
    _rect(walkable, 10, 13, 10, 17)            # north-south path
    _rect(walkable, 2, 14, 20, 14)             # east-west street-side path
    _rect(walkable, 2, 15, 8, 17)              # park lawn
    _rect(walkable, 11, 15, 20, 16)            # street
    locations.extend([
        Location("front", "Front door", "path", 10, 12, capacity=4),
        Location("park", "Park", "park", 5, 16, capacity=8, rect=(2, 15, 8, 17)),
        Location("street", "Street", "street", 15, 15, capacity=8,
                 rect=(11, 15, 20, 16)),
        Location("car-1", "Parked car", "car", 12, 16, capacity=1),
        Location("car-2", "Parked car", "car", 18, 16, capacity=1),
        Location("path", "Garden path", "path", 10, 14, capacity=6),
    ])
    return walkable, locations


# These solid decorative objects share coordinates with the renderer.
ROOM_FIXTURES = {
    "office-a": [(1, 1, "bookshelf"), (2, 1, "cabinet")],
    "office-b": [(6, 1, "bookshelf"), (7, 1, "plant")],
    "collab": [(12, 1, "big_table"), (13, 1, "whiteboard")],
    "library": [(16, 1, "bookshelf_row"), (17, 1, "archive_box")],
    "kitchen": [(1, 11, "counter"), (2, 11, "stove"), (3, 11, "kitchen_table"),
                (4, 11, "fridge"), (5, 11, "sink")],
    "bedroom": [(7, 11, "dresser"), (8, 11, "lamp")],
    "office-c": [(12, 11, "cabinet"), (13, 11, "plant")],
    "hall": [(17, 11, "bench"), (18, 11, "plant")],
}

WALKABLE, LOCATIONS = build_layout()
WALKABLE.difference_update((x, y) for room in ROOM_FIXTURES.values() for x, y, _ in room)
WALKABLE.difference_update((loc.x, loc.y) for loc in LOCATIONS if loc.kind == "car")

LOC_BY_ID = {loc.id: loc for loc in LOCATIONS}


def loc_at(x, y):
    for loc in LOCATIONS:
        if loc.x == x and loc.y == y:
            return loc
    return None


# ---------------------------------------------------------------------------
# Characters / world
# ---------------------------------------------------------------------------
@dataclass
class Character:
    id: str
    name: str
    persona: str = ""
    x: int = 10
    y: int = 6
    room: str = "hall"
    state: str = "idle"
    needs: dict = field(default_factory=lambda: {n: 20.0 for n in NEEDS})
    goal: str = "work"
    path: list = field(default_factory=list)
    memory: list = field(default_factory=list)
    task_ref: str | None = None
    blocked_reason: str = ""
    dwell: int = 0
    activity_ticks: int = 0
    blocked_ticks: int = 0
    retry_after: dict = field(default_factory=dict)
    block_notice_after: dict = field(default_factory=dict)
    social_partner: str | None = None
    work_finished: bool = False

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "persona": self.persona,
            "x": self.x, "y": self.y, "room": self.room, "state": self.state,
            "goal": self.goal, "task_ref": self.task_ref,
            "blocked_reason": self.blocked_reason, "dwell": self.dwell,
            "needs": {k: round(float(v), 2) for k, v in self.needs.items()},
            "bar": {k: round(bar_value(v), 2) for k, v in self.needs.items()},
            "memory": list(self.memory[-12:]),
            "path_left": len(self.path),
        }


@dataclass
class World:
    clock: int = 0                    # in-world seconds
    tick: int = 0
    speed: float = 1.0
    paused: bool = False
    characters: list = field(default_factory=list)
    locations: list = field(default_factory=lambda: list(LOCATIONS))
    walkable: set = field(default_factory=lambda: set(WALKABLE))
    reservations: dict = field(default_factory=dict)   # loc_id -> char_id
    events: deque = field(default_factory=lambda: deque(maxlen=500))
    pending_events: list = field(default_factory=list)
    planner_calls: int = 0
    model_budget: int = 0             # 0 == never call a model (default)
    seed: int = 7
    steps_per_tick: int = 10          # in-world seconds per tick
    identity_links: list = field(default_factory=list)  # verified installation provenance, no routing policy

    def rng(self):
        return random.Random(self.seed + self.tick)


def bar_value(urgency):
    """UI bar value: urgency 100 (desperate) -> bar 0 (empty)."""
    return max(0.0, min(100.0, 100.0 - float(urgency)))


def new_world(seed: int = 7, characters: int = 4, model_budget: int = 0) -> World:
    """A fresh world with a few personas already placed on the corridor."""
    defaults = [
        ("friday", "FRIDAY", "Coordinator: routes the goal and reports back."),
        ("research", "Research", "Researcher: compares sources, sends evidence."),
        ("code", "Code", "Builder: drafts bounded proposals."),
        ("vision", "Vision", "Reviewer: checks results against evidence."),
    ]
    chars = []
    spots = [(10, 6), (9, 6), (11, 6), (12, 6)]
    for i in range(min(characters, len(defaults))):
        cid, name, persona = defaults[i]
        x, y = spots[i % len(spots)]
        chars.append(Character(id=cid, name=name, persona=persona, x=x, y=y,
                               room="hall", needs={n: 15.0 + 5 * i for n in NEEDS},
                               memory=["woke up on the corridor"]))
    return World(characters=chars, seed=seed, model_budget=int(model_budget))


# ---------------------------------------------------------------------------
# Pathfinding
# ---------------------------------------------------------------------------
def astar(world, start, goal, avoid=()):
    """4-neighbour A* over walkable cells. Returns [(x,y)...] or None."""
    if goal not in world.walkable or goal in avoid:
        return None
    if start == goal:
        return [start]

    def h(cell):
        return abs(cell[0] - goal[0]) + abs(cell[1] - goal[1])

    frontier = [(h(start), h(start), start)]
    came = {}
    g = {start: 0}
    closed = set()
    while frontier:
        _, _, current = heappop(frontier)
        if current in closed:
            continue
        if current == goal:
            path = [current]
            while current in came:
                current = came[current]
                path.append(current)
            path.reverse()
            return path[1:]
        closed.add(current)
        for nx, ny in ((current[0] + 1, current[1]), (current[0] - 1, current[1]),
                       (current[0], current[1] + 1), (current[0], current[1] - 1)):
            neighbour = (nx, ny)
            if neighbour not in world.walkable or neighbour in closed or neighbour in avoid:
                continue
            tentative = g[current] + 1
            if tentative < g.get(neighbour, 1 << 30):
                came[neighbour] = current
                g[neighbour] = tentative
                estimate = h(neighbour)
                heappush(frontier, (tentative + estimate, estimate, neighbour))
    return None


# ---------------------------------------------------------------------------
# Targeting helpers
# ---------------------------------------------------------------------------
def _distance(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _cell(char):
    return char.x, char.y


def _neighbors(cell):
    x, y = cell
    return ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))


def interaction_cells(world, loc):
    """A room has distinct usable positions, never a shared target tile."""
    center = (loc.x, loc.y)
    if not loc.rect or loc.kind not in {"kitchen", "park", "street", "hall", "library"}:
        return [center] if center in world.walkable else []
    x0, y0, x1, y1 = loc.rect
    points = {(p.x, p.y) for p in world.locations if p.id != loc.id}
    cells = [cell for cell in world.walkable
             if x0 <= cell[0] <= x1 and y0 <= cell[1] <= y1
             and cell not in points]
    return sorted(cells, key=lambda c: (_distance(center, c), c))[:loc.capacity]


def _destinations(world, char, *, goal=None):
    kinds = {"rest": {"bed"}, "eat": {"kitchen"}, "explore": {"park", "street"},
             "work": {"desk", "hall", "library"}}
    for loc in world.locations:
        if loc.kind in kinds[goal or char.goal]:
            for cell in interaction_cells(world, loc):
                key = loc.id if not loc.rect else "%s@%d,%d" % (loc.id, *cell)
                yield key, loc, cell


def _note(world, char, kind, text):
    event = {"tick": world.tick, "actor": char.id, "kind": kind, "text": text,
             "event_key": uuid4().hex, "created_at": datetime.now(timezone.utc).timestamp()}
    world.events.append(event)
    world.pending_events.append(event)


def _release(world, char, keep=None):
    for key, holder in list(world.reservations.items()):
        if holder == char.id and key != keep:
            del world.reservations[key]


def _set_goal(world, char, goal):
    _release(world, char)
    char.goal = goal
    char.dwell = char.activity_ticks = char.blocked_ticks = 0
    char.work_finished = False
    char.social_partner = None
    char.path = []
    char.state = "idle"
    char.blocked_reason = ""
    if goal != "work":
        _note(world, char, "goal", "%s decides to %s" % (char.name, goal))


def _select_goal(world, char):
    options = [(need, val) for need, val in char.needs.items()
               if val >= TRIGGER_URGENCY and need in NEED_GOAL
               and world.tick >= char.retry_after.get(NEED_GOAL[need], 0)]
    if options:
        need, _ = max(options, key=lambda item: item[1])
        _set_goal(world, char, NEED_GOAL[need])
        return True
    return False


def _block(world, char, reason):
    char.state = "blocked"
    char.blocked_reason = reason
    char.blocked_ticks += 1
    char.path = []
    if (char.blocked_ticks >= MIN_DWELL_TICKS
            and world.tick >= char.block_notice_after.get(char.goal, 0)):
        char.block_notice_after[char.goal] = world.tick + BLOCK_NOTICE_INTERVAL
        _note(world, char, "blocked", "%s: %s" % (char.name, reason))
    if char.blocked_ticks >= BLOCK_LIMIT or char.dwell >= TRAVEL_LIMIT:
        _abandon(world, char, reason)


def _abandon(world, char, reason):
    goal = char.goal
    _note(world, char, "abandoned", "%s stops trying to %s: %s"
          % (char.name, goal, reason))
    char.retry_after[goal] = world.tick + RETRY_DELAY
    _set_goal(world, char, "work")
    _select_goal(world, char)


def _route(world, char, occupied):
    start = _cell(char)
    options = list(_destinations(world, char))
    options.sort(key=lambda item: (world.reservations.get(item[0]) != char.id,
                                  char.goal == "work" and item[1].kind != "desk",
                                  _distance(start, item[2]), item[0]))
    for key, loc, cell in options:
        if world.reservations.get(key) not in (None, char.id) or cell in occupied:
            continue
        path = astar(world, start, cell, occupied)
        if path is not None:
            _release(world, char, key)
            world.reservations[key] = char.id
            return loc, path
    return None, None


LIFE_ACTIONS = ("rest", "eat", "explore")


def offer_route_rest(world, char, claim):
    """Visual response to a failed route. Never bypass movement or interrupt tasks."""
    if (world.paused or char.goal != "work" or char.task_ref
            or world.tick < char.retry_after.get("rest", 0) or _cell(char) not in world.walkable):
        return False
    occupied = {_cell(other) for other in world.characters if other is not char}
    if not any(world.reservations.get(key) in (None, char.id)
               and astar(world, _cell(char), cell, occupied) is not None
               for key, _, cell in _destinations(world, char, goal="rest")) or not claim():
        return False
    _set_goal(world, char, "rest")
    return True


def apply_life_plan(world, char, action):
    """Accept a routine proposal only where the current simulator can run it."""
    if (world.paused or char.goal != "work" or action not in LIFE_ACTIONS
            or world.tick < char.retry_after.get(action, 0)
            or char.needs[GOAL_NEED[action]] < 25
            or max(char.needs.values()) >= TRIGGER_URGENCY
            or _cell(char) not in world.walkable):
        return False
    occupied = {_cell(other) for other in world.characters if other is not char}
    if not any(world.reservations.get(key) in (None, char.id)
               and astar(world, _cell(char), cell, occupied) is not None
               for key, _, cell in _destinations(world, char, goal=action)):
        return False
    _set_goal(world, char, action)
    _note(world, char, "life_plan", "%s accepted a background AI proposal to %s" % (char.name, action))
    return True


def _social_route(world, char, occupied):
    start = _cell(char)
    # Keep an available partner preferred, while allowing another reachable
    # approach when the original partner moves or is cut off.
    others = sorted((c for c in world.characters if c is not char),
                    key=lambda c: (c.id != char.social_partner,
                                   _distance(start, _cell(c)), c.id))
    for other in others:
        for cell in sorted(_neighbors(_cell(other)), key=lambda p: _distance(start, p)):
            path = astar(world, start, cell, occupied)
            if path is not None:
                char.social_partner = other.id
                return path
    return None


def _room_at(world, cell):
    for loc in world.locations:
        if loc.rect:
            x0, y0, x1, y1 = loc.rect
            if x0 <= cell[0] <= x1 and y0 <= cell[1] <= y1:
                return loc.id
    return "corridor"


def _finish(world, char):
    _note(world, char, "completed", "%s completes %s" % (char.name, char.goal))
    if char.goal == "work":
        char.work_finished = True
        return
    _set_goal(world, char, "work")
    _select_goal(world, char)


def _activity(world, char, seconds, social_recovered):
    if char.state == "socializing":
        partner = next((c for c in world.characters if c.id == char.social_partner), None)
        if partner is None or _distance(_cell(char), _cell(partner)) != 1:
            _abandon(world, char, "conversation partner moved away")
            return
        participants = (char, partner)
    else:
        participants = (char,)
    char.activity_ticks += 1
    for participant in participants:
        for need, rate in RESTORE_RATE[char.state].items():
            if need == "social":
                if participant.id in social_recovered:
                    continue
                social_recovered.add(participant.id)
            participant.needs[need] = max(0.0, participant.needs[need] - rate * seconds)
    if (char.activity_ticks >= MIN_DWELL_TICKS
            and char.needs[GOAL_NEED[char.goal]] <= EXIT_URGENCY):
        _finish(world, char)
    elif char.activity_ticks >= MAX_ACTIVITY_TICKS:
        _abandon(world, char, "activity time limit")


def tick(world, planner=None):
    """Advance one tick, with model calls only at budgeted activity starts."""
    if world.paused:
        return snapshot(world)
    world.tick += 1
    seconds = world.steps_per_tick * max(0.0, float(world.speed))
    world.clock += int(seconds)
    for char in world.characters:
        for need, rate in DECAY_PER_SECOND.items():
            char.needs[need] = min(100.0, char.needs[need] + rate * seconds)

    # Rotate first mover to distribute access to shared slots and passages.
    offset = world.tick % max(1, len(world.characters))
    order = world.characters[offset:] + world.characters[:offset]
    occupied = {_cell(c) for c in order}
    social_recovered = set()
    for char in order:
        char.dwell += 1
        if _cell(char) not in world.walkable:
            # A legacy save on newly solid furniture can walk off that cell.
            char.state = "idle"
        if char.state in RESTORE_RATE and char.goal in GOAL_NEED:
            _activity(world, char, seconds, social_recovered)
            continue
        if char.goal not in GOAL_NEED and char.dwell >= MIN_DWELL_TICKS:
            _select_goal(world, char)
        if char.goal == "work" and char.state == "working":
            char.activity_ticks += 1
            if char.activity_ticks >= MIN_DWELL_TICKS and not char.work_finished:
                _finish(world, char)
            continue
        if char.goal == "work" and world.tick < char.retry_after.get("work", 0):
            continue
        if char.goal != "work" and char.dwell > TRAVEL_LIMIT:
            _abandon(world, char, "destination not reached in time")
            continue
        start = _cell(char)
        others = occupied - {start}
        loc = None
        if char.goal == "social":
            path = _social_route(world, char, others)
        else:
            loc, path = _route(world, char, others)
        if path is None:
            destination = {"rest": "bed", "eat": "kitchen seat", "social": "conversation partner"}
            _block(world, char, "no reachable %s" % destination.get(char.goal, char.goal + " position"))
            continue
        char.path = path
        char.blocked_ticks = 0
        char.blocked_reason = ""
        if path[0] == start:
            _arrive(world, char, loc, planner, seconds)
            continue
        step = path[0]
        if _distance(start, step) != 1 or step not in world.walkable or step in others:
            _block(world, char, "unsafe next step")
            continue
        char.path.pop(0)
        occupied.remove(start)
        occupied.add(step)
        char.x, char.y = step
        char.room = _room_at(world, step)
        char.state = "moving"

    return snapshot(world)


def _arrive(world, char, loc, planner, seconds):
    char.path = []
    char.activity_ticks = 0
    char.work_finished = False
    if char.goal == "social":
        char.state = "socializing"
        partner = next(c for c in world.characters if c.id == char.social_partner)
        if partner.state != "socializing" or partner.social_partner != char.id:
            _note(world, char, "social", "%s meets %s" % (char.name, partner.name))
            _maybe_plan(world, char, "dialogue", planner)
        return
    char.room = loc.id
    char.state = {"rest": "resting", "eat": "eating", "explore": "exploring",
                  "work": "working"}[char.goal]
    _note(world, char, "routine", "%s starts %s at %s" % (char.name, char.goal, loc.name))
    if char.goal == "work":
        _maybe_plan(world, char, "reflection", planner)


def _maybe_plan(world, char, kind, planner):
    """The ONLY path to a model call. Budget-gated, never called for movement."""
    if planner is None:
        return
    if world.model_budget <= world.planner_calls:
        return
    world.planner_calls += 1
    try:
        result = planner(world, char, kind)
    except Exception as error:                     # honest failure, never fatal
        result = {"ok": False, "reason": type(error).__name__}
    if isinstance(result, str) and result.strip():
        _note(world, char, kind, result.strip()[:240])


def snapshot(world):
    """JSON-safe world snapshot for the UI/bridge."""
    occupied = {}
    for loc_id, char_id in world.reservations.items():
        occupied[loc_id] = char_id
    locations = []
    for loc in world.locations:
        item = loc.to_dict()
        holders = [holder for key, holder in occupied.items()
                   if key == loc.id or key.startswith(loc.id + "@")]
        item["occupied"] = holders[0] if holders else None
        item["occupants"] = holders
        if loc.rect and loc.kind in {"kitchen", "hall", "park", "street"}:
            item["interaction_cells"] = interaction_cells(world, loc)
        locations.append(item)
    return {
        "clock": int(world.clock),
        "tick": int(world.tick),
        "speed": float(world.speed),
        "paused": bool(world.paused),
        "seed": int(world.seed),
        "stats": {
            "ticks": int(world.tick),
            "planner_calls": int(world.planner_calls),
            "model_budget_left": int(max(0, world.model_budget - world.planner_calls)),
            "characters": len(world.characters),
        },
        "characters": [c.to_dict() for c in world.characters],
        "locations": locations,
        "last_events": [dict(e) for e in list(world.events)[-40:]],
    }
