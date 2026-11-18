"""FRIDAY Future Lab — living world + agent roster (ROUND ZB-WORLD).

Three layers, deliberately separated:

* `sim`     — procedural tick: movement, pathfinding, occupancy, needs.
              **Zero model calls per tick** by default; the optional `planner`
              hook is only reachable from dialogue/reflection events and only
              while an explicit model budget remains.
* `store`   — SQLite persistence: world state, append-only event ledger,
              checkpoints and replay.
* `roster`  — real agent/task lifecycle: assignment, bounded subtask requests,
              review/consolidation, budget and error tracking, and the rule that
              resting never silently drops a running task.

Fictional character needs/memory live ONLY here. They never touch FRIDAY's
personal memory (`~/.friday`), model quotas or provider health — those remain
in `api_pools` / `gemini_tts` / `friday_tasks.db`.
"""

from .sim import (
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
from .store import Store
from .roster import Roster

__all__ = [
    "NEEDS",
    "MIN_DWELL_TICKS",
    "TRIGGER_URGENCY",
    "EXIT_URGENCY",
    "Location",
    "Character",
    "World",
    "bar_value",
    "astar",
    "new_world",
    "tick",
    "snapshot",
    "Store",
    "Roster",
]
