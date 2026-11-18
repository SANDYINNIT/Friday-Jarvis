"""Five-domain local memory for FRIDAY with explicit, tested controls.

Importing this module has NO side effects: no database is opened, no schema is
written, and no environment variable is read at import time.  Every public
operation runs against an explicit :class:`LabMemory` handle constructed by the
caller; there is no module-level singleton.  The constructor itself performs no
I/O — the schema is created lazily on first use (or eagerly via
``init_schema()``).

Storage design (the decisions this module makes)
-------------------------------------------------
Domain 1 — assistant facts: live in the EXISTING ``memories`` table owned by
``memory.MemoryStore``.  This module performs a guarded, additive, idempotent
migration to add the provenance/pin columns the table is missing:

    epistemic TEXT            one of observed | told | interpretation
    source_event_id TEXT      world-event linkage (domain 4) for provenance
    pinned INTEGER NOT NULL DEFAULT 0
    version INTEGER NOT NULL DEFAULT 1
    derived_from TEXT         parent row id when a correction created this row

The migration reads ``PRAGMA table_info(memories)`` and issues one
``ALTER TABLE memories ADD COLUMN <c> <decl>`` per MISSING column only.  It
never DROPs a column, never UPDATEs an existing row, and never DELETEs one.
If an ALTER fails (table locked, read-only file, ...) the failure is recorded
in the returned report (``{"added", "failed", "missing"}``) and every consumer
degrades gracefully — ``pin()`` returns ``{"ok": False, "reason":
"migration_failed"}``, ``why_remember()`` returns ``None`` for missing
provenance — nothing raises.  Rows that predate the migration read back the
column defaults (``pinned=0``, ``version=1``) and ``epistemic=NULL``, which
every visibility predicate treats as ``observed``.

Domain 2 — character experiences: NEW table ``character_experiences`` in the
same database, ``UNIQUE(char_id, tick, kind, text)``, plus a ``deleted`` flag
so restore can SOFT-retire rows instead of deleting them (hard DELETE is never
used outside test fixtures).

Domain 3 — shared authorized work: NEW table ``artifact_memory``
(``artifact_id`` PK, ``revoked`` flag — revoke is a flag, never a delete).

Domain 4 — world events/replay: the world store's ``events`` table is
authoritative.  This module only ever SELECTs from it (and from ``checkpoints``
to anchor a date to ticks, and from ``characters`` to snapshot/restore a
position).  It never writes, never prunes, and never opens the world database
unless the caller passed an explicit ``world_db`` path that already exists.

Domain 5 — Notion = dashboard only: ``dashboard_projection()`` assembles plain
dicts from domains 1–4.  It has no write path of any kind (proved by test:
every SQL statement it issues is a SELECT/PRAGMA).

Full-text index — when FTS5 is available in the build, an EXTERNAL-CONTENT
FTS5 index (``memories_fts``, ``content='memories'``) keeps text out of a
second copy; triggers keep it in sync under the invariant that the index
holds EXACTLY the live rows (``deleted = 0 AND superseded_by IS NULL``):
insert indexes a live row, every UPDATE deletes the old entry only when the
pre-update row was live (FTS5's ``'delete'`` command raises "database disk
image is malformed" if run twice for one row, and ``WHERE rowid = ?`` on an
external-content table resolves against the content table so it cannot be
used as a membership test), and re-inserts only when the post-update row is
live and not superseded; the delete trigger drops a live row's entry the
same way.  ``init_schema()`` runs a full ``'rebuild'`` plus a cleanup pass
so the invariant holds even for rows that predate the triggers.  When FTS5 is unavailable (or ``prefer_fts5=False``, or the
``memories_fts`` name is taken by a non-FTS table) the module falls back to a
documented keyword scan (token overlap over ``key/topic/content``, the same
ranking ``MemoryStore.recall`` uses) and REPORTS the truth in
``index_status()`` — ``{"fts5": bool, "engine": "fts5"|"keyword", "error":
str|None}``.  This build is probed at runtime; the module never claims FTS5
works when the test proves otherwise.

Permission model (enforced at retrieval time AND inside every index query)
---------------------------------------------------------------------------
Permission levels, ordered: ``none(0) < read(1) < interpret(2) < admin(3)``.

* Ownership: domain-1 rows are assistant-private — the owner is
  ``ASSISTANT_ACTOR`` ("friday").  Domain-2/3 rows are owned by their
  ``char_id``.  Domain 4 is world-public.  Domain 5 is the assistant's own
  dashboard (liveness-filtered, not permission-filtered).
* ``observed`` / ``told``: visible to their owner at ANY level (even
  ``none``); visible to anyone else only with ``read`` or above.
* ``interpretation``: requires ``interpret`` or above — for the owner too.
* Artifacts: the owning character always sees its own; anyone with ``read``
  sees non-revoked artifacts; revoked artifacts are never visible.
* A row that fails the predicate is excluded in the SQL of ``index_search()``
  (the index-time enforcement) and re-checked in Python by ``recall()`` (the
  retrieval-time enforcement) — a filtered-out row is never returned by either
  surface.  Deletion/supersession are enforced even harder: ``forget()`` and
  ``correct()`` physically remove the row from the FTS index via triggers, so
  a raw ``MATCH`` cannot find it either (proved by test).

Controls
--------
* ``remember(..., epistemic=..., source=..., source_event_id=...)`` writes with
  provenance; ``epistemic`` is validated against the allowed set (others raise
  ``ValueError``).  Writes go through ``MemoryStore.remember`` (scope/UNIQUE
  semantics and the ``redact_for_storage`` chokepoint) with provenance in the same write.
* ``why_remember(id)`` returns exactly ``{source, epistemic, source_event_id,
  created_at, version}``.
* ``pin(id)`` / ``unpin(id)`` — pinned rows are excluded from fading/pruning.
* ``correct(id, new_content, reason)`` bumps ``version``, moves the canonical
  ``(scope, key, topic)`` slot to a NEW row (the superseded row's topic is
  suffixed ``[superseded:<id>]`` — additive bookkeeping; superseded rows are
  excluded from every read path and physically leave the index), sets
  ``superseded_by`` on the old row, records the reason in
  ``memory_corrections``, and marks every dependent summary stale so corrected
  content cannot resurface.
* ``forget(id)`` soft-deletes (``deleted=1``) and invalidates dependents the
  same way.  ``unforget(id)`` reverses it ONLY if no dependent was rebuilt:
  if any dependent summary is fresh (``stale=0``) the rebuild must have
  happened while the fact was dead, so the forget is documented as NOT
  reversible through this API and ``unforget`` refuses with ``reason:
  "dependent_rebuilt"``.  Fresh dependents can only be created while the
  source row is live, so the normal forget -> unforget cycle is allowed and
  dependents stay stale until explicitly regenerated.
* ``fade_relevance(...)`` soft-retires trivial rows (low confidence, unpinned,
  old) by relevance — it sets ``expires_at`` (never a hard delete, never
  ``deleted=1``), and pinned rows never fade.
* ``character_checkpoint(char_id)`` / ``restore_character_checkpoint(char_id,
  checkpoint_id)`` snapshot/restore ONLY that character's domain-2 rows plus
  its world ``characters`` row (decor/position) when a world DB is present.
  Restore never touches domain 1, ``meta`` (provider/sim config), or anything
  else — proved byte-identical by test.
* ``scrapbook(char_id)`` / ``journal_recap(date)`` are built purely from
  recorded rows/events (bounded, verbatim; never fabricated prose).
  ``journal_recap`` anchors world events to a date via ``checkpoints``
  (``date(created_at,'unixepoch')`` -> ticks) because ``events`` has no
  timestamp column of its own.

Non-negotiables honoured here
-----------------------------
* Never writes under ``~/.friday``: constructing a handle with either path
  inside ``~/.friday`` raises ``ValueError``; the world DB is never created
  (only opened if it already exists); no default path is ever derived.
* Never hard-deletes rows in production code — soft delete, supersession,
  ``revoked`` and ``stale`` flags only (test fixtures may delete inside this
  module's own NEW tables).
* Every return value is a plain JSON-serialisable dict/list of scalars.
* Every recap/list output has an explicit cap.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .credential_stripper import redact_for_storage
from .memory import MemoryStore, live_memory_clause, memory_row_id

__all__ = [
    "LabMemory",
    "migrate_assistant_columns",
    "ASSISTANT_ACTOR",
    "EPISTEMIC_CLASSES",
    "PERMISSION_NAMES",
    "ASSISTANT_COLUMNS",
]

ASSISTANT_ACTOR = "friday"
EPISTEMIC_CLASSES = ("observed", "told", "interpretation")
PERMISSION_NAMES = ("none", "read", "interpret", "admin")
_PERMISSION_LEVELS = {"none": 0, "read": 1, "interpret": 2, "admin": 3}

ASSISTANT_COLUMNS = {
    "epistemic": "TEXT",
    "source_event_id": "TEXT",
    "pinned": "INTEGER NOT NULL DEFAULT 0",
    "version": "INTEGER NOT NULL DEFAULT 1",
    "derived_from": "TEXT",
}

FTS_TABLE = "memories_fts"

DEFAULT_TOP_N = 5
MAX_TOP_N = 50
MAX_CANDIDATES = 500
DEFAULT_DASHBOARD_LIMIT = 20
MAX_DASHBOARD_LIMIT = 100
DEFAULT_SCRAPBOOK_LIMIT = 50
MAX_SCRAPBOOK_LIMIT = 200
DEFAULT_RECAP_LIMIT = 25
MAX_RECAP_LIMIT = 100
MAX_ARTIFACT_LIMIT = 500
MAX_SUMMARY_LIMIT = 200
MAX_SUMMARY_CHARS = 1000
MAX_CHECKPOINT_EXPERIENCES = 500
DEFAULT_FADE_LIMIT = 50
MAX_FADE_LIMIT = 500

_DOMAIN_ALIASES = {
    "1": "assistant", "d1": "assistant", "assistant": "assistant",
    "facts": "assistant", "fact": "assistant", "memory": "assistant",
    "memories": "assistant",
    "2": "character", "d2": "character", "character": "character",
    "characters": "character", "experiences": "character",
    "experience": "character",
    "3": "artifact", "d3": "artifact", "artifact": "artifact",
    "artifacts": "artifact", "artefacts": "artifact",
    "4": "world", "d4": "world", "world": "world", "events": "world",
    "5": "dashboard", "d5": "dashboard", "dashboard": "dashboard",
    "notion": "dashboard",
}

LAB_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS character_experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id TEXT NOT NULL,
    tick INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    epistemic TEXT NOT NULL DEFAULT 'observed',
    source_event_id TEXT,
    created_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (char_id, tick, kind, text)
);
CREATE INDEX IF NOT EXISTS character_experiences_char_idx
    ON character_experiences (char_id, deleted, tick);
CREATE TABLE IF NOT EXISTS artifact_memory (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT,
    char_id TEXT,
    label TEXT,
    path TEXT,
    created_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS artifact_memory_char_idx
    ON artifact_memory (char_id, revoked);
CREATE TABLE IF NOT EXISTS memory_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_summaries_memory_idx
    ON memory_summaries (memory_id, stale);
CREATE TABLE IF NOT EXISTS memory_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER NOT NULL,
    corrected_id INTEGER,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_corrections_memory_idx
    ON memory_corrections (memory_id);
CREATE TABLE IF NOT EXISTS character_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    char_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS character_checkpoints_char_idx
    ON character_checkpoints (char_id, created_at);
"""

_TRIGGER_INSERT = """
CREATE TRIGGER IF NOT EXISTS memories_lab_fts_insert AFTER INSERT ON memories
WHEN new.deleted = 0 AND new.superseded_by IS NULL
BEGIN
  INSERT INTO memories_fts(rowid, key, topic, content)
  VALUES (new.id, new.key, new.topic, new.content);
END;
"""

_TRIGGER_UPDATE = """
CREATE TRIGGER IF NOT EXISTS memories_lab_fts_update AFTER UPDATE ON memories
BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, key, topic, content)
  SELECT 'delete', old.id, old.key, old.topic, old.content
  WHERE old.deleted = 0 AND old.superseded_by IS NULL;
  INSERT INTO memories_fts(rowid, key, topic, content)
  SELECT new.id, new.key, new.topic, new.content
  WHERE new.deleted = 0 AND new.superseded_by IS NULL;
END;
"""

_TRIGGER_DELETE = """
CREATE TRIGGER IF NOT EXISTS memories_lab_fts_delete AFTER DELETE ON memories
BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, key, topic, content)
  SELECT 'delete', old.id, old.key, old.topic, old.content
  WHERE old.deleted = 0 AND old.superseded_by IS NULL;
END;
"""


def _timestamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _tokens(value):
    return tuple(dict.fromkeys(re.findall(r"[\w]+", str(value or "").casefold())))


def _clamp(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


def _optional_str(value):
    return None if value is None else str(value)


def _validate_epistemic(value):
    normalized = str(value or "").strip().lower()
    if normalized not in EPISTEMIC_CLASSES:
        raise ValueError(
            "epistemic must be one of: " + ", ".join(EPISTEMIC_CLASSES)
        )
    return normalized


def _permission_level(permission):
    key = str(permission or "").strip().lower()
    if key not in _PERMISSION_LEVELS:
        raise ValueError(
            "permission must be one of: " + ", ".join(PERMISSION_NAMES)
        )
    return _PERMISSION_LEVELS[key]


def _normalize_domain(domain):
    key = str(domain or "").strip().lower()
    mapped = _DOMAIN_ALIASES.get(key)
    if mapped is None:
        raise ValueError(
            "domain must be one of: assistant (1), character (2), "
            "artifact (3), world (4)"
        )
    if mapped == "dashboard":
        raise ValueError(
            "domain 5 (dashboard) is projection-only; use dashboard_projection()"
        )
    return mapped


def _validate_date(value):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("date must be YYYY-MM-DD")
    datetime.strptime(text, "%Y-%m-%d")
    return text


def _is_under_friday_state(path):
    """True when ``path`` lives inside ``~/.friday`` (never allowed here)."""
    try:
        target = Path(path).expanduser().resolve()
        forbidden = (Path.home() / ".friday").resolve()
    except Exception:
        return False
    return target == forbidden or forbidden in target.parents


def _visibility_clause(alias, actor, level, *, owner_column=None):
    """SQL fragment + params enforcing the documented permission model."""
    read = 1 if level >= 1 else 0
    interpret = 1 if level >= 2 else 0
    if owner_column is None:
        owner = 1 if str(actor) == ASSISTANT_ACTOR else 0
        owner_sql = "?"
        owner_params = (owner,)
    else:
        owner_sql = f"{alias}.{owner_column} = ?"
        owner_params = (str(actor),)
    clause = (
        f"((COALESCE({alias}.epistemic, 'observed') IN ('observed', 'told') "
        f"AND ({owner_sql} OR ?)) "
        f"OR (COALESCE({alias}.epistemic, 'observed') = 'interpretation' AND ?))"
    )
    return clause, (*owner_params, read, interpret)


def _artifact_visibility_clause(alias, actor, level):
    read = 1 if level >= 1 else 0
    return f"({alias}.char_id = ? OR ?)", (str(actor), read)


def _row_visible(row, domain, actor, level):
    """Python-side mirror of the SQL visibility predicates."""
    if domain == "world":
        return True
    if domain == "artifact":
        if level >= 1:
            return True
        return str(row.get("char_id") or "") == str(actor)
    epistemic = row.get("epistemic") or "observed"
    if epistemic == "interpretation":
        return level >= 2
    if domain == "character":
        return str(row.get("char_id") or "") == str(actor) or level >= 1
    return str(actor) == ASSISTANT_ACTOR or level >= 1


_SEARCH_FIELDS = {
    "assistant": ("key", "topic", "content"),
    "character": ("text", "kind"),
    "artifact": ("label", "path", "task_id", "artifact_id"),
    "world": ("actor", "kind", "text"),
}


def _searchable_text(row, domain):
    fields = _SEARCH_FIELDS[domain]
    return " ".join(str(row.get(field) or "") for field in fields)


def _rank_rows(rows, tokens, domain, *, require_match, top_n):
    scored = []
    for row in rows:
        data = dict(row)
        searchable = _searchable_text(data, domain).casefold()
        matches = sum(1 for token in tokens if token in searchable)
        if require_match and matches == 0:
            continue
        confidence = data.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = 0.5
        tie_time = str(data.get("updated_at") or data.get("created_at") or "")
        tie_id = str(data.get("id") or data.get("artifact_id") or "")
        scored.append((matches + float(confidence), tie_time, tie_id, data))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    results = []
    for item in scored[:top_n]:
        data = item[3]
        data["domain"] = domain
        results.append(data)
    return results


def migrate_assistant_columns(conn):
    """Additive, idempotent, guarded migration of the ``memories`` table.

    Adds only the columns in ``ASSISTANT_COLUMNS`` that are missing.  Never
    DROPs, never UPDATEs an existing row, never DELETEs one.  An ALTER that
    fails (locked database, read-only file, ...) is recorded in ``failed``
    rather than raised, so callers can degrade gracefully.
    """
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    except sqlite3.Error as error:
        return {
            "added": [],
            "failed": [{"column": "*", "error": str(error)}],
            "missing": sorted(ASSISTANT_COLUMNS),
        }
    if "id" not in existing:
        return {
            "added": [],
            "failed": [{"column": "*", "error": "memories table missing"}],
            "missing": sorted(ASSISTANT_COLUMNS),
        }
    added = []
    failed = []
    for column, declaration in ASSISTANT_COLUMNS.items():
        if column in existing:
            continue
        try:
            conn.execute(
                f"ALTER TABLE memories ADD COLUMN {column} {declaration}"
            )
            added.append(column)
        except sqlite3.Error as error:
            failed.append({"column": column, "error": str(error)})
    try:
        after = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    except sqlite3.Error as error:
        after = existing
        failed.append({"column": "*", "error": str(error)})
    missing = sorted(column for column in ASSISTANT_COLUMNS if column not in after)
    return {"added": added, "failed": failed, "missing": missing}


# LAB-3R5: additive-only columns for the character-experiences table (the
# character inspector's pin/unpin needs durable state). Same guard rules as
# migrate_assistant_columns: never DROP, never UPDATE, never DELETE.
EXPERIENCE_COLUMNS = {
    "pinned": "INTEGER NOT NULL DEFAULT 0",
}


def migrate_experience_columns(conn):
    """Additive, idempotent, guarded migration of ``character_experiences``."""
    try:
        existing = {
            row[1] for row in
            conn.execute("PRAGMA table_info(character_experiences)")
        }
    except sqlite3.Error as error:
        return {"added": [],
                "failed": [{"column": "*", "error": str(error)}],
                "missing": sorted(EXPERIENCE_COLUMNS)}
    if "id" not in existing:
        return {"added": [],
                "failed": [{"column": "*",
                            "error": "character_experiences table missing"}],
                "missing": sorted(EXPERIENCE_COLUMNS)}
    added, failed = [], []
    for column, declaration in EXPERIENCE_COLUMNS.items():
        if column in existing:
            continue
        try:
            conn.execute(
                f"ALTER TABLE character_experiences "
                f"ADD COLUMN {column} {declaration}"
            )
            added.append(column)
        except sqlite3.Error as error:
            failed.append({"column": column, "error": str(error)})
    try:
        after = {
            row[1] for row in
            conn.execute("PRAGMA table_info(character_experiences)")
        }
    except sqlite3.Error as error:
        after = existing
        failed.append({"column": "*", "error": str(error)})
    missing = sorted(c for c in EXPERIENCE_COLUMNS if c not in after)
    return {"added": added, "failed": failed, "missing": missing}


class LabMemory:
    """Explicit handle over FRIDAY's five memory domains.

    The constructor performs no I/O; the schema is created on first use.

    For the process-wide singleton see the module-level `shared()` at the
    BOTTOM of this module (ROUND LAB-3).
    """

    def __init__(self, memory_db, world_db=None, *, prefer_fts5=True):
        if memory_db is None:
            raise ValueError("memory_db is required (explicit store handle)")
        self._memory_db = Path(memory_db)
        self._world_db = Path(world_db) if world_db else None
        if _is_under_friday_state(self._memory_db):
            raise ValueError("lab memory must not live under ~/.friday")
        if self._world_db is not None and _is_under_friday_state(self._world_db):
            raise ValueError("lab memory must not use ~/.friday world state")
        self._prefer_fts5 = bool(prefer_fts5)
        self._memory = None
        self._ready = False
        self._init_lock = threading.RLock()
        self._columns = set(ASSISTANT_COLUMNS)
        self._migration_report = {"added": [], "failed": [], "missing": []}
        self._engine = "keyword"
        self._fts5 = False
        self._index_error = None
        self._index_detail = "schema not initialised yet"

    # -- connections -------------------------------------------------------
    def _connect(self):
        conn = sqlite3.connect(str(self._memory_db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _conn(self):
        self._ensure_ready()
        conn = self._connect()
        try:
            try:
                self._columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(memories)")
                }
            except sqlite3.Error:
                pass
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _world_conn(self, *, write=False):
        if self._world_db is None or not Path(self._world_db).exists():
            yield None
            return
        conn = sqlite3.connect(str(self._world_db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                with conn:
                    yield conn
            else:
                yield conn
        finally:
            conn.close()

    def _memory_store(self):
        if self._memory is None:
            self._memory = MemoryStore(self._memory_db)
        return self._memory

    def _ensure_ready(self):
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            self.init_schema()

    # -- schema ------------------------------------------------------------
    def init_schema(self):
        """Create/migrate the schema idempotently; return the migration report."""
        self._memory_store()
        conn = self._connect()
        try:
            with conn:
                report = self._initialize(conn)
            try:
                self._columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(memories)")
                }
            except sqlite3.Error:
                pass
            self._ready = True
            return report
        finally:
            conn.close()

    def _initialize(self, conn):
        migration = migrate_assistant_columns(conn)
        self._migration_report = migration
        conn.executescript(LAB_TABLES_DDL)
        # LAB-3R5: additive experience columns AFTER the DDL (CREATE TABLE
        # IF NOT EXISTS never adds columns to an existing table).
        experience_migration = migrate_experience_columns(conn)
        self._experience_migration_report = experience_migration
        index = self._init_index(conn)
        return {"added": list(migration["added"]),
                "failed": list(migration["failed"]),
                "missing": list(migration["missing"]),
                "experience_added": list(experience_migration["added"]),
                "experience_failed": list(experience_migration["failed"]),
                "experience_missing": list(experience_migration["missing"]),
                "index": index}

    def _init_index(self, conn):
        if not self._prefer_fts5:
            self._engine = "keyword"
            self._fts5 = False
            self._index_error = None
            self._index_detail = "disabled by caller (prefer_fts5=False)"
            return {"engine": "keyword", "fts5": False, "error": None,
                    "detail": self._index_detail}
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (FTS_TABLE,)
        ).fetchone()
        if row is None:
            try:
                conn.execute(
                    f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
                    "key, topic, content, content='memories', content_rowid='id')"
                )
            except sqlite3.Error as error:
                self._engine = "keyword"
                self._fts5 = False
                self._index_error = str(error)
                self._index_detail = f"FTS5 unavailable: {error}"
                return {"engine": "keyword", "fts5": False,
                        "error": str(error), "detail": self._index_detail}
        elif not row["sql"] or "fts5" not in str(row["sql"]).lower():
            error = f"table {FTS_TABLE} already exists and is not an FTS5 table"
            self._engine = "keyword"
            self._fts5 = False
            self._index_error = error
            self._index_detail = error
            return {"engine": "keyword", "fts5": False, "error": error,
                    "detail": error}
        conn.execute(_TRIGGER_INSERT)
        conn.execute(_TRIGGER_UPDATE)
        conn.execute(_TRIGGER_DELETE)
        self._rebuild_fts(conn)
        self._engine = "fts5"
        self._fts5 = True
        self._index_error = None
        self._index_detail = (
            "external-content FTS5 index over memories(key, topic, content) "
            "maintained by triggers"
        )
        return {"engine": "fts5", "fts5": True, "error": None,
                "detail": self._index_detail}

    @staticmethod
    def _rebuild_fts(conn):
        """Full rebuild from the content table, then drop non-live entries."""
        conn.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')")
        conn.execute(
            f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, key, topic, content) "
            "SELECT 'delete', id, key, topic, content FROM memories "
            "WHERE deleted = 1 OR superseded_by IS NOT NULL"
        )

    def migration_report(self):
        """Current migration state: columns added/failed/still missing."""
        with self._conn() as conn:
            try:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(memories)")
                }
            except sqlite3.Error:
                columns = set()
        recorded = self._migration_report or {"added": [], "failed": []}
        missing = sorted(
            column for column in ASSISTANT_COLUMNS if column not in columns
        )
        return {"added": list(recorded.get("added", [])),
                "failed": list(recorded.get("failed", [])),
                "missing": missing}

    def index_status(self):
        """Truthful report of the full-text engine actually in use."""
        self._ensure_ready()
        return {"engine": self._engine, "fts5": self._fts5,
                "error": self._index_error,
                "table": FTS_TABLE if self._engine == "fts5" else None,
                "detail": self._index_detail}

    def rebuild_index(self):
        """Explicitly rebuild the index (repairs drift from outside writers)."""
        with self._conn() as conn:
            if self._engine != "fts5":
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM memories "
                    "WHERE deleted = 0 AND superseded_by IS NULL"
                ).fetchone()["n"]
                return {"ok": True, "engine": "keyword", "indexed": int(count),
                        "detail": "keyword fallback has no separate index; "
                                  "live rows are scanned directly"}
            self._rebuild_fts(conn)
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM memories "
                "WHERE deleted = 0 AND superseded_by IS NULL"
            ).fetchone()["n"]
        return {"ok": True, "engine": "fts5", "indexed": int(count)}

    # -- domain 1: assistant facts ----------------------------------------
    def remember(
        self,
        key=None,
        content="",
        *,
        scope="persistent",
        topic="",
        source="explicit",
        confidence=1.0,
        expires_at=None,
        kind="fact",
        epistemic="observed",
        source_event_id=None,
        derived_from=None,
        pinned=False,
    ):
        """Write one assistant fact with validated provenance; return its row.

        Goes through ``MemoryStore.remember`` (scope/UNIQUE semantics and the
        ``redact_for_storage`` chokepoint) and then sets the provenance
        columns.  ``epistemic`` must be observed|told|interpretation.
        ``pinned`` only applies to a newly created row; pin state is otherwise
        owned by ``pin()``/``unpin()``.  Version is only bumped by ``correct()``.
        """
        self._ensure_ready()
        return self._memory_store().remember(
            key=key,
            content=content,
            scope=scope,
            topic=topic,
            source=source,
            confidence=confidence,
            expires_at=expires_at,
            kind=kind,
            _provenance={
                "epistemic": _validate_epistemic(epistemic),
                "source_event_id": _optional_str(source_event_id),
                "derived_from": _optional_str(derived_from),
                "pinned": int(bool(pinned)),
            },
        )

    def why_remember(self, memory_id):
        """Return {source, epistemic, source_event_id, created_at, version}."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (int(memory_id),)
            ).fetchone()
        if row is None:
            raise ValueError("unknown memory id")
        data = dict(row)
        return {
            "source": data.get("source"),
            "epistemic": data.get("epistemic"),
            "source_event_id": data.get("source_event_id"),
            "created_at": data.get("created_at"),
            "version": data.get("version"),
        }

    def _set_pin(self, memory_id, value):
        try:
            memory_id = memory_row_id(memory_id)
        except ValueError:
            return {"ok": False, "reason": "invalid_memory_id"}
        try:
            with self._conn() as conn:
                if "pinned" not in self._columns:
                    return {"ok": False, "reason": "migration_failed"}
                changed = conn.execute(
                    f"UPDATE memories SET pinned = ? WHERE id = ? AND {live_memory_clause()}",
                    (int(value), memory_id, _timestamp()),
                ).rowcount
        except sqlite3.Error:
            return {"ok": False, "reason": "pin_write_failed"}
        if not changed:
            return {"ok": False, "reason": "no_live_source"}
        return {"ok": True, "memory_id": memory_id, "pinned": value}

    def pin(self, memory_id):
        """Pin a memory so it is never faded or pruned."""
        return self._set_pin(memory_id, True)

    def unpin(self, memory_id):
        """Remove the pin from a memory."""
        return self._set_pin(memory_id, False)

    def correct(self, memory_id, new_content, reason):
        """Supersede a current canonical fact and preserve its audit history."""
        self._ensure_ready()
        result = self._memory_store().correct(
            memory_id, content=str(new_content or ""), reason=reason,
        )
        if result is None:
            raise ValueError("memory is inactive or the correction would not change it")
        return result

    def forget(self, memory_id):
        """Soft-delete one current fact and invalidate dependent summaries."""
        try:
            memory_id = memory_row_id(memory_id)
        except ValueError:
            return {"ok": False, "reason": "invalid_memory_id"}
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _timestamp()
            live = conn.execute(
                f"SELECT 1 FROM memories WHERE id = ? AND {live_memory_clause()}",
                (memory_id, now),
            ).fetchone()
            if live is None:
                return {"ok": False, "reason": "no_live_source"}
            invalidated = self._memory_store()._forget_rows(conn, [memory_id], now)
        return {"ok": True, "memory_id": memory_id,
                "dependents_invalidated": invalidated}

    def unforget(self, memory_id):
        """Reverse a forget, but only when no dependent was rebuilt.

        Proof rule: every dependent summary referencing the fact must still be
        stale (or absent).  A fresh dependent could only have been rebuilt
        while the fact was dead, so in that case the forget is NOT reversible
        through this API and the call refuses with
        ``reason="dependent_rebuilt"``.
        """
        try:
            memory_id = memory_row_id(memory_id)
        except ValueError:
            return {"ok": False, "reason": "invalid_memory_id"}
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _timestamp()
            row = conn.execute(
                "SELECT deleted, superseded_by, expires_at FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                return {"ok": False, "reason": "unknown_memory"}
            if (row["superseded_by"] is not None
                    or (row["expires_at"] is not None and row["expires_at"] <= now)):
                return {"ok": False, "reason": "no_live_source"}
            if not row["deleted"]:
                return {"ok": False, "reason": "not_forgotten"}
            fresh = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_summaries "
                "WHERE memory_id = ? AND stale = 0",
                (memory_id,),
            ).fetchone()["n"]
            if fresh:
                return {
                    "ok": False,
                    "reason": "dependent_rebuilt",
                    "detail": "a dependent was rebuilt after the forget; this "
                              "forget is not reversible through this API",
                }
            conn.execute(
                "UPDATE memories SET deleted = 0, updated_at = ? WHERE id = ?",
                (now, memory_id),
            )
        return {"ok": True, "memory_id": memory_id,
                "note": "dependent summaries stay stale until regenerated"}

    def fade_relevance(
        self,
        *,
        query="",
        max_confidence=0.5,
        min_age_days=7,
        limit=DEFAULT_FADE_LIMIT,
    ):
        """Soft-retire trivial (low-confidence, unpinned, old) memories.

        Fading sets ``expires_at`` — the row stays fully present, never a hard
        delete, never ``deleted=1``.  Pinned rows are excluded by the query
        itself and can never fade.  Candidates are retired least-relevant
        first (token overlap with ``query`` when given, then confidence, then
        age), up to ``limit``.
        """
        try:
            max_confidence = float(max_confidence)
        except (TypeError, ValueError):
            max_confidence = 0.5
        max_confidence = min(max(max_confidence, 0.0), 1.0)
        try:
            min_age_days = max(0, int(min_age_days))
        except (TypeError, ValueError):
            min_age_days = 7
        limit = _clamp(limit, DEFAULT_FADE_LIMIT, 0, MAX_FADE_LIMIT)
        if limit == 0:
            return {"ok": True, "faded": [], "count": 0}
        now = _timestamp()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=min_age_days)
        ).replace(microsecond=0).isoformat()
        tokens = _tokens(query)
        with self._conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT id, key, topic, content, confidence, updated_at "
                    "FROM memories "
                    f"WHERE {live_memory_clause()} "
                    "AND pinned = 0 AND confidence <= ? AND updated_at <= ? "
                    "ORDER BY updated_at ASC, id ASC LIMIT ?",
                    (now, max_confidence, cutoff, MAX_CANDIDATES),
                ).fetchall()
            except sqlite3.Error as error:
                return {"ok": False, "reason": "migration_failed",
                        "error": str(error)}
            scored = []
            for row in rows:
                searchable = " ".join(
                    (str(row["key"] or ""), str(row["topic"] or ""),
                     str(row["content"] or ""))
                ).casefold()
                overlap = sum(1 for token in tokens if token in searchable)
                confidence = (
                    row["confidence"]
                    if isinstance(row["confidence"], (int, float)) else 0.5
                )
                scored.append(
                    (overlap, float(confidence),
                     str(row["updated_at"] or ""), row["id"])
                )
            scored.sort()
            faded = [item[3] for item in scored[:limit]]
            if faded:
                conn.executemany(
                    "UPDATE memories SET expires_at = ? WHERE id = ?",
                    [(now, memory_id) for memory_id in faded],
                )
        return {"ok": True, "faded": faded, "count": len(faded)}

    # -- retrieval: recall (retrieval-time) + index_search (index-time) ----
    @staticmethod
    def _fts_match(tokens):
        if not tokens:
            return None
        return " OR ".join('"' + token.replace('"', '""') + '"'
                           for token in tokens)

    def _assistant_candidates(self, conn, tokens, actor, level):
        """Rows for domain 1 via the index (FTS) or the keyword fallback."""
        live_sql, live_params = live_memory_clause("m"), (_timestamp(),)
        vis_sql, vis_params = _visibility_clause("m", actor, level)
        match = self._fts_match(tokens)
        if self._engine == "fts5" and match:
            try:
                rows = conn.execute(
                    f"SELECT m.* FROM {FTS_TABLE} "
                    "JOIN memories m ON m.id = memories_fts.rowid "
                    f"WHERE memories_fts MATCH ? AND {live_sql} AND {vis_sql} "
                    "ORDER BY m.id DESC LIMIT ?",
                    (match, *live_params, *vis_params, MAX_CANDIDATES),
                ).fetchall()
                return rows, False
            except sqlite3.Error:
                pass  # corrupt/unavailable index -> keyword fallback below
        rows = conn.execute(
            f"SELECT m.* FROM memories m WHERE {live_sql} AND {vis_sql} "
            "ORDER BY m.id DESC LIMIT ?",
            (*live_params, *vis_params, MAX_CANDIDATES),
        ).fetchall()
        return rows, True

    def _character_candidates(self, conn, actor, level):
        vis_sql, vis_params = _visibility_clause(
            "e", actor, level, owner_column="char_id"
        )
        return conn.execute(
            f"SELECT e.* FROM character_experiences e "
            f"WHERE e.deleted = 0 AND {vis_sql} "
            "ORDER BY e.id DESC LIMIT ?",
            (*vis_params, MAX_CANDIDATES),
        ).fetchall()

    def _artifact_candidates(self, conn, actor, level):
        vis_sql, vis_params = _artifact_visibility_clause("a", actor, level)
        return conn.execute(
            f"SELECT a.* FROM artifact_memory a "
            f"WHERE a.revoked = 0 AND {vis_sql} "
            "ORDER BY a.created_at DESC, a.artifact_id LIMIT ?",
            (*vis_params, MAX_CANDIDATES),
        ).fetchall()

    def _world_candidates(self, conn):
        if conn is None:
            return []
        try:
            return conn.execute(
                "SELECT id, tick, actor, kind, text FROM events "
                "ORDER BY id DESC LIMIT ?",
                (MAX_CANDIDATES,),
            ).fetchall()
        except sqlite3.Error:
            return []

    def index_search(
        self,
        query,
        *,
        domain="assistant",
        actor=ASSISTANT_ACTOR,
        permission="read",
        top_n=DEFAULT_TOP_N,
    ):
        """Search through the index with the permission predicate IN SQL.

        This is the index-time enforcement surface: a row the actor may not
        see is filtered out inside the index query itself and can never be
        returned here, regardless of how the caller phrases the query.
        """
        domain = _normalize_domain(domain)
        level = _permission_level(permission)
        top_n = _clamp(top_n, DEFAULT_TOP_N, 0, MAX_TOP_N)
        tokens = _tokens(query)
        if not tokens or top_n == 0:
            return []
        if domain == "world":
            with self._world_conn() as world:
                rows = self._world_candidates(world)
            require_match = True
        else:
            with self._conn() as conn:
                if domain == "assistant":
                    rows, require_match = self._assistant_candidates(
                        conn, tokens, actor, level
                    )
                elif domain == "character":
                    rows = self._character_candidates(conn, actor, level)
                    require_match = True
                else:
                    rows = self._artifact_candidates(conn, actor, level)
                    require_match = True
        return _rank_rows(rows, tokens, domain,
                          require_match=require_match, top_n=top_n)

    def recall(
        self,
        query,
        *,
        domain="assistant",
        actor=ASSISTANT_ACTOR,
        permission="read",
        top_n=DEFAULT_TOP_N,
    ):
        """Permission-aware retrieval: index search + retrieval-time re-check.

        Every returned row is re-validated against the visibility model in
        Python, so a row filtered out for this actor cannot leak even if the
        index path were bypassed or buggy.
        """
        domain = _normalize_domain(domain)
        level = _permission_level(permission)
        results = self.index_search(
            query, domain=domain, actor=actor, permission=permission,
            top_n=top_n,
        )
        return [row for row in results
                if _row_visible(row, domain, actor, level)]

    # -- dependent summaries (invalidated by correct/forget) ---------------
    def create_summary(self, memory_id, summary_text):
        """Attach a dependent summary to a LIVE fact (refuses dead sources)."""
        try:
            memory_id = memory_row_id(memory_id)
        except ValueError:
            raise ValueError("memory_id must be an integer") from None
        text = redact_for_storage(str(summary_text or "").strip())
        if not text:
            raise ValueError("summary_text is required")
        now = _timestamp()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT deleted, superseded_by, expires_at FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if source is None:
                return {"ok": False, "reason": "unknown_memory"}
            if source["deleted"]:
                return {"ok": False, "reason": "memory_forgotten"}
            if source["superseded_by"] is not None:
                return {"ok": False, "reason": "memory_superseded"}
            if source["expires_at"] is not None and source["expires_at"] <= now:
                return {"ok": False, "reason": "no_live_source"}
            existing = conn.execute(
                "SELECT id FROM memory_summaries WHERE memory_id = ? "
                "ORDER BY id ASC LIMIT 1",
                (memory_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memory_summaries SET summary = ?, stale = 0, "
                    "updated_at = ? WHERE id = ?",
                    (text, now, existing["id"]),
                )
                summary_id = existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO memory_summaries "
                    "(memory_id, summary, stale, created_at, updated_at) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (memory_id, text, now, now),
                )
                summary_id = cursor.lastrowid
        return {"ok": True, "summary_id": summary_id,
                "memory_id": memory_id, "stale": 0}

    def list_summaries(self, memory_id, *, include_stale=False):
        """List dependent summaries; fresh ones by default, bounded."""
        try:
            memory_id = memory_row_id(memory_id)
        except ValueError:
            return []
        sql = "SELECT s.* FROM memory_summaries s"
        if include_stale:
            sql += " WHERE s.memory_id = ?"
            params = (memory_id, MAX_SUMMARY_LIMIT)
        else:
            sql += (" JOIN memories m ON m.id = s.memory_id "
                    f"WHERE s.memory_id = ? AND s.stale = 0 AND {live_memory_clause('m')}")
            params = (memory_id, _timestamp(), MAX_SUMMARY_LIMIT)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY s.id ASC LIMIT ?", params).fetchall()
        return [dict(row) for row in rows]

    def regenerate_summary(self, memory_id):
        """Rebuild a dependent from the CURRENT live row only.

        A forgotten or superseded source has no live content to rebuild from,
        so the dependent stays stale forever — corrected/removed content
        cannot be regenerated and therefore cannot resurface.
        """
        try:
            memory_id = memory_row_id(memory_id)
        except ValueError:
            raise ValueError("memory_id must be an integer") from None
        now = _timestamp()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                f"SELECT content FROM memories WHERE id = ? AND {live_memory_clause()}",
                (memory_id, now),
            ).fetchone()
            if source is None:
                return {
                    "ok": False,
                    "reason": "no_live_source",
                    "detail": "source is forgotten or superseded; the "
                              "dependent stays stale and cannot resurface",
                }
            summary = str(source["content"])[:MAX_SUMMARY_CHARS]
            existing = conn.execute(
                "SELECT id FROM memory_summaries WHERE memory_id = ? "
                "ORDER BY id ASC LIMIT 1",
                (memory_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memory_summaries SET summary = ?, stale = 0, "
                    "updated_at = ? WHERE id = ?",
                    (summary, now, existing["id"]),
                )
                summary_id = existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO memory_summaries "
                    "(memory_id, summary, stale, created_at, updated_at) "
                    "VALUES (?, ?, 0, ?, ?)",
                    (memory_id, summary, now, now),
                )
                summary_id = cursor.lastrowid
        return {"ok": True, "summary_id": summary_id,
                "memory_id": memory_id, "stale": 0, "summary": summary}

    # -- domain 2: character experiences ----------------------------------
    def record_experience(
        self,
        char_id,
        tick,
        kind,
        text,
        *,
        epistemic="observed",
        source_event_id=None,
    ):
        """Record one character experience with provenance (idempotent)."""
        epistemic = _validate_epistemic(epistemic)
        char_id = str(char_id or "").strip()
        kind = str(kind or "").strip()
        text = redact_for_storage(str(text or "").strip())
        if not char_id or not kind or not text:
            raise ValueError("char_id, kind and text are required")
        try:
            tick = int(tick)
        except (TypeError, ValueError):
            raise ValueError("tick must be an integer") from None
        now = _timestamp()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM character_experiences "
                "WHERE char_id = ? AND tick = ? AND kind = ? AND text = ?",
                (char_id, tick, kind, text),
            ).fetchone()
            if existing:
                data = dict(existing)
                data["created"] = False
                return data
            cursor = conn.execute(
                "INSERT INTO character_experiences "
                "(char_id, tick, kind, text, epistemic, source_event_id, "
                " created_at, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (char_id, tick, kind, text, epistemic,
                 _optional_str(source_event_id), now),
            )
            row = conn.execute(
                "SELECT * FROM character_experiences WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        data = dict(row)
        data["created"] = True
        return data

    @staticmethod
    def _experience_target(conn, memory_id, char_id):
        """Resolve an owned, live experience inside its write transaction."""
        return conn.execute(
            "SELECT * FROM character_experiences "
            "WHERE id = ? AND char_id = ? AND deleted = 0",
            (memory_id, char_id),
        ).fetchone()

    def correct_experience(self, memory_id, new_content, reason, *, expect_char_id,
                           expected_text=None):
        """Correct one live experience owned by the specified character."""
        memory_id = memory_row_id(memory_id)
        new_content = redact_for_storage(str(new_content or "").strip())
        reason_text = redact_for_storage(str(reason or "").strip())
        if not new_content or not reason_text:
            raise ValueError("new content and reason are required")
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._experience_target(conn, memory_id, expect_char_id)
            if row is None:
                raise ValueError("no live experience for this character")
            if expected_text is not None and row["text"] != expected_text:
                raise ValueError("This experience changed while you were editing it.")
            conn.execute(
                "UPDATE character_experiences SET text = ? WHERE id = ?",
                (f"{new_content}  (corrected: {reason_text})", memory_id),
            )
        return {"ok": True, "id": memory_id, "ref": f"exp-{memory_id}",
                "char_id": row["char_id"], "previous_text": row["text"]}

    def forget_experience(self, memory_id, *, expect_char_id):
        """Soft-delete an experience only when it is live and owned."""
        try:
            memory_id = memory_row_id(memory_id)
        except ValueError:
            return {"ok": False, "reason": "invalid_memory_id"}
        with self._conn() as conn:
            changed = conn.execute(
                "UPDATE character_experiences SET deleted = 1 "
                "WHERE id = ? AND char_id = ? AND deleted = 0",
                (memory_id, expect_char_id),
            ).rowcount
        if not changed:
            return {"ok": False, "reason": "no live experience for this character"}
        return {"ok": True, "memory_id": memory_id, "ref": f"exp-{memory_id}"}

    def set_experience_pin(self, memory_id, pinned, *, expect_char_id):
        """Pin/unpin an experience only when it is live and owned."""
        try:
            memory_id = memory_row_id(memory_id)
        except ValueError:
            return {"ok": False, "reason": "invalid_memory_id"}
        if type(pinned) is not bool:
            return {"ok": False, "reason": "pinned must be a boolean"}
        with self._conn() as conn:
            changed = conn.execute(
                "UPDATE character_experiences SET pinned = ? "
                "WHERE id = ? AND char_id = ? AND deleted = 0",
                (int(pinned), memory_id, expect_char_id),
            ).rowcount
        if not changed:
            return {"ok": False, "reason": "no live experience for this character"}
        return {"ok": True, "memory_id": memory_id,
                "ref": f"exp-{memory_id}", "pinned": pinned}

    def character_checkpoint(self, char_id):
        """Snapshot ONE character's domain-2 state (+ world decor/position).

        Only that character's rows are captured.  A checkpoint that would
        exceed ``MAX_CHECKPOINT_EXPERIENCES`` is refused rather than silently
        truncated (a truncated snapshot would retire live rows on restore).
        """
        char_id = str(char_id or "").strip()
        if not char_id:
            raise ValueError("char_id is required")
        now = _timestamp()
        with self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM character_experiences "
                "WHERE char_id = ? AND deleted = 0",
                (char_id,),
            ).fetchone()["n"]
            if int(count) > MAX_CHECKPOINT_EXPERIENCES:
                return {"ok": False, "reason": "experience_limit",
                        "detail": f"character has {int(count)} live "
                                  f"experiences; cap is "
                                  f"{MAX_CHECKPOINT_EXPERIENCES}"}
            experiences = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM character_experiences "
                    "WHERE char_id = ? AND deleted = 0 "
                    "ORDER BY tick, id LIMIT ?",
                    (char_id, MAX_CHECKPOINT_EXPERIENCES),
                ).fetchall()
            ]
        world_state = None
        with self._world_conn() as world:
            if world is not None:
                try:
                    row = world.execute(
                        "SELECT x, y, room, state, needs_json, memory_json, "
                        "dwell, goal, task_ref, blocked_reason "
                        "FROM characters WHERE id = ?",
                        (char_id,),
                    ).fetchone()
                    world_state = dict(row) if row else None
                except sqlite3.Error:
                    world_state = None
        payload = {"char_id": char_id, "created_at": now,
                   "experiences": experiences, "world": world_state}
        checkpoint_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO character_checkpoints "
                "(checkpoint_id, char_id, created_at, snapshot_json) "
                "VALUES (?, ?, ?, ?)",
                (checkpoint_id, char_id, now, json.dumps(payload)),
            )
        return {"ok": True, "checkpoint_id": checkpoint_id,
                "char_id": char_id, "created_at": now,
                "experience_count": len(experiences),
                "has_world_state": world_state is not None}

    def restore_character_checkpoint(self, char_id, checkpoint_id):
        """Restore ONE character's snapshot; never touches domain 1/config.

        Domain-2 rows outside the snapshot are SOFT-retired (deleted=1),
        snapshot rows are (re)instated, and the world ``characters`` row
        (decor/position) is put back when present.  ``memories`` (assistant
        facts), ``meta`` (provider/sim config) and everything else are never
        written — proved byte-identical by test.
        """
        char_id = str(char_id or "").strip()
        checkpoint_id = str(checkpoint_id or "").strip()
        if not char_id or not checkpoint_id:
            raise ValueError("char_id and checkpoint_id are required")
        with self._conn() as conn:
            stored = conn.execute(
                "SELECT * FROM character_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
            if stored is None:
                return {"ok": False, "reason": "unknown_checkpoint"}
            if stored["char_id"] != char_id:
                return {"ok": False, "reason": "checkpoint_char_mismatch"}
            try:
                snapshot = json.loads(stored["snapshot_json"])
            except (TypeError, ValueError):
                return {"ok": False, "reason": "corrupt_checkpoint"}
            experiences = list(snapshot.get("experiences") or [])
            snapshot_ids = {
                row.get("id") for row in experiences if row.get("id") is not None
            }
            live_rows = conn.execute(
                "SELECT id FROM character_experiences "
                "WHERE char_id = ? AND deleted = 0",
                (char_id,),
            ).fetchall()
            retire = [row["id"] for row in live_rows
                      if row["id"] not in snapshot_ids]
            if retire:
                conn.executemany(
                    "UPDATE character_experiences SET deleted = 1 WHERE id = ?",
                    [(row_id,) for row_id in retire],
                )
            restored = 0
            for row in experiences[:MAX_CHECKPOINT_EXPERIENCES]:
                conn.execute(
                    "INSERT INTO character_experiences "
                    "(char_id, tick, kind, text, epistemic, source_event_id, "
                    " created_at, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(char_id, tick, kind, text) DO UPDATE SET "
                    "deleted = 0, epistemic = excluded.epistemic, "
                    "source_event_id = excluded.source_event_id",
                    (
                        char_id,
                        int(row.get("tick") or 0),
                        str(row.get("kind") or ""),
                        str(row.get("text") or ""),
                        str(row.get("epistemic") or "observed"),
                        row.get("source_event_id"),
                        str(row.get("created_at") or _timestamp()),
                    ),
                )
                restored += 1
        world_restored = False
        world_state = snapshot.get("world")
        if isinstance(world_state, dict) and world_state:
            allowed = ("x", "y", "room", "state", "needs_json", "memory_json",
                       "dwell", "goal", "task_ref", "blocked_reason")
            fields = [name for name in allowed if name in world_state]
            if fields:
                with self._world_conn(write=True) as world:
                    if world is not None:
                        try:
                            cursor = world.execute(
                                "UPDATE characters SET "
                                + ", ".join(f"{name} = ?" for name in fields)
                                + " WHERE id = ?",
                                tuple(world_state[name] for name in fields)
                                + (char_id,),
                            )
                            world_restored = cursor.rowcount > 0
                        except sqlite3.Error:
                            world_restored = False
        return {"ok": True, "char_id": char_id,
                "checkpoint_id": checkpoint_id,
                "experiences_restored": restored,
                "experiences_retired": len(retire),
                "world_state_restored": world_restored}

    # -- domain 3: shared authorized work ---------------------------------
    def grant_artifact(self, artifact_id, *, task_id=None, char_id=None,
                       label=None, path=None):
        """Record one shared, authorized artefact (never deleted, revocable)."""
        artifact_id = str(artifact_id or "").strip()
        if not artifact_id:
            raise ValueError("artifact_id is required")
        now = _timestamp()
        label_text = redact_for_storage(str(label or "").strip())
        path_text = redact_for_storage(str(path or "").strip())
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM artifact_memory WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if existing:
                data = dict(existing)
                data["created"] = False
                return data
            conn.execute(
                "INSERT INTO artifact_memory "
                "(artifact_id, task_id, char_id, label, path, created_at, "
                " revoked) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (artifact_id, _optional_str(task_id),
                 _optional_str(char_id), label_text or None,
                 path_text or None, now),
            )
            row = conn.execute(
                "SELECT * FROM artifact_memory WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        data = dict(row)
        data["created"] = True
        return data

    def revoke_artifact(self, artifact_id):
        """Revoke an artefact with a flag (soft — the row is never deleted)."""
        artifact_id = str(artifact_id or "").strip()
        if not artifact_id:
            return {"ok": False, "reason": "invalid_artifact_id"}
        with self._conn() as conn:
            found = conn.execute(
                "SELECT 1 FROM artifact_memory WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if found is None:
                return {"ok": False, "reason": "unknown_artifact"}
            conn.execute(
                "UPDATE artifact_memory SET revoked = 1 WHERE artifact_id = ?",
                (artifact_id,),
            )
        return {"ok": True, "artifact_id": artifact_id, "revoked": True}

    def list_artifacts(self, *, include_revoked=False,
                       limit=DEFAULT_DASHBOARD_LIMIT):
        """Bounded list of artefacts (revoked hidden by default)."""
        limit = _clamp(limit, DEFAULT_DASHBOARD_LIMIT, 1, MAX_ARTIFACT_LIMIT)
        sql = "SELECT * FROM artifact_memory"
        if not include_revoked:
            sql += " WHERE revoked = 0"
        sql += " ORDER BY created_at DESC, artifact_id LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -- reads: scrapbook / journal recap / dashboard ----------------------
    def scrapbook(self, char_id, *, limit=DEFAULT_SCRAPBOOK_LIMIT):
        """A character's scrapbook built ONLY from recorded rows and events.

        Every entry is verbatim from ``character_experiences`` (live rows)
        and from the world ``events`` ledger for that actor — nothing is
        generated, summarised, or paraphrased.  Bounded by ``limit``.
        """
        char_id = str(char_id or "").strip()
        if not char_id:
            raise ValueError("char_id is required")
        limit = _clamp(limit, DEFAULT_SCRAPBOOK_LIMIT, 1, MAX_SCRAPBOOK_LIMIT)
        with self._conn() as conn:
            experiences = []
            for row in conn.execute(
                "SELECT id, char_id, tick, kind, text, epistemic, "
                "source_event_id, created_at, pinned "
                "FROM character_experiences "
                "WHERE char_id = ? AND deleted = 0 "
                "ORDER BY tick DESC, id DESC LIMIT ?",
                (char_id, limit),
            ).fetchall():
                item = dict(row)
                # LAB-3R5 typed domain reference: three tables share bare
                # integer ids, so every row carries an unambiguous ref the
                # UI/bridge can dispatch on without guessing.
                item["ref"] = "exp-%d" % int(item["id"])
                experiences.append(item)
        experiences.reverse()
        events = []
        with self._world_conn() as world:
            if world is not None:
                try:
                    for row in world.execute(
                        "SELECT id, tick, actor, kind, text FROM events "
                        "WHERE actor = ? ORDER BY id DESC LIMIT ?",
                        (char_id, limit),
                    ).fetchall():
                        item = dict(row)
                        item["ref"] = "evt-%d" % int(item["id"])
                        events.append(item)
                except sqlite3.Error:
                    events = []
        return {"char_id": char_id, "experiences": experiences,
                "events": events,
                "count": len(experiences) + len(events)}

    def journal_recap(self, date, *, limit=DEFAULT_RECAP_LIMIT, char_id=None):
        """A day's recap built ONLY from actual recorded rows/events.

        New world events use their recorded UTC time. Legacy rows without a
        timestamp are included only when a checkpoint anchors their tick.
        A character view scopes experiences and world events to that actor.
        """
        day = _validate_date(date)
        limit = _clamp(limit, DEFAULT_RECAP_LIMIT, 1, MAX_RECAP_LIMIT)
        now = _timestamp()
        with self._conn() as conn:
            facts = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM memories "
                    f"WHERE {live_memory_clause()} "
                    "AND created_at LIKE ? ORDER BY id ASC LIMIT ?",
                    (now, day + "%", limit),
                ).fetchall()
            ]
            experiences = [
                dict(row) for row in conn.execute(
                    "SELECT id, char_id, tick, kind, text, epistemic, "
                    "source_event_id, created_at FROM character_experiences "
                    "WHERE deleted = 0 AND created_at LIKE ? "
                    "AND (? IS NULL OR char_id = ?) ORDER BY tick, id LIMIT ?",
                    (day + "%", char_id, char_id, limit))
            ]
        events = []
        with self._world_conn() as world:
            if world is not None:
                try:
                    columns = {row[1] for row in world.execute("PRAGMA table_info(events)")}
                    legacy = ("tick IN (SELECT tick FROM checkpoints "
                              "WHERE date(created_at, 'unixepoch') = ?)")
                    condition, params = legacy, (day,)
                    if "created_at" in columns:
                        start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
                        condition = ("(created_at >= ? AND created_at < ?) OR "
                                     "(created_at IS NULL AND " + legacy + ")")
                        params = (start.timestamp(), (start + timedelta(days=1)).timestamp(), day)
                    events = [dict(row) for row in world.execute(
                        f"SELECT id, tick, actor, kind, text FROM events WHERE ({condition}) "
                        "AND (? IS NULL OR actor = ?) ORDER BY id DESC LIMIT ?",
                        (*params, char_id, char_id, limit))]
                    events.reverse()
                except sqlite3.Error:
                    events = []
        return {"date": day, "facts": facts, "experiences": experiences,
                "events": events}

    def dashboard_projection(self, *, limit=DEFAULT_DASHBOARD_LIMIT):
        """Read-only Notion-style projection of domains 1-4 (plain dicts).

        This function has NO write path: it issues SELECTs only (proved by
        test).  It shows live rows only — forgotten, superseded and faded rows
        are excluded — and is the assistant's own full view (liveness-
        filtered, not permission-filtered).  Every section is capped.
        """
        limit = _clamp(limit, DEFAULT_DASHBOARD_LIMIT, 1, MAX_DASHBOARD_LIMIT)
        now = _timestamp()
        with self._conn() as conn:
            facts = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM memories "
                    f"WHERE {live_memory_clause()} "
                    "ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (now, limit),
                ).fetchall()
            ]
            experiences = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, char_id, tick, kind, text, epistemic, "
                    "source_event_id, created_at "
                    "FROM character_experiences WHERE deleted = 0 "
                    "ORDER BY tick DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
            artifacts = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM artifact_memory WHERE revoked = 0 "
                    "ORDER BY created_at DESC, artifact_id LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        world_events = []
        with self._world_conn() as world:
            if world is not None:
                try:
                    world_events = [
                        dict(row)
                        for row in world.execute(
                            "SELECT id, tick, actor, kind, text FROM events "
                            "ORDER BY id DESC LIMIT ?",
                            (limit,),
                        ).fetchall()
                    ]
                except sqlite3.Error:
                    world_events = []
        return {
            "generated_at": _timestamp(),
            "assistant_facts": facts,
            "character_experiences": experiences,
            "artifacts": artifacts,
            "world_events": world_events,
        }






# ---------------------------------------------------------------------------
# Process singleton (ROUND LAB-3). Same pytest-guard pattern as
# lab_authority/lab_budget/lab_artifacts: under pytest with no explicit
# configure, the singleton returns None (tests set configure explicitly),
# and a non-pytest bridge call degrades to an honest "memory is not
# configured" state instead of silently opening a store. The default
# memory_db is IN-SCOPE (NOT ~/.friday) and the default world_db is the
# lab's own world.db (never the forbidden default).
# ---------------------------------------------------------------------------
_shared = None
_shared_memory_db = None
_shared_world_db = None
_shared_lock = threading.RLock()


def configure(memory_db, world_db=None, *, prefer_fts5=True) -> LabMemory:
    """Bind the singleton to EXPLICIT paths (tests/harnesses only)."""
    global _shared, _shared_memory_db, _shared_world_db
    with _shared_lock:
        _shared_memory_db = str(memory_db)
        _shared_world_db = str(world_db) if world_db else None
        _shared = None
    return shared()


def shared(memory_db=None, world_db=None, *, prefer_fts5=True
           ) -> Optional[LabMemory]:
    """The one LabMemory for this process, opened lazily.

    `memory_db=None` derives a path from the lab store's parent directory
    (in-scope; NOT `~/.friday`). Under pytest with no explicit `configure()`,
    returns None: the suite must never open a memory database by accident.
    """
    global _shared, _shared_memory_db, _shared_world_db
    if _shared is not None:
        return _shared
    import sys as _sys
    if _shared_memory_db is not None:
        target_memory_db = _shared_memory_db
        target_world_db = _shared_world_db
    elif "pytest" in _sys.modules:
        return None                      # never open memory in the suite
    else:
        try:
            from . import lab_state
            _parent = Path(lab_state.db_path()).parent
            target_memory_db = str(_parent / "lab_character_memory.db")
            from .world.store import default_path as _dp
            target_world_db = str(_dp())
        except Exception:
            return None
    try:
        lab_memory = LabMemory(target_memory_db, world_db=target_world_db,
                               prefer_fts5=True)
    except Exception:
        return None
    with _shared_lock:
        if _shared is None:
            _shared = lab_memory
    return _shared


def reset_shared() -> None:
    global _shared, _shared_memory_db, _shared_world_db
    with _shared_lock:
        _shared = None
        _shared_memory_db = None
        _shared_world_db = None
