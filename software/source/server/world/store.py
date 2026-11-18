"""SQLite persistence for the Future Lab world: state, ledger, checkpoints.

Everything the world needs to survive a restart lives here. `path` is always
caller-supplied for tests; the production default is under `~/.friday`, which
is FRIDAY's own state directory and is deliberately NEVER used by tests.

Schema
------
    meta        key/value (seed, clock, tick, speed, paused, model_budget, ...)
    characters  one row per character (needs/memory as JSON)
    reservations loc_id -> char_id (single-occupancy claims)
    events      append-only ledger, ordered by id
    checkpoints (tick, created_at, snapshot_json) for replay
    agents / roster_tasks  see roster.py (created here so one file owns the DDL)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .sim import Character, World, Location, LOCATIONS, WALKABLE, NEEDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY, name TEXT, persona TEXT,
    x INTEGER, y INTEGER, room TEXT, state TEXT,
    needs_json TEXT, goal TEXT, task_ref TEXT, blocked_reason TEXT,
    memory_json TEXT, dwell INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reservations (loc_id TEXT PRIMARY KEY, char_id TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tick INTEGER,
    actor TEXT, kind TEXT, text TEXT, event_key TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    tick INTEGER PRIMARY KEY, created_at REAL, snapshot_json TEXT
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT, role TEXT, model TEXT, state TEXT,
    task_id TEXT, permissions_json TEXT, budget_json TEXT,
    last_error TEXT, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS roster_tasks (
    id TEXT PRIMARY KEY, goal TEXT, owner TEXT, state TEXT, steps INTEGER,
    artifacts_json TEXT, error TEXT, approval_required INTEGER DEFAULT 0,
    paused_by TEXT, created_at REAL, updated_at REAL,
    category TEXT DEFAULT 'user_work'
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT, dst TEXT, kind TEXT,
    state TEXT, at REAL, message TEXT
);
"""


def default_path() -> Path:
    """Production location. Tests must never call this â€” they pass a path.

    `FRIDAY_WORLD_DB` is an optional override following the project's existing
    env-override convention (`FRIDAY_MEMORY_DB`, `FRIDAY_AUDIT_DIR`, ...). It
    exists so verification can point the LAB at an in-scope store instead of
    the production one, which lives OUTSIDE the working tree at
    `~/.friday/world.db`. The default is unchanged: production still writes
    exactly where it always did.
    """
    configured = os.environ.get("FRIDAY_WORLD_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".friday" / "world.db"


# Bounded retention for LAB data (the world journal), NOT personal memory.
# FRIDAY's personal memory lives in a different database and is never read or
# written here - pruning this file can only ever touch simulation rows.
EVENT_RETENTION = 5000
CHECKPOINT_RETENTION = 50


class Store:
    """Thread-safe SQLite facade. Every public method never raises."""

    def __init__(self, path=None, *, existing=False):
        self.path = Path(path) if path else default_path()
        self._lock = threading.RLock()
        if existing:
            self._conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True,
                                         check_same_thread=False, isolation_level=None, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # `isolation_level=None` = driver autocommit: an explicit BEGIN is how
        # multi-statement atomicity is expressed (see `transaction`). Under the
        # default implicit driver mode, python's own deferred transactions
        # fought every explicit BEGIN ("cannot start a transaction within a
        # transaction"), leaving half-failed saves stranded in an open
        # transaction - measured on 2026-11-05. `_exec` still ends with
        # `.commit()`, which is a harmless no-op in autocommit mode.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     isolation_level=None, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._migrate()

    def _migrate(self):
        """Additive column migration for pre-existing store tables.

        Uses `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` (same additive
        guard style as lab_memory). Never drops, never updates existing rows.
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(events)")}
        for name, sql_type in (("event_key", "TEXT"), ("created_at", "REAL")):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE events ADD COLUMN {name} {sql_type}")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS world_event_key ON events(event_key)")
        try:
            existing = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(roster_tasks)")}
            if "category" not in existing and "id" in existing:
                self._conn.execute(
                    "ALTER TABLE roster_tasks ADD COLUMN "
                    "category TEXT DEFAULT 'user_work'")
                self._conn.commit()
        except Exception:
            pass

    # -- helpers ------------------------------------------------------------
    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _exec_tx(self, sql, params=()):
        """Execute WITHOUT committing - for use inside `transaction()`.

        `_exec` commits after every statement, which would end an open
        BEGIN IMMEDIATE transaction early; `_exec_tx` runs the statement under
        the same lock but leaves the transaction boundary to `transaction()`.
        """
        with self._lock:
            return self._conn.execute(sql, params)

    @contextmanager
    def transaction(self):
        """Explicit multi-statement transaction. ATOMIC, unlike `_exec`.

        Every `_exec` call commits immediately, so a multi-statement rewrite
        (e.g. Roster's DELETE-then-INSERT) that used `_exec` per statement was
        NOT atomic: a crash between the DELETE and the INSERT left empty or
        partial tables. The connection runs in explicit-transaction mode
        (`isolation_level=None`), so this context issues one real
        `BEGIN IMMEDIATE`, commits on clean exit and ROLLS BACK on any
        exception: a failed rewrite can never destroy existing roster data.
        (A previous attempt relied on the driver's implicit deferred mode;
        `execute("BEGIN IMMEDIATE")` there collided with python's own
        deferred transaction and every subsequent save failed with
        "cannot start a transaction within a transaction".) If the connection
        is ALREADY inside a transaction (nested use), __enter__ is a no-op
        rather than an error.
        """
        with self._lock:
            if self._conn.in_transaction:
                yield self
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def in_transaction(self) -> bool:
        try:
            with self._lock:
                return bool(self._conn.in_transaction)
        except Exception:
            return False

    def meta_get(self, key: str, default: str = "") -> str:
        """Read one meta row as TEXT. Never raises (additive, LAB-owned keys
        like `lab_presence` and `task_serving` ride the same table)."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default
        except Exception:
            return default

    def meta_set(self, key: str, value: str) -> bool:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)",
                    (str(key), str(value)))
                self._conn.commit()
            return True
        except Exception:
            return False

    def close(self):
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    # -- world --------------------------------------------------------------
    def identity_links(self):
        """Read installed links only. Ordinary startup never creates their schema."""
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM sqlite_schema WHERE name='identity_resident_links'").fetchone():
                return []
            return [json.loads(row[0]) for row in self._conn.execute(
                "SELECT record FROM identity_resident_links ORDER BY namespace,resident_id")]

    def save(self, world: World) -> dict:
        """Persist the world ATOMICALLY (ROUND LAB-2A: one transaction).

        The old version wrote per-statement under the default driver, never
        atomic: a crash mid-rewrite could leave a character present in
        `reservations` but deleted from `characters`. Now the entire rewrite
        is one transaction with the same commit/rollback guarantee the roster
        tables already get in this round.
        """
        try:
            if self.in_transaction():
                return {"ok": False, "reason": "world save requires its own transaction"}
            pending = tuple(world.pending_events)
            inserted = []
            with self.transaction():
                inserted = self._save_world_tx(world, pending)
            del world.pending_events[:len(pending)]
            return {"ok": True, "characters": len(world.characters), "events": inserted}
        except Exception as error:
            return {"ok": False, "reason": type(error).__name__}

    def _save_world_tx(self, world, pending):
        """Shared world writer. Caller owns the transaction and post-commit publication."""
        if not self._conn.in_transaction:
            raise RuntimeError("world writer requires transaction")
        inserted = []
        self._exec_tx("DELETE FROM characters")
        self._exec_tx("DELETE FROM reservations")
        for char in world.characters:
            self._exec_tx(
                "INSERT INTO characters (id,name,persona,x,y,room,state,"
                "needs_json,goal,task_ref,blocked_reason,memory_json,dwell)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (char.id, char.name, char.persona, char.x, char.y, char.room,
                 char.state,
                 json.dumps({k: float(v) for k, v in char.needs.items()}),
                 char.goal, char.task_ref, char.blocked_reason,
                 json.dumps(list(char.memory)), int(char.dwell)),
            )
        for loc_id, char_id in world.reservations.items():
            self._exec_tx(
                "INSERT OR REPLACE INTO reservations (loc_id,char_id) VALUES (?,?)",
                (loc_id, char_id))
        meta = {
            "clock": str(world.clock), "tick": str(world.tick),
            "speed": str(world.speed), "paused": "1" if world.paused else "0",
            "seed": str(world.seed),
            "model_budget": str(world.model_budget),
            "planner_calls": str(world.planner_calls),
            "steps_per_tick": str(world.steps_per_tick),
            "layout": json.dumps({"walkable": sorted(world.walkable),
                                  "locations": [loc.to_dict() for loc in world.locations]}),
            "activity": json.dumps({
                c.id: {key: getattr(c, key) for key in (
                    "activity_ticks", "blocked_ticks", "retry_after", "block_notice_after",
                    "social_partner", "work_finished", "path")}
                for c in world.characters}),
        }
        for key, value in meta.items():
            self._exec_tx(
                "INSERT INTO meta (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
        for entry in pending:
            cursor = self._exec_tx(
                "INSERT INTO events (tick,actor,kind,text,event_key,created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING",
                (entry["tick"], entry["actor"], entry["kind"], entry["text"][:400],
                 entry["event_key"], entry["created_at"]))
            if cursor.rowcount:
                inserted.append(entry)
        return inserted

    def load(self, world: World = None) -> World:
        """Rebuild a world from disk. Returns a fresh world when empty."""
        world = world or World()
        try:
            with self._lock:
                meta = {row["key"]: row["value"]
                        for row in self._conn.execute("SELECT key,value FROM meta")}
                rows = list(self._conn.execute("SELECT * FROM characters"))
                reservations = list(self._conn.execute("SELECT * FROM reservations"))
                recent = self.events(limit=world.events.maxlen)
                installed_links = self.identity_links()
            world.clock = int(meta.get("clock", 0))
            world.tick = int(meta.get("tick", 0))
            world.speed = float(meta.get("speed", 1.0))
            world.paused = meta.get("paused", "0") == "1"
            world.seed = int(meta.get("seed", 7))
            world.model_budget = int(meta.get("model_budget", 0))
            world.planner_calls = int(meta.get("planner_calls", 0))
            world.steps_per_tick = int(meta.get("steps_per_tick", 10))
            layout = json.loads(meta.get("layout", "null"))
            if layout is not None:
                world.walkable = {tuple(cell) for cell in layout["walkable"]}
                world.locations = [Location(**{**row, "rect": tuple(row["rect"]) if row.get("rect") else None})
                                   for row in layout["locations"]]
            activity = json.loads(meta.get("activity", "{}"))
            for record in activity.values():
                if "path" in record:
                    record["path"] = [tuple(cell) for cell in record["path"]]
            world.identity_links = installed_links
            if rows:
                characters = []
                for row in rows:
                    needs = json.loads(row["needs_json"] or "{}")
                    for need in NEEDS:
                        needs.setdefault(need, 20.0)
                    characters.append(Character(
                        id=row["id"], name=row["name"], persona=row["persona"] or "",
                        x=row["x"], y=row["y"], room=row["room"] or "hall",
                        state=row["state"] or "idle",
                        needs={k: float(v) for k, v in needs.items()},
                        goal=row["goal"] or "work",
                        memory=json.loads(row["memory_json"] or "[]"),
                        task_ref=row["task_ref"],
                        blocked_reason=row["blocked_reason"] or "",
                        dwell=int(row["dwell"] or 0),
                        **activity.get(row["id"], {}),
                    ))
                world.characters = characters
            world.reservations = {row["loc_id"]: row["char_id"] for row in reservations}
            world.events.clear()
            world.events.extend(reversed(recent))
            world.pending_events.clear()
            return world
        except Exception:
            return world

    # -- ledger / replay ----------------------------------------------------
    def append_event(self, tick, actor, kind, text) -> bool:
        try:
            self._exec("INSERT INTO events (tick,actor,kind,text) VALUES (?,?,?,?)",
                       (int(tick), str(actor), str(kind), str(text)[:400]))
            return True
        except Exception:
            return False

    def events(self, limit=100, from_tick=None) -> list:
        try:
            sql = "SELECT id,tick,actor,kind,text,event_key,created_at FROM events"
            params = ()
            if from_tick is not None:
                sql += " WHERE tick >= ?"
                params = (int(from_tick),)
            sql += " ORDER BY id DESC LIMIT ?"
            with self._lock:
                rows = list(self._conn.execute(sql, params + (int(limit),)))
            return [{key: row[key] for key in row.keys()
                     if key not in {"id", "event_key", "created_at"} or row["event_key"]}
                    for row in rows]
        except Exception:
            return []

    def checkpoint(self, world: World) -> dict:
        try:
            from .sim import snapshot

            payload = json.dumps(snapshot(world))
            self._exec(
                "INSERT INTO checkpoints (tick,created_at,snapshot_json) VALUES (?,?,?) "
                "ON CONFLICT(tick) DO UPDATE SET snapshot_json=excluded.snapshot_json,"
                "created_at=excluded.created_at",
                (int(world.tick), time.time(), payload))
            # A checkpoint is the natural maintenance point: keep the ledger and
            # the checkpoint table bounded without touching anything else.
            return {"ok": True, "tick": int(world.tick),
                    "pruned": self.maintenance()}
        except Exception as error:
            return {"ok": False, "reason": type(error).__name__}

    def prune_events(self, keep=EVENT_RETENTION) -> int:
        """Keep the newest `keep` ledger rows. Returns how many were removed.

        The events table is an append-only SIMULATION journal that grows every
        tick; unbounded it would grow forever on an always-on desktop. Only
        simulation rows are ever touched.
        """
        try:
            keep = max(100, int(keep))
            with self._lock:
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0]
                if total <= keep:
                    return 0
                cursor = self._conn.execute(
                    "DELETE FROM events WHERE id NOT IN "
                    "(SELECT id FROM events ORDER BY id DESC LIMIT ?)", (keep,))
                self._conn.commit()
                return int(cursor.rowcount or 0)
        except Exception:
            return 0

    def prune_checkpoints(self, keep=CHECKPOINT_RETENTION) -> int:
        try:
            keep = max(1, int(keep))
            with self._lock:
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM checkpoints").fetchone()[0]
                if total <= keep:
                    return 0
                cursor = self._conn.execute(
                    "DELETE FROM checkpoints WHERE tick NOT IN "
                    "(SELECT tick FROM checkpoints ORDER BY tick DESC LIMIT ?)",
                    (keep,))
                self._conn.commit()
                return int(cursor.rowcount or 0)
        except Exception:
            return 0

    def maintenance(self) -> dict:
        """Bounded retention for the lab's own tables. Never raises.

        Explicitly scoped: it prunes `events` and `checkpoints` only. Personal
        memory (`friday_memory.db`), tasks (`friday_tasks.db`) and provider
        state are different files and are never opened here.
        """
        try:
            return {"events_pruned": self.prune_events(),
                    "checkpoints_pruned": self.prune_checkpoints(),
                    "event_retention": EVENT_RETENTION,
                    "checkpoint_retention": CHECKPOINT_RETENTION}
        except Exception as error:
            return {"events_pruned": 0, "checkpoints_pruned": 0,
                    "error": type(error).__name__}

    def list_checkpoints(self) -> list:
        try:
            with self._lock:
                rows = list(self._conn.execute(
                    "SELECT tick,created_at FROM checkpoints ORDER BY tick DESC LIMIT 50"))
            return [{"tick": row["tick"], "created_at": row["created_at"]}
                    for row in rows]
        except Exception:
            return []

    def checkpoint_snapshot(self, tick) -> dict | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT snapshot_json FROM checkpoints WHERE tick=?",
                    (int(tick),)).fetchone()
            if row is None:
                return None
            return json.loads(row["snapshot_json"])
        except Exception:
            return None

    def replay(self, from_tick=0) -> list:
        """Ordered events from `from_tick` â€” the exact journal for replay."""
        return list(reversed(self.events(limit=10000, from_tick=from_tick)))
