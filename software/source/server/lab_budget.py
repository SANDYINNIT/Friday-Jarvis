"""Background-life budgets: durable settings, usage ledger, admission caps.

WHY THIS EXISTS (Round LAB-2C)
------------------------------
Budgets used to live in `process-local` dicts inside `lab_learning` (the
handoff's own docstring says "caps are process-local on purpose"), so a restart
silently reset every cap and every debate round-bound. This module owns the
DURABLE state:

* settings  -> one additive key per bucket + per-character overrides in the
               lab's own SQLite store (`world.db` meta, purely additive).
* usage     -> a NEW additive table `lab_usage`: one row per attempt, keyed by
               a stable `attempt` id (task+kind+attempt) so a repeated event or
               a restart can never double-count.
* consumers -> `lab_state.execute_task` checks the cap BEFORE any queue
               admission, refunds nothing (no cancel/refund promise), and
               records attempts through `record_from_note` (the same honest
               events the executor emits).

EVERY number here stays a rubric-free setting: no invented prices and never a
$0 display for unknown costs. Paid unattended life is OFF by default
(`enabled: false`) until the user configures and opts in; motion/needs are pure
procedural ticks and cost zero model calls. Live user work keeps priority: the
budget gates background admissions only, and never touches a live turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

BUCKET_LIFE = "life"
BUCKET_USER = "user_work"
# Buckets THAT ARE REAL: only `life` can have a budget (`user_work` is
# the user-assigned real work, recorded for accounting but never budget-gated).
BUCKETS = (BUCKET_LIFE, BUCKET_USER)

# Lanes/features that exist in code but have NO usage producer yet. Honesty
# contract: listed here until a real producer wires in (no phantom rows).
NOT_YET_WIRED = (
    "embeddings", "life_memory", "life_dialogue", "life_summary",
    "peer_tutor", "classroom", "vision", "voice", "listen",
)

_CAP_KEYS = ("max_model_calls_day", "max_requests_day", "max_tokens_day",
             "max_cost_day")
_DEFAULT_SETTINGS = {
    "enabled": False,                  # paid unattended life stays OFF
    "reset_period": "daily",
    "reset_tz": "UTC",
    "updated_at": 0.0,
}


def attempt_key(task_id, kind, attempt=None, extra="") -> str:
    """Stable per-attempt identity, deduped by UNIQUE(attempt) in the table."""
    raw = "%s|%s|%s|%s" % (str(task_id or ""), str(kind or ""),
                           str(attempt if attempt is not None else ""), str(extra))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


def _is_pytest() -> bool:
    import sys
    return "pytest" in sys.modules


class LabBudget:
    """Durable background budgets + one usage ledger. Never raises on read."""

    def __init__(self, conn, lock: Optional[threading.RLock] = None):
        self._conn = conn
        self._lock = lock or threading.RLock()
        # Row-factory defensive: callers may hand in a raw sqlite connection
        # (isolated harnesses) without the Store's factory set up.
        try:
            self._conn.row_factory = sqlite3.Row
        except Exception:
            pass
        self.init_schema()

    # -- schema ------------------------------------------------------------
    def record_from_note(self, kind: str, data: Optional[Dict[str, Any]] = None,
                         at: Optional[float] = None) -> Dict[str, Any]:
        """The method `lab_state.note` calls: turn one real event into the
        durable ledger (idempotent per attempt; see `_apply_note_to_ledger`)."""
        try:
            return _apply_note_to_ledger(self, str(kind or ""),
                                         data if isinstance(data, dict) else {},
                                         at)
        except Exception as error:
            return {"ok": False, "reason": "%s: %s"
                    % (type(error).__name__, error)}

    def init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta "
                "(key TEXT PRIMARY KEY, value TEXT)")
            # Additive table: one row per attempt, UNIQUE(attempt) makes a
            # repeated event or a restart double-record impossible.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS lab_usage ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " attempt TEXT UNIQUE NOT NULL,"
                " at REAL NOT NULL,"
                " bucket TEXT NOT NULL,"
                " char_id TEXT, task_id TEXT, provider TEXT, model TEXT,"
                " category TEXT, ok INTEGER, error TEXT,"
                " tokens_in INTEGER, tokens_out INTEGER, cached INTEGER,"
                " cost_estimate REAL, price_known INTEGER NOT NULL DEFAULT 0)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS lab_usage_bucket_at "
                "ON lab_usage (bucket, at)")
            self._conn.commit()

    # -- meta helpers (same contract as lab_authority) ---------------------
    def _get(self, key: str, default: str = "") -> str:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default
        except Exception:
            return default

    def _set(self, key: str, value: str) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    (str(key), str(value)))
                self._conn.commit()
        except Exception:
            pass

    # -- settings ----------------------------------------------------------
    def set_bucket_settings(self, bucket: str, *, enabled: bool = None,
                            max_model_calls_day: int = None,
                            max_requests_day: int = None,
                            max_tokens_day: int = None,
                            max_cost_day: float = None,
                            reset_period: str = None,
                            reset_tz: str = None) -> Dict[str, Any]:
        bucket = str(bucket or "")
        if bucket not in BUCKETS:
            return {"ok": False, "reason": "unknown bucket %r" % bucket,
                    "buckets": list(BUCKETS)}
        current = self.settings(bucket, raw=True)
        updates = {}
        if enabled is not None:
            updates["enabled"] = bool(enabled)
        for key, value in (("max_model_calls_day", max_model_calls_day),
                           ("max_requests_day", max_requests_day),
                           ("max_tokens_day", max_tokens_day),
                           ("max_cost_day", max_cost_day)):
            if value is None:
                continue
            try:
                number = float(value)
            except Exception:
                return {"ok": False, "reason": "%s must be a number" % key}
            if number < 0:
                return {"ok": False, "reason": "%s must be >= 0" % key}
            updates[key] = number
        if reset_period is not None:
            if str(reset_period) != "daily":
                return {"ok": False, "reason": "reset_period must be 'daily'",
                        "accepted": ["daily"]}
            updates["reset_period"] = "daily"
        if reset_tz is not None:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(str(reset_tz))
            except Exception as error:
                return {"ok": False,
                        "reason": "unknown timezone %r (%s)" % (reset_tz,
                                                                error.__class__.__name__)}
            updates["reset_tz"] = str(reset_tz)
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(current)
        merged.update(updates)
        merged["updated_at"] = round(time.time(), 3)
        self._set("budget_%s" % bucket, json.dumps(merged))
        self._bump_rev()
        return {"ok": True, "bucket": bucket,
                "settings": self.settings(bucket, raw=False),
                "rev": self.rev()}

    def set_char_budget(self, char_id: str, *, bucket: str = BUCKET_LIFE,
                        max_model_calls_day: int = None,
                        max_requests_day: int = None) -> Dict[str, Any]:
        """Per-character override: the TIGHTER limit of bucket vs char wins."""
        char_id = str(char_id or "")
        if not char_id:
            return {"ok": False, "reason": "empty char_id"}
        if bucket not in BUCKETS:
            return {"ok": False, "reason": "unknown bucket %r" % bucket}
        current = self.char_overrides(char_id)
        updates = {}
        for key, value in (("max_model_calls_day", max_model_calls_day),
                           ("max_requests_day", max_requests_day)):
            if value is None:
                continue
            try:
                number = float(value)
            except Exception:
                return {"ok": False, "reason": "%s must be a number" % key}
            if number < 0:
                return {"ok": False, "reason": "%s must be >= 0" % key}
            updates[key] = number
        merged = dict(current)
        merged.update(updates)
        merged["updated_at"] = round(time.time(), 3)
        self._set("budget_char_%s" % char_id, json.dumps(merged))
        self._bump_rev()
        return {"ok": True, "char_id": char_id,
                "overrides": self.char_overrides(char_id), "rev": self.rev()}

    def _period_window(self, settings: Dict[str, Any], now: Optional[float] = None
                       ) -> tuple:
        """(period_start, period_end) in UTC epoch, or (0.0, inf) if unset.

        Timezone-aware semantics (the user's C-brief): the reset boundary is
        computed in the bucket's configured timezone once (daily by default).
        Failures degrade to an unbounded window rather than fake limits.
        """
        tz_name = str((settings or {}).get("reset_tz") or "UTC")
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(tz_name)
        except Exception:
            return 0.0, float("inf")
        now_value = float(now if now is not None else time.time())
        try:
            import datetime as _dt
            local = _dt.datetime.fromtimestamp(now_value, zone)
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            start = midnight.timestamp()
            return start, float("inf")
        except Exception:
            return 0.0, float("inf")

    def settings(self, bucket: str, raw: bool = False) -> Dict[str, Any]:
        if bucket not in BUCKETS:
            return {}
        try:
            payload = json.loads(self._get("budget_%s" % bucket, "") or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if raw:
            return payload
        view = dict(_DEFAULT_SETTINGS)
        view.update(payload)
        # Unknown-price honesty: a max_cost_day without any known price is
        # surfaced as unmeasurable, never as 0.
        view["price_known"] = False          # no provider pricing integrated
        view["cost_unknown"] = view.get("max_cost_day") is not None and \
            not view["price_known"]
        return view

    def char_overrides(self, char_id: str, bucket: str = BUCKET_LIFE
                       ) -> Dict[str, Any]:
        try:
            payload = json.loads(
                self._get("budget_char_%s" % str(char_id or ""), "") or "{}")
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def effective_limits(self, bucket: str, char_id: str = "",
                         role: str = "") -> Dict[str, Any]:
        """Tighter of the bucket settings and the character override.

        `role` honours the accepted design: the LEADER/assistant duty is
        never budget-limited by background life settings.
        """
        settings = self.settings(bucket)
        overrides = (self.char_overrides(char_id, bucket)
                     if char_id else {})
        limits = {}
        for key in _CAP_KEYS:
            if key in settings and settings.get(key) is not None:
                limits[key] = settings.get(key)
            if key in overrides and overrides.get(key) is not None:
                current = limits.get(key)
                if current is None or overrides[key] < current:
                    limits[key] = overrides[key]
        return {"bucket": bucket, "char_id": str(char_id or ""),
                "role": str(role or ""), "enabled": settings["enabled"],
                "limits": limits,
                "cost_unknown": settings.get("cost_unknown", False),
                "price_known": settings.get("price_known", False)}

    # -- shortage detection -------------------------------------------------
    def check(self, bucket: str, char_id: str = "", role: str = "",
              pending_reserved: int = 0) -> Dict[str, Any]:
        """Admission gate BEFORE any queue work.

        Honest limit semantics (never a fake), round LAB-2C:
        * `max_requests_day` is enforceable NOW (an admission reserves one
          slot; `pending_reserved` is the count the caller has already
          reserved for this task during its pending window);
        * `max_model_calls_day` uses the measured row count;
        * `max_tokens_day` is only actionable if rows carry REAL usage; with
          unknown/absent pricing a token cap is reported UNMEASURABLE, never
          treated as exhausted;
        * `max_cost_day` without a known price is an explicit refusal to
          enforce a fake $0 (the user decides: use request/token caps, or
          integrate real pricing later).
        The LEADER role (the live assistant) is exempt: user work keeps
        priority and its availability is never gated by background budgets.
        """
        settings = self.settings(bucket)
        limits = self.effective_limits(bucket, char_id, role)
        if role == "leader":
            return {"ok": True, "exempt": True,
                    "reason": "live assistant duty is never budget-limited",
                    "limits": limits["limits"]}
        if not settings.get("enabled"):
            # Paid unattended life stays OFF until configured. Honest refusal.
            return {"ok": False, "reason": "background budget not enabled",
                    "detail": "the user must enable the budget to opt in",
                    "enabled": False, "limits": limits["limits"]}
        start, _end = self._period_window(settings)
        used = self.used(bucket, since=start)
        refusals = []
        max_requests = limits["limits"].get("max_requests_day")
        if max_requests is not None and \
                used.get("attempts", 0) + max(0, int(pending_reserved or 0)) + 1 \
                > max_requests:
            refusals.append("daily request cap reached (%d)" % max_requests)
        max_calls = limits["limits"].get("max_model_calls_day")
        if max_calls is not None and used.get("model_calls", 0) >= max_calls:
            refusals.append("daily model-call cap reached (%d)" % max_calls)
        max_tokens = limits["limits"].get("max_tokens_day")
        token_unmeasurable = max_tokens is not None and \
            not used.get("token_cap_actionable")
        if max_tokens is not None and not token_unmeasurable and \
                used.get("tokens_total", 0) > max_tokens:
            refusals.append("daily token cap reached (%d)" % max_tokens)
        max_cost = limits["limits"].get("max_cost_day")
        if max_cost is not None and not settings.get("price_known"):
            refusals.append(
                "cost cap is set but the price is UNKNOWN - refusing to "
                "enforce a fake $0; set request/model/token caps instead or "
                "integrate real provider pricing first")
        remaining = {}
        if max_requests is not None:
            remaining["requests"] = max(0, int(max_requests) -
                                        int(used.get("attempts", 0)))
        if max_calls is not None:
            remaining["model_calls"] = max(0, int(max_calls) -
                                           int(used.get("model_calls", 0)))
        if max_tokens is not None:
            remaining["tokens"] = max(0, int(max_tokens) -
                                      int(used.get("tokens_total", 0)))
        result = {"enabled": True, "limits": limits["limits"],
                  "remaining": remaining, "used": used,
                  "token_unmeasurable": token_unmeasurable,
                  "price_known": settings.get("price_known", False)}
        if refusals:
            result.update({"ok": False, "reason": "; ".join(refusals)})
        else:
            result.update({"ok": True})
        return result

    # -- usage ledger -------------------------------------------------------
    def try_life_enabled(self):
        """Do not make the Town ticker wait behind a budget reservation."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self.settings("life").get("enabled")
        finally:
            self._lock.release()

    def reserve_town(self, attempt, char_id, provider, model, *, daily_total, daily_character):
        """Atomically count pending and finished Life reservations before dispatch."""
        with self._lock:
            started = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                started = True
                settings = self.settings("life")
                check = self.check("life", char_id, role="worker")
                if not check.get("ok"):
                    return "budget_exhausted" if settings["enabled"] else "paused"
                if check.get("used", {}).get("error"):
                    return "unavailable"
                start, _ = self._period_window(settings)
                total, personal = self._conn.execute(
                    """SELECT count(*),count(CASE WHEN char_id=? THEN 1 END)
                       FROM lab_usage WHERE bucket='life' AND at>=?""", (char_id, start)).fetchone()
                limit = check["limits"].get("max_model_calls_day")
                if (total >= daily_total or personal >= daily_character
                        or (limit is not None and total >= limit)):
                    return "budget_exhausted"
                self._conn.execute("""INSERT INTO lab_usage
                    (attempt,at,bucket,char_id,provider,model,category,ok,error,
                     tokens_in,tokens_out,cached,cost_estimate,price_known,task_id)
                    VALUES (?,?, 'life',?,?,?,'town_plan',NULL,'',0,0,0,NULL,0,'')""",
                    (attempt, time.time(), char_id, provider, model))
                self._conn.commit()
                self._bump_rev()
                return "reserved"
            except Exception:
                return "unavailable"
            finally:
                if started and self._conn.in_transaction:
                    self._conn.rollback()

    def record(self, bucket: str, char_id: str = "", task_id: str = "",
               attempt: Optional[str] = None, provider: str = "",
               model: str = "", category: str = "", ok: Optional[bool] = None,
               error: str = "", tokens_in: int = None, tokens_out: int = None,
               cached: bool = False, at: Optional[float] = None,
               kind: str = "") -> Dict[str, Any]:
        """One attempt row; idempotent per `attempt` (UNIQUE). `at` exists so
        tests can freeze the clock deterministically."""
        bucket = str(bucket or "")
        if bucket not in BUCKETS:
            return {"ok": False, "reason": "unknown bucket %r" % bucket,
                    "buckets": list(BUCKETS)}
        attempt = str(attempt or attempt_key(task_id, kind_of(kind or category),
                                             extra=provider))
        started = float(at if at is not None else time.time())
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO lab_usage (attempt, at, bucket,"
                    " char_id, task_id, provider, model, category, ok, error,"
                    " tokens_in, tokens_out, cached, cost_estimate,"
                    " price_known)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (attempt, started, bucket, str(char_id or ""),
                     str(task_id or ""), str(provider or ""),
                     str(model or ""), str(category or ""),
                     None if ok is None else int(bool(ok)),
                     str(error or "")[:200],
                     int(tokens_in or 0), int(tokens_out or 0),
                     1 if cached else 0,
                     None,                      # estimate: unknown, never 0
                     0))                        # price_known: false
                self._conn.commit()
            inserted = bool(cursor and cursor.rowcount)
        except Exception as error:
            return {"ok": False, "reason": "%s: %s"
                    % (type(error).__name__, error)}
        rev = self._bump_rev()
        return {"ok": True, "attempt": attempt, "recorded": inserted,
                "rev": rev}                     # recorded=False == dedup hit

    def update_outcome(self, attempt: str, *, ok: bool = None, error: str = "",
                       tokens_in: int = None, tokens_out: int = None,
                       cached: bool = None, at: Optional[float] = None, dispatched=None
                       ) -> Dict[str, Any]:
        """Fill the real outcome into an existing attempt row (dedup-safe)."""
        attempt = str(attempt or "")
        if not attempt:
            return {"ok": False, "reason": "attempt required"}
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT id FROM lab_usage WHERE attempt=?",
                    (attempt,)).fetchone()
                if row is None:
                    return {"ok": False, "reason": "unknown attempt %s"
                                                  % attempt}
                sets, params = [], []
                if ok is not None:
                    sets.append("ok=?"); params.append(int(bool(ok)))
                if error:
                    sets.append("error=?"); params.append(str(error)[:200])
                if tokens_in is not None:
                    sets.append("tokens_in=?"); params.append(int(tokens_in or 0))
                if tokens_out is not None:
                    sets.append("tokens_out=?"); params.append(int(tokens_out or 0))
                if cached is not None:
                    sets.append("cached=?"); params.append(1 if cached else 0)
                if dispatched is False:
                    sets.append("category=CASE WHEN category='town_plan' THEN 'town_not_sent' ELSE category END")
                if not sets:
                    return {"ok": True, "recorded": False,
                            "reason": "nothing to update"}
                params.append(attempt)
                self._conn.execute(
                    "UPDATE lab_usage SET %s WHERE attempt=?" % ", ".join(sets),
                    tuple(params))
                self._conn.commit()
        except Exception as error:
            return {"ok": False, "reason": "%s: %s"
                    % (type(error).__name__, error)}
        return {"ok": True, "attempt": attempt, "rev": self._bump_rev()}

    def used(self, bucket: str, since: float = 0.0) -> Dict[str, Any]:
        """Bounded, real totals within a period. `price_known` is the actual
        truth of whether ANY provider pricing is integrated (never inferred);
        `cost_estimate` stays None when unknown (never a fake 0)."""
        bucket = str(bucket or "")
        try:
            with self._lock:
                rows = list(self._conn.execute(
                    "SELECT ok, tokens_in, tokens_out, cached, category FROM lab_usage"
                    " WHERE bucket=? AND at >= ?", (bucket, float(since))))
        except Exception as error:
            return {"error": type(error).__name__,
                    "attempts": 0, "model_calls": 0, "outcomes": {},
                    "tokens_in": 0, "tokens_out": 0, "tokens_total": 0,
                    "token_cap_actionable": False,
                    "price_known": False, "cost_estimate": None,
                    "cost_unknown": True}
        attempts = 0
        outcomes = {}
        calls = 0                      # one attempt => one provider call
        tokens_in_total = 0
        tokens_out_total = 0
        measured_tokens = 0            # rows that carry actual usage numbers
        for row in rows:
            attempts += 1
            outcome_key = {None: "pending", 1: "completed", 0: "failed"}[row["ok"]]
            outcomes[outcome_key] = outcomes.get(outcome_key, 0) + 1
            if row["ok"] is not None and row["category"] != "town_not_sent":
                calls += 1
            tokens_in_total += int(row["tokens_in"] or 0)
            tokens_out_total += int(row["tokens_out"] or 0)
            measured_tokens += (1 if (row["tokens_in"] or row["tokens_out"])
                                else 0)
        tokens_total = tokens_in_total + tokens_out_total
        return {
            "bucket": bucket,
            "attempts": attempts,
            "model_calls": calls,
            "outcomes": outcomes,
            "tokens_in": tokens_in_total,
            "tokens_out": tokens_out_total,
            "tokens_total": tokens_total,
            "measured_token_rows": measured_tokens,
            # request caps are enforceable regardless of measured tokens
            "token_cap_actionable": measured_tokens > 0,
            "price_known": False,        # no provider pricing integrated yet
            "cost_estimate": None,       # never invent one
            "cost_unknown": True,
        }

    # -- revision ------------------------------------------------------------
    def rev(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='budget_rev'").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _bump_rev(self) -> int:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO meta (key,value) VALUES ('budget_rev','1') "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")
                self._conn.commit()
        except Exception:
            pass
        return self.rev()


def kind_of(category: str) -> str:
    """Canonical event kind -> budget record category."""
    mapping = {
        "task_completed": "task",
        "task_failed": "task",
        "task_retry": "attempt",
        "task_paused": "task",
        "task_started": "admission",
        "task_cancel": "task",
    }
    return mapping.get(str(category or ""), str(category or ""))


# ---------------------------------------------------------------------------
# Process singleton (same pytest guard as lab_authority under suite runs).
# ---------------------------------------------------------------------------
_shared: Optional[LabBudget] = None
_shared_path = None
_shared_lock = threading.RLock()


def configure(path) -> LabBudget:
    """Bind the singleton to an EXPLICIT path (tests/harnesses only)."""
    global _shared, _shared_path
    with _shared_lock:
        _shared_path = str(path)
        _shared = None
    return shared()


def shared(path=None) -> Optional[LabBudget]:
    """The one LabBudget for this process, opened lazily.

    `path=None` derives the store from `lab_state.db_path()` (honours
    `FRIDAY_WORLD_DB`). Under pytest with no explicit `configure()`, this
    returns **None**: the suite must never open the real store, and every
    budget-aware caller degrades honestly to a no-op.
    """
    global _shared, _shared_path
    if path is not None:
        with _shared_lock:
            key = str(path)
            if _shared is None or _shared_path != key:
                _shared = None
                _shared_path = key
    if _shared is None:
        import sys as _sys
        if "pytest" in _sys.modules and not _shared_path:
            return None
        target = _shared_path
        if target is None:
            try:
                from . import lab_state
                target = lab_state.db_path()
            except Exception:
                return None
        try:
            import sqlite3
            conn = sqlite3.connect(str(target), check_same_thread=False,
                                   isolation_level=None, timeout=5.0)
            budget = LabBudget(conn)
        except Exception:
            return None
        with _shared_lock:
            if _shared is None:
                _shared = budget
    return _shared


def reset_shared() -> None:
    global _shared, _shared_path
    with _shared_lock:
        _shared = None
        _shared_path = None


# Map of the executors' real event kinds to honest attempt identities.
# `task_pending` is the retry window; retries reuse the SAME attempt row so
# repeated notes can never double-count a single in-flight run.
_ATTEMPT_KINDS = ("task_started", "task_completed", "task_failed",
                  "task_retry", "task_paused", "task_cancel")


def _apply_note_to_ledger(budget, kind: str, data: Dict[str, Any],
                          at: Optional[float] = None) -> Dict[str, Any]:
    """Adapter: lab_state.note events -> durable budget rows (module helper).

    `bucket` comes from the event itself (`task_note_bucket`, LAB-2C): a real
    the user-assigned worker attempt is `user_work` (accounted, never gated);
    a background-life attempt is `life` (accounted AND budget-gated).

    Dedup rule: each (task_id, attempt#, kind) collapses to one row. The FIRST
    seen combo creates the pending row; completing/failing RE-USES it by
    `update_outcome`. `task_paused`/`task_cancel` mark the outcome without an
    extra attempt (no double count on retries/outcome ordering).
    """
    if budget is None:
        return {"ok": False, "reason": "budget not configured"}
    data = data if isinstance(data, dict) else {}
    task_id = str((data or {}).get("task") or "")
    if not task_id:
        return {"ok": False, "reason": "no task"}
    kind = str(kind or "")
    if kind not in _ATTEMPT_KINDS:
        return {"ok": False, "reason": "not a usable event kind",
                "kinds": list(_ATTEMPT_KINDS)}
    bucket = str((data or {}).get("bucket") or BUCKET_USER)
    if bucket not in BUCKETS:
        bucket = BUCKET_USER
    provider = str((data or {}).get("provider") or "")
    model = str((data or {}).get("model") or "")
    has_tokens = ("tokens_in" in data or "tokens_out" in data)
    attempt_n = (data or {}).get("attempt")
    step = (data or {}).get("steps")
    tokens_in = (data or {}).get("tokens_in")
    tokens_out = (data or {}).get("tokens_out")
    holder = str((data or {}).get("agent") or "")

    if kind == "task_started":
        attempt = attempt_key(task_id, "admission", attempt=attempt_n,
                              extra="lab")
        return budget.record(bucket, char_id=holder, task_id=task_id,
                             attempt=attempt, provider=provider, model=model,
                             category="admission", ok=None, error="",
                             tokens_in=None, tokens_out=None, at=at)
    if kind == "task_retry":
        attempt = attempt_key(task_id, "retry", attempt=attempt_n,
                              extra=provider)
        budget.record(bucket, char_id=holder, task_id=task_id,
                      attempt=attempt, provider=provider, model=model,
                      category="retry", ok=None,
                      error=str((data or {}).get("error") or ""),
                      tokens_in=None, tokens_out=None, at=at)
        return budget.update_outcome(
            attempt, ok=None, error=str((data or {}).get("error") or ""),
            tokens_in=None, tokens_out=None)

    # completion paths
    outcome_ok = None
    if kind == "task_completed":
        outcome_ok = True
    elif kind in ("task_failed", "task_paused", "task_cancel"):
        outcome_ok = False

    attempt = attempt_key(task_id, "final", attempt=attempt_n if attempt_n is not None
                          else step, extra=provider)
    budget.record(bucket, char_id=holder, task_id=task_id, attempt=attempt,
                  provider=provider, model=model, category=kind_of(kind),
                  ok=None, error=str((data or {}).get("reason") or ""),
                  tokens_in=None, tokens_out=None, at=at)
    return budget.update_outcome(
        attempt, ok=outcome_ok,
        error=str((data or {}).get("reason") or ""),
        tokens_in=tokens_in if has_tokens else None,
        tokens_out=tokens_out if has_tokens else None)
