"""Lab learning: practice, tutoring, debate, validation and skill cards.

WHAT "LEARNING" MEANS HERE
-------------------------
Memory, retrieval and procedure adaptation through practice and feedback.
It is deliberately NOT model training: this module never modifies weights,
never fine-tunes, never uploads a dataset, and exposes no entry point that
could do so (a test pins that: no public attribute matches
``train|fine_tune|finetune|upload_dataset``).

SKILL CARDS ARE TEXT, NOT CODE
------------------------------
The existing skill mechanism in ``profiles/default.py`` writes ``./skills/*.py``
and ``exec``s it. That is a different, executable system and it is untouched
here. A skill card in this module is a row of TEXT fields (when it applies,
the procedure, limits, evidence, checks, provenance) that nothing in this
file ever runs, imports or evaluates - retrieval only.

STORAGE
-------
One SQLite file, path always supplied by the caller (never a hardcoded path,
never a home-state location). Every table is created with ``CREATE TABLE IF
NOT EXISTS`` so ``init_schema()`` is idempotent and ``__init__`` performs no
I/O at all - the connection is opened lazily on first use.

IDs are opaque ``prefix-hex8`` strings (see ``_new_id``), every return value is
JSON-safe, and every list output is capped at ``LIST_CAP``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

# Hard ceiling on ANY list this module returns, whatever the caller asks for.
LIST_CAP = 100

# Debate rounds by default; a debate that hits the cap without agreement is
# disputed - never silently "agreed".
DEFAULT_DEBATE_ROUNDS = 3

# Validation attempts allowed before a goal without evidence becomes disputed.
DEFAULT_VALIDATION_CAP = 3

USAGE_BUCKETS = ("life", "practice", "user_work")
CAP_METRICS = ("calls", "tokens", "cost")

GOAL_STATES = ("open", "practising", "validating", "promoted", "disputed",
               "rolled_back")

# Goal lifecycle. `promoted` additionally requires a passing validation run
# (checked separately, see `_can_promote`); `rolled_back` only via rollback().
GOAL_TRANSITIONS = {
    "open": {"practising", "validating", "disputed", "promoted"},
    "practising": {"validating", "disputed", "promoted"},
    "validating": {"promoted", "disputed", "rolled_back"},
    "promoted": {"rolled_back"},
    # `rolled_back -> promoted` exists ONLY so the promotion gate can say
    # why re-promotion was refused (a new validation run is required); it is
    # never a free pass - see `_can_promote`.
    "rolled_back": {"validating", "disputed", "promoted"},
    "disputed": {"validating", "promoted"},
}

# Flags that block a character from recorded class attendance.
ATTENDANCE_BLOCKING_FLAGS = ("npc", "manual")

# Every column of every mandated table, in order. Used by the schema test and
# by `_row` so a row can never leak a non-JSON-safe value.
SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS learning_goals (
        id TEXT PRIMARY KEY,
        text TEXT,
        success_criteria TEXT,
        held_out_case_ids TEXT,
        baseline_ref TEXT,
        state TEXT,
        created_at REAL,
        updated_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS exercises (
        id TEXT PRIMARY KEY,
        goal_id TEXT,
        prompt TEXT,
        reference_answer TEXT,
        held_out INTEGER,
        created_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS practice_runs (
        id TEXT PRIMARY KEY,
        goal_id TEXT,
        char_id TEXT,
        exercise_id TEXT,
        answer TEXT,
        correct INTEGER,
        latency_ms INTEGER,
        model_calls INTEGER,
        provider TEXT,
        model TEXT,
        at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS debate_sessions (
        id TEXT PRIMARY KEY,
        goal_id TEXT,
        round INTEGER,
        a_char TEXT,
        b_char TEXT,
        transcript TEXT,
        state TEXT,
        at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS validation_runs (
        id TEXT PRIMARY KEY,
        goal_id TEXT,
        skill_id TEXT,
        held_out_score REAL,
        baseline_score REAL,
        regression_pass INTEGER,
        evidence TEXT,
        at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS skill_cards (
        id TEXT PRIMARY KEY,
        version INTEGER,
        name TEXT,
        when_applicable TEXT,
        procedure TEXT,
        limitations TEXT,
        evidence TEXT,
        checks TEXT,
        provenance TEXT,
        state TEXT,
        created_at REAL,
        promoted_at REAL,
        rolled_back_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS skill_outcomes (
        id TEXT PRIMARY KEY,
        skill_id TEXT,
        task_fingerprint TEXT,
        ok INTEGER,
        latency_ms INTEGER,
        model_calls INTEGER,
        at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS classes (
        id TEXT PRIMARY KEY,
        skill_id TEXT,
        goal_id TEXT,
        teacher TEXT,
        members TEXT,
        state TEXT,
        created_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS class_sessions (
        id TEXT PRIMARY KEY,
        class_id TEXT,
        char_id TEXT,
        question TEXT,
        answer TEXT,
        attempt INTEGER,
        gap TEXT,
        joined_late INTEGER,
        at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS usage_ledger (
        id TEXT PRIMARY KEY,
        bucket TEXT,
        char_id TEXT,
        calls INTEGER,
        model_calls INTEGER,
        tokens_in INTEGER,
        tokens_out INTEGER,
        cost_estimate REAL,
        price_known INTEGER,
        at REAL
    )""",
)

# Column list per table (mirrors SCHEMA_STATEMENTS; kept explicit so the
# schema test can assert the two never drift apart).
TABLE_COLUMNS = {
    "learning_goals": ("id", "text", "success_criteria", "held_out_case_ids",
                       "baseline_ref", "state", "created_at", "updated_at"),
    "exercises": ("id", "goal_id", "prompt", "reference_answer", "held_out",
                  "created_at"),
    "practice_runs": ("id", "goal_id", "char_id", "exercise_id", "answer",
                      "correct", "latency_ms", "model_calls", "provider",
                      "model", "at"),
    "debate_sessions": ("id", "goal_id", "round", "a_char", "b_char",
                        "transcript", "state", "at"),
    "validation_runs": ("id", "goal_id", "skill_id", "held_out_score",
                        "baseline_score", "regression_pass", "evidence", "at"),
    "skill_cards": ("id", "version", "name", "when_applicable", "procedure",
                    "limitations", "evidence", "checks", "provenance", "state",
                    "created_at", "promoted_at", "rolled_back_at"),
    "skill_outcomes": ("id", "skill_id", "task_fingerprint", "ok",
                       "latency_ms", "model_calls", "at"),
    "classes": ("id", "skill_id", "goal_id", "teacher", "members", "state",
                "created_at"),
    "class_sessions": ("id", "class_id", "char_id", "question", "answer",
                       "attempt", "gap", "joined_late", "at"),
    "usage_ledger": ("id", "bucket", "char_id", "calls", "model_calls",
                     "tokens_in", "tokens_out", "cost_estimate",
                     "price_known", "at"),
}


# ---------------------------------------------------------------------------
# small pure helpers (roster.py conventions: prefix-hex8 ids, JSON-safe rows)
# ---------------------------------------------------------------------------
def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:8])


def _now() -> float:
    return time.time()


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def _as_bool(value) -> bool:
    """Truthiness for outcomes: bools, 0/1, yes/no/pass/fail, ok-dicts."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, dict):
        for key in ("ok", "correct", "passed", "met"):
            if key in value:
                return _as_bool(value.get(key))
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "pass",
                                         "passed", "ok", "met")
    return False


def _case_outcome(value):
    """Verdict for ONE held-out case, or None when it carries none.

    Accepted shapes: a plain bool/0-1, a dict with an explicit verdict key
    (`ok`/`correct`/`passed`/`met`), or a dict pairing `answer` with
    `reference` (equal strings count as met). A bare case id carries no
    verdict - that is "no evidence", never an assumed pass.
    """
    if isinstance(value, dict):
        for key in ("ok", "correct", "passed", "met"):
            if key in value:
                return _as_bool(value.get(key))
        answer = None
        reference = None
        for key in ("answer", "response", "output", "given"):
            if key in value:
                answer = value.get(key)
                break
        for key in ("reference", "reference_answer", "expected", "gold"):
            if key in value:
                reference = value.get(key)
                break
        if answer is not None and reference is not None:
            return str(answer).strip() == str(reference).strip()
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _as_seq(value) -> list:
    """Normalise a caller-supplied collection.

    A dict is read as id -> verdict (its VALUES are the outcomes); strings,
    numbers and other scalars become single-element lists; anything else is
    iterated. Never raises on nonsense.
    """
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (str, bytes, bool, int, float)):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    try:
        return list(value)
    except Exception:
        return [value]


def _mean(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return float(sum(values)) / float(len(values))


def _as_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _as_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _normalize_case_ids(value) -> list:
    """Held-out case ids as a clean list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, (set, frozenset, tuple, list)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_flags(value) -> list:
    """Character flags as a clean list of lower-cased non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        out = []
        for item in value:
            text = str(item).strip().lower()
            if text:
                out.append(text)
        return out
    text = str(value).strip().lower()
    return [text] if text else []


def _vote_summary(votes) -> dict:
    """Vote tallies - reported, NEVER consulted by the promotion gate."""
    if votes is None:
        return {"total": 0, "yes": 0, "no": 0, "unanimous_yes": False}
    if isinstance(votes, (str, bytes)) or not isinstance(
            votes, (list, tuple, set, frozenset)):
        votes = [votes]
    items = [str(item).strip().lower() for item in votes]
    yes_words = ("yes", "y", "true", "1", "for", "aye", "approve")
    no_words = ("no", "n", "false", "0", "against", "reject")
    yes = sum(1 for item in items if item in yes_words)
    no = sum(1 for item in items if item in no_words)
    total = len(items)
    return {"total": total, "yes": yes, "no": no,
            "unanimous_yes": bool(total > 0 and yes == total)}


def _clamp_limit(limit, default: int = 50) -> int:
    try:
        value = int(limit) if limit is not None else int(default)
    except Exception:
        value = int(default)
    if value < 1:
        value = 1
    return min(value, LIST_CAP)


def _loads(text, default):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


class LabLearning:
    """Practice/validation store. Construct with a caller-supplied path.

    ``__init__`` performs NO I/O: no directory is created, no database is
    opened. Call ``init_schema()`` (idempotent) or just start calling
    methods - the connection is opened lazily on first use.
    """

    def __init__(self, path, char_flags=None):
        if not path:
            raise ValueError(
                "LabLearning requires a caller-supplied database path; "
                "no default location is built in")
        self.path = Path(path)
        # Character flags (`npc`, `manual`, ...) come from the caller: either a
        # mapping {char_id: [flags]} or a callable char_id -> flags. Kept out
        # of the database on purpose - the authority system owns that truth.
        self._char_flags = char_flags
        self._local_flags: dict = {}
        self._caps: dict = {}          # bucket -> {metric: value}
        self._debate_rounds: dict = {}  # debate id -> max rounds
        self._lock = threading.RLock()
        self._conn = None              # opened lazily; __init__ does no I/O

    # -- storage ------------------------------------------------------------
    def init_schema(self) -> dict:
        """Create every table if missing. Safe to call any number of times."""
        with self._lock:
            if self._conn is None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                self._conn = sqlite3.connect(str(self.path),
                                             check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            for statement in SCHEMA_STATEMENTS:
                self._conn.execute(statement)
            self._conn.commit()
        return {"ok": True, "tables": len(SCHEMA_STATEMENTS),
                "path": str(self.path)}

    def _db(self):
        if self._conn is None:
            self.init_schema()
        return self._conn

    def _write(self, sql, params=()):
        with self._lock:
            conn = self._db()
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    def _rows(self, sql, params=()):
        with self._lock:
            return list(self._db().execute(sql, params))

    def _one(self, sql, params=()):
        with self._lock:
            return self._db().execute(sql, params).fetchone()

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # -- row helpers --------------------------------------------------------
    @staticmethod
    def _row(row) -> dict:
        if row is None:
            return {}
        return {key: _json_safe(row[key]) for key in row.keys()}

    @staticmethod
    def _class_row(row) -> dict:
        """A `classes` row with `members` always exposed as a char list.

        Tutor rows store `{"chars": [...], "gap": ...}` (see
        `request_tutor`); plain classes store a bare list. The gap is
        surfaced as its own key so callers never have to know the encoding.
        """
        data = LabLearning._row(row)
        if not data:
            return {}
        parsed = _loads(data.get("members"), [])
        if isinstance(parsed, dict):
            data["members"] = [str(item) for item in parsed.get("chars") or []]
            data["gap"] = str(parsed.get("gap") or "")
        else:
            data["members"] = [str(item) for item in parsed or []]
            data["gap"] = ""
        data["skill_id"] = data.get("skill_id") or ""
        data["goal_id"] = data.get("goal_id") or ""
        if data.get("state") == "tutoring":
            data["from_char"] = data["members"][0] if data["members"] else ""
            data["to_char"] = str(data.get("teacher") or "")
        return data

    @staticmethod
    def _session_row(row) -> dict:
        data = LabLearning._row(row)
        if data:
            data["joined_late"] = bool(data.get("joined_late"))
        return data

    def _count_sessions(self, class_id) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM class_sessions "
                        "WHERE class_id=?", (str(class_id),))
        return int(row["n"]) if row else 0

    def _table_names(self) -> list:
        rows = self._rows(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")
        return sorted(str(row["name"]) for row in rows)

    # -- goal lifecycle -----------------------------------------------------
    def open_goal(self, text, success_criteria, held_out_case_ids,
                  baseline_ref="") -> dict:
        """Create a goal in state `open`.

        `success_criteria` and `held_out_case_ids` are MANDATORY: a goal
        nobody can score, or that has no held-out split, is not a goal this
        module will ever validate.
        """
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "reason": "a goal needs text"}
        criteria = success_criteria
        if isinstance(criteria, (list, tuple)):
            criteria_text = json.dumps([str(item) for item in criteria])
            criteria_ok = len(criteria) > 0
        else:
            criteria_text = str(criteria or "").strip()
            criteria_ok = bool(criteria_text)
        if not criteria_ok:
            return {"ok": False, "reason": "success_criteria is mandatory"}
        held = _normalize_case_ids(held_out_case_ids)
        if not held:
            return {"ok": False,
                    "reason": "held_out_case_ids is mandatory at creation"}
        goal_id = _new_id("goal")
        now = _now()
        self._write(
            "INSERT INTO learning_goals (id,text,success_criteria,"
            "held_out_case_ids,baseline_ref,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (goal_id, text, criteria_text, json.dumps(held),
             str(baseline_ref or ""), "open", now, now))
        return {"ok": True, "goal": self.goal(goal_id)}

    def goal(self, goal_id) -> dict:
        row = self._one("SELECT * FROM learning_goals WHERE id=?",
                        (str(goal_id or ""),))
        return self._goal_row(row)

    def goals(self, limit=None) -> list:
        return [self._goal_row(row) for row in self._rows(
            "SELECT * FROM learning_goals ORDER BY rowid LIMIT ?",
            (_clamp_limit(limit),))]

    @staticmethod
    def _goal_row(row) -> dict:
        data = LabLearning._row(row)
        if not data:
            return {}
        data["held_out_case_ids"] = _loads(data.get("held_out_case_ids"), [])
        criteria = data.get("success_criteria")
        parsed = _loads(criteria, None)
        data["success_criteria"] = parsed if parsed is not None else criteria
        return data

    def set_goal_state(self, goal_id, state) -> dict:
        """Move a goal through its lifecycle, refusing illegal jumps.

        `promoted` is additionally gated: it needs a passing validation run
        (see `_can_promote`), so a goal can never be promoted by fiat.
        """
        goal = self.goal(goal_id)
        if not goal:
            return {"ok": False, "reason": "unknown goal %s" % goal_id}
        state = str(state or "").strip()
        if state not in GOAL_STATES:
            return {"ok": False, "reason": "unknown state %r" % state,
                    "allowed": list(GOAL_STATES)}
        current = goal.get("state")
        if state == current:
            return {"ok": True, "goal": goal, "changed": False}
        if state not in GOAL_TRANSITIONS.get(current, set()):
            return {"ok": False,
                    "reason": "cannot go from %s to %s" % (current, state),
                    "allowed": sorted(GOAL_TRANSITIONS.get(current, set()))}
        if state == "promoted":
            allowed, why = self._can_promote(str(goal_id))
            if not allowed:
                return {"ok": False, "reason": why}
        self._write("UPDATE learning_goals SET state=?, updated_at=? "
                      "WHERE id=?", (state, _now(), str(goal_id)))
        return {"ok": True, "goal": self.goal(goal_id), "changed": True}

    def start_practising(self, goal_id) -> dict:
        return self.set_goal_state(goal_id, "practising")

    def promote_goal(self, goal_id) -> dict:
        """Promotion only ever rides on a passing validation run."""
        return self.set_goal_state(goal_id, "promoted")

    def _can_promote(self, goal_id):
        """(allowed, reason) for moving `goal_id` to `promoted`.

        The latest validation run must exist, must have passed the gate
        (`regression_pass = 1`) with real evidence, and must be NEWER than
        the skill card's rollback - a rolled-back card is never re-promoted
        by reusing the run that promoted it the first time.
        """
        rows = self._rows(
            "SELECT * FROM validation_runs WHERE goal_id=? "
            "ORDER BY at DESC, rowid DESC LIMIT 1", (str(goal_id),))
        if not rows:
            return False, "promotion requires a passing validation run"
        run = self._row(rows[0])
        if int(run.get("regression_pass") or 0) != 1:
            return False, ("latest validation run did not pass: %s"
                           % run.get("evidence"))
        if not str(run.get("evidence") or "").startswith("reference outcomes met"):
            return False, ("latest validation run did not promote: %s"
                           % run.get("evidence"))
        skill_id = str(run.get("skill_id") or "")
        if skill_id:
            card = self.skill_card(skill_id)
            rolled_back_at = card.get("rolled_back_at")
            if rolled_back_at and float(run.get("at") or 0) <= float(rolled_back_at):
                return False, ("re-promotion requires a NEW validation run "
                               "(the previous one predates the rollback)")
        return True, "passing validation run found"

    # -- exercises + practice ----------------------------------------------
    def add_exercise(self, goal_id, prompt, reference_answer="",
                     held_out=False) -> dict:
        goal = self.goal(goal_id)
        if not goal:
            return {"ok": False, "reason": "unknown goal %s" % goal_id}
        prompt = str(prompt or "").strip()
        if not prompt:
            return {"ok": False, "reason": "an exercise needs a prompt"}
        exercise_id = _new_id("ex")
        self._write(
            "INSERT INTO exercises (id,goal_id,prompt,reference_answer,"
            "held_out,created_at) VALUES (?,?,?,?,?,?)",
            (exercise_id, str(goal_id), prompt, str(reference_answer or ""),
             1 if held_out else 0, _now()))
        return {"ok": True, "exercise": self.exercise(exercise_id)}

    def exercise(self, exercise_id) -> dict:
        row = self._one("SELECT * FROM exercises WHERE id=?",
                        (str(exercise_id or ""),))
        data = self._row(row)
        if data:
            data["held_out"] = bool(data.get("held_out"))
        return data

    def exercises(self, goal_id=None, held_out=None, limit=None) -> list:
        sql = "SELECT * FROM exercises"
        params = []
        if goal_id is not None:
            sql += " WHERE goal_id=?"
            params.append(str(goal_id))
        if held_out is not None:
            sql += (" AND" if params else " WHERE") + " held_out=?"
            params.append(1 if held_out else 0)
        sql += " ORDER BY rowid LIMIT ?"
        params.append(_clamp_limit(limit))
        out = []
        for row in self._rows(sql, tuple(params)):
            data = self._row(row)
            data["held_out"] = bool(data.get("held_out"))
            out.append(data)
        return out

    def record_practice(self, goal_id, char_id, exercise_id, answer,
                        correct, latency_ms=0, model_calls=0, provider="",
                        model="") -> dict:
        """One practice run. Held-out exercises are REFUSED here.

        The held-out split only means anything if it survives contact with
        practice: a case held out for validation can never double as a
        drill.
        """
        goal = self.goal(goal_id)
        if not goal:
            return {"ok": False, "reason": "unknown goal %s" % goal_id}
        exercise = self.exercise(exercise_id)
        if not exercise:
            return {"ok": False, "reason": "unknown exercise %s" % exercise_id}
        if str(exercise.get("goal_id")) != str(goal_id):
            return {"ok": False, "reason": "exercise belongs to another goal"}
        held_ids = set(str(item) for item in goal.get("held_out_case_ids") or [])
        if exercise.get("held_out") or str(exercise_id) in held_ids:
            return {"ok": False,
                    "reason": "held-out exercise cannot be used as a practice "
                              "run: %s" % exercise_id}
        run_id = _new_id("run")
        self._write(
            "INSERT INTO practice_runs (id,goal_id,char_id,exercise_id,"
            "answer,correct,latency_ms,model_calls,provider,model,at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, str(goal_id), str(char_id or ""), str(exercise_id),
             str(answer if answer is not None else ""),
             1 if _as_bool(correct) else 0, _as_int(latency_ms),
             _as_int(model_calls), str(provider or ""), str(model or ""),
             _now()))
        if goal.get("state") == "open":
            # First real practice moves the goal along its own lifecycle.
            self.set_goal_state(goal_id, "practising")
        return {"ok": True, "run": self.practice_run(run_id)}

    def practice_run(self, run_id) -> dict:
        row = self._one("SELECT * FROM practice_runs WHERE id=?",
                        (str(run_id or ""),))
        data = self._row(row)
        if data:
            data["correct"] = bool(data.get("correct"))
        return data

    def practice_runs(self, goal_id=None, char_id=None, exercise_id=None,
                      limit=None) -> list:
        sql = "SELECT * FROM practice_runs"
        clauses, params = [], []
        if goal_id is not None:
            clauses.append("goal_id=?")
            params.append(str(goal_id))
        if char_id is not None:
            clauses.append("char_id=?")
            params.append(str(char_id))
        if exercise_id is not None:
            clauses.append("exercise_id=?")
            params.append(str(exercise_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid LIMIT ?"
        params.append(_clamp_limit(limit))
        out = []
        for row in self._rows(sql, tuple(params)):
            data = self._row(row)
            data["correct"] = bool(data.get("correct"))
            out.append(data)
        return out

    # -- tutoring -----------------------------------------------------------
    def request_tutor(self, gap, from_char, to_char) -> dict:
        """Register a tutoring request about one specific gap.

        Stored as a two-person class (`id` prefix `tut-`, `teacher` = the
        tutor, `members` = the learner plus the gap) so the mandated schema
        stays exactly as specified. NOTHING is written to `class_sessions`
        here: a request that produced no exchange stored no exchange.
        """
        gap = str(gap or "").strip()
        if not gap:
            return {"ok": False, "reason": "a tutor request needs a gap"}
        from_char = str(from_char or "").strip()
        to_char = str(to_char or "").strip()
        if not from_char or not to_char:
            return {"ok": False,
                    "reason": "a tutor request needs from_char and to_char"}
        request_id = _new_id("tut")
        created_at = _now()
        self._write(
            "INSERT INTO classes (id,skill_id,goal_id,teacher,members,state,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (request_id, None, None, to_char,
             json.dumps({"chars": [from_char], "gap": gap}),
             "tutoring", created_at))
        return {"ok": True,
                "request": {"id": request_id, "gap": gap,
                            "from_char": from_char, "to_char": to_char,
                            "members": [from_char], "state": "tutoring",
                            "created_at": created_at}}

    def tutor_request(self, request_id) -> dict:
        row = self._one("SELECT * FROM classes WHERE id=? AND state='tutoring'",
                        (str(request_id or ""),))
        return self._class_row(row)

    def tutor_requests(self, limit=None) -> list:
        return [self._class_row(row) for row in self._rows(
            "SELECT * FROM classes WHERE state='tutoring' ORDER BY rowid "
            "LIMIT ?", (_clamp_limit(limit),))]

    def record_tutor_session(self, request_id, answer, question=None) -> dict:
        """Store EXACTLY one exchange: the question asked and the answer
        given. Both come from the caller; this method invents no summary,
        no interpretation and no follow-up of its own."""
        request = self.tutor_request(request_id)
        if not request:
            return {"ok": False, "reason": "unknown tutor request %s"
                    % request_id}
        if question is None:
            question = request.get("gap") or ""
        question = str(question)
        answer = str(answer if answer is not None else "")
        if not answer.strip():
            return {"ok": False, "reason": "an exchange needs the answer "
                                           "that was actually given"}
        attempt = self._count_sessions(str(request_id)) + 1
        session_id = _new_id("ses")
        self._write(
            "INSERT INTO class_sessions (id,class_id,char_id,question,answer,"
            "attempt,gap,joined_late,at) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, str(request_id), request.get("from_char") or "",
             question, answer, attempt, request.get("gap") or "", 0, _now()))
        return {"ok": True, "session": self._session_row(
            self._one("SELECT * FROM class_sessions WHERE id=?",
                      (session_id,)))}

    def tutor_sessions(self, request_id, limit=None) -> list:
        return [self._session_row(row) for row in self._rows(
            "SELECT * FROM class_sessions WHERE class_id=? ORDER BY rowid "
            "LIMIT ?", (str(request_id or ""), _clamp_limit(limit)))]

    # -- debate -------------------------------------------------------------
    def start_debate(self, goal_id, a_char, b_char,
                     max_rounds=DEFAULT_DEBATE_ROUNDS) -> dict:
        """Open a bounded debate (default `DEFAULT_DEBATE_ROUNDS` rounds)."""
        goal = self.goal(goal_id)
        if not goal:
            return {"ok": False, "reason": "unknown goal %s" % goal_id}
        a_char = str(a_char or "").strip()
        b_char = str(b_char or "").strip()
        if not a_char or not b_char:
            return {"ok": False, "reason": "a debate needs both characters"}
        try:
            bound = int(max_rounds)
        except Exception:
            bound = DEFAULT_DEBATE_ROUNDS
        if bound < 1:
            bound = 1
        debate_id = _new_id("deb")
        self._write(
            "INSERT INTO debate_sessions (id,goal_id,round,a_char,b_char,"
            "transcript,state,at) VALUES (?,?,?,?,?,?,?,?)",
            (debate_id, str(goal_id), 0, a_char, b_char, json.dumps([]),
             "open", _now()))
        self._debate_rounds[debate_id] = bound
        return {"ok": True, "debate": self.debate(debate_id),
                "max_rounds": bound}

    def debate(self, debate_id) -> dict:
        row = self._one("SELECT * FROM debate_sessions WHERE id=?",
                        (str(debate_id or ""),))
        data = self._row(row)
        if data:
            data["transcript"] = _loads(data.get("transcript"), [])
            data["max_rounds"] = self._debate_rounds.get(
                str(debate_id), DEFAULT_DEBATE_ROUNDS)
        return data

    def debates(self, goal_id=None, limit=None) -> list:
        sql = "SELECT * FROM debate_sessions"
        params = []
        if goal_id is not None:
            sql += " WHERE goal_id=?"
            params.append(str(goal_id))
        sql += " ORDER BY rowid LIMIT ?"
        params.append(_clamp_limit(limit))
        out = []
        for row in self._rows(sql, tuple(params)):
            data = self._row(row)
            debate_id = str(data.get("id"))
            data["transcript"] = _loads(data.get("transcript"), [])
            data["max_rounds"] = self._debate_rounds.get(
                debate_id, DEFAULT_DEBATE_ROUNDS)
            out.append(data)
        return out

    def debate_round(self, debate_id, text, agreed=False) -> dict:
        """Add one round. Hitting the cap without `agreed=True` makes the
        debate (and the goal, where that is a legal transition) DISPUTED -
        agreement is only ever an explicit caller decision, never inferred
        from running out of rounds."""
        debate = self.debate(debate_id)
        if not debate:
            return {"ok": False, "reason": "unknown debate %s" % debate_id}
        if debate.get("state") in ("agreed", "disputed"):
            return {"ok": False,
                    "reason": "debate already %s" % debate.get("state")}
        bound = int(debate.get("max_rounds") or DEFAULT_DEBATE_ROUNDS)
        current = int(debate.get("round") or 0)
        if current >= bound:
            return {"ok": False, "reason": "debate already at its round cap"}
        new_round = current + 1
        transcript = list(debate.get("transcript") or [])
        transcript.append({"round": new_round,
                           "text": str(text if text is not None else "")})
        if agreed:
            state = "agreed"
        elif new_round >= bound:
            state = "disputed"
        else:
            state = "active"
        self._write("UPDATE debate_sessions SET round=?,transcript=?,"
                      "state=?,at=? WHERE id=?",
                      (new_round, json.dumps(transcript), state, _now(),
                       str(debate_id)))
        if state == "disputed":
            # A capped debate is a dispute, not a silent agreement; the goal
            # follows when the lifecycle allows it (a promoted goal is never
            # demoted by a debate round).
            self.set_goal_state(debate.get("goal_id"), "disputed")
        return {"ok": True, "debate": self.debate(debate_id),
                "agreed": bool(agreed), "capped": state == "disputed"}

    # -- skill cards (TEXT ONLY - nothing here ever runs a card) -----------
    def create_skill_card(self, name, when_applicable, procedure,
                          limitations="", evidence="", checks="",
                          provenance="", version=1) -> dict:
        """Store a skill card as plain text fields.

        Deliberately separate from the executable `./skills/*.py` mechanism in
        `profiles/default.py`: this row is retrieved and READ, never imported,
        never compiled, never run. `procedure` may contain anything - it stays
        a string.
        """
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "reason": "a skill card needs a name"}
        when_applicable = str(when_applicable or "").strip()
        if not when_applicable:
            return {"ok": False,
                    "reason": "a skill card needs when_applicable text"}
        procedure = str(procedure or "")
        if not procedure.strip():
            return {"ok": False, "reason": "a skill card needs a procedure"}
        card_id = _new_id("skl")
        try:
            version_value = max(1, int(version))
        except Exception:
            version_value = 1
        self._write(
            "INSERT INTO skill_cards (id,version,name,when_applicable,"
            "procedure,limitations,evidence,checks,provenance,state,"
            "created_at,promoted_at,rolled_back_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (card_id, version_value, name, when_applicable, procedure,
             str(limitations or ""), str(evidence or ""), str(checks or ""),
             str(provenance or ""), "draft", _now(), None, None))
        return {"ok": True, "card": self.skill_card(card_id)}

    def skill_card(self, skill_id) -> dict:
        row = self._one("SELECT * FROM skill_cards WHERE id=?",
                        (str(skill_id or ""),))
        return self._row(row)

    def skill_cards(self, state=None, limit=None) -> list:
        sql = "SELECT * FROM skill_cards"
        params = []
        if state is not None:
            sql += " WHERE state=?"
            params.append(str(state))
        sql += " ORDER BY rowid LIMIT ?"
        params.append(_clamp_limit(limit))
        return [self._row(row) for row in self._rows(sql, tuple(params))]

    # -- validation gate ----------------------------------------------------
    def run_validation(self, goal, skill, held_out_cases, baseline_answers,
                       reference_outcomes=None, votes=None, target=1.0,
                       cap=DEFAULT_VALIDATION_CAP) -> dict:
        """The gate a goal/skill pair must pass to be promoted.

        Promotion requires ALL of:
          1. real evidence - held-out cases with known reference outcomes
             AND a non-empty baseline to compare against;
          2. EVERY reference outcome met;
          3. held_out_score >= baseline_score;
          4. baseline_score < target (baseline already at/above target means
             `no headroom` - there is nothing to improve, so no promotion).
        Votes never enter the predicate: unanimous "yes" over failing
        reference outcomes still refuses.

        A run with no evidence counts against `cap`; reaching the cap
        without evidence moves the goal to `disputed`.
        """
        goal_row = self.goal(goal)
        if not goal_row:
            return {"ok": False, "reason": "unknown goal %s" % goal}
        card = self.skill_card(skill)
        if not card:
            return {"ok": False, "reason": "unknown skill %s" % skill}
        if goal_row.get("state") == "promoted":
            return {"ok": False,
                    "reason": "goal is already promoted; rollback first"}
        cases = _as_seq(held_out_cases)
        baseline_list = _as_seq(baseline_answers)
        if reference_outcomes is None:
            outcomes = [_case_outcome(item) for item in cases]
        else:
            reference_outcomes = _as_seq(reference_outcomes)
            if len(reference_outcomes) != len(cases):
                return {"ok": False,
                        "reason": "reference_outcomes must align with "
                                  "held_out_cases (%d vs %d)"
                                  % (len(reference_outcomes), len(cases))}
            outcomes = [_as_bool(item) for item in reference_outcomes]

        known = [item for item in outcomes if item is not None]
        have_evidence = (bool(cases) and len(known) == len(cases)
                         and bool(baseline_list))
        held_out_score = (_mean([1.0 if item else 0.0 for item in outcomes])
                          if cases else None)
        baseline_score = (_mean([1.0 if _as_bool(item) else 0.0
                                 for item in baseline_list])
                          if baseline_list else None)
        try:
            target_value = float(target)
        except Exception:
            target_value = 1.0

        promoted = False
        regression_pass = 0
        if not have_evidence:
            evidence = "no evidence"
        elif baseline_score >= target_value:
            # Baseline already at/above target: nothing to improve, so no
            # promotion - regardless of how well the skill did.
            evidence = "no headroom"
            regression_pass = 1 if (all(outcomes)
                                    and held_out_score >= baseline_score) else 0
        elif held_out_score < baseline_score:
            evidence = "held_out below baseline"
            regression_pass = 0
        elif not all(outcomes):
            evidence = "reference outcomes not met"
            regression_pass = 0
        else:
            evidence = ("reference outcomes met; held_out %.2f >= baseline "
                        "%.2f" % (held_out_score, baseline_score))
            regression_pass = 1
            promoted = True

        attempts = self._count_validation_runs(str(goal))
        if goal_row.get("state") in ("open", "practising", "disputed",
                                     "rolled_back"):
            self.set_goal_state(goal, "validating")
        validation_id = _new_id("val")
        self._write(
            "INSERT INTO validation_runs (id,goal_id,skill_id,"
            "held_out_score,baseline_score,regression_pass,evidence,at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (validation_id, str(goal), str(skill), held_out_score,
             baseline_score, regression_pass, evidence, _now()))
        if promoted:
            now = _now()
            self._write("UPDATE skill_cards SET state='promoted',"
                        "promoted_at=?,rolled_back_at=NULL WHERE id=?",
                        (now, str(skill)))
            self.set_goal_state(goal, "promoted")
        elif not have_evidence and (attempts + 1) >= int(cap):
            # The cap is on attempts WITHOUT evidence: no evidence at the
            # cap means the question cannot be settled - disputed, openly.
            self.set_goal_state(goal, "disputed")

        return {"ok": True, "promoted": promoted, "reason": evidence,
                "validation": self.validation_run(validation_id),
                "held_out_score": held_out_score,
                "baseline_score": baseline_score,
                "target": target_value,
                "votes": _vote_summary(votes),
                "goal": self.goal(goal),
                "skill": self.skill_card(skill)}

    def validation_run(self, validation_id) -> dict:
        row = self._one("SELECT * FROM validation_runs WHERE id=?",
                        (str(validation_id or ""),))
        data = self._row(row)
        if data:
            data["regression_pass"] = bool(data.get("regression_pass"))
        return data

    def validation_runs(self, goal_id=None, skill_id=None,
                        limit=None) -> list:
        sql = "SELECT * FROM validation_runs"
        clauses, params = [], []
        if goal_id is not None:
            clauses.append("goal_id=?")
            params.append(str(goal_id))
        if skill_id is not None:
            clauses.append("skill_id=?")
            params.append(str(skill_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid LIMIT ?"
        params.append(_clamp_limit(limit))
        out = []
        for row in self._rows(sql, tuple(params)):
            data = self._row(row)
            data["regression_pass"] = bool(data.get("regression_pass"))
            out.append(data)
        return out

    def _count_validation_runs(self, goal_id) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM validation_runs "
                        "WHERE goal_id=?", (str(goal_id),))
        return int(row["n"]) if row else 0

    def rollback(self, skill_id, goal_id=None, reason="") -> dict:
        """Roll a promoted skill card (and its goal) back.

        `rolled_back_at` is stamped, which is what forces a NEW validation
        run for any later promotion: `_can_promote` refuses runs that
        predate it.
        """
        card = self.skill_card(skill_id)
        if not card:
            return {"ok": False, "reason": "unknown skill %s" % skill_id}
        if card.get("state") == "rolled_back":
            return {"ok": True, "card": card, "changed": False}
        now = _now()
        self._write("UPDATE skill_cards SET state='rolled_back',"
                    "rolled_back_at=? WHERE id=?", (now, str(skill_id)))
        if not goal_id:
            goal_id = self._goal_for_skill(skill_id)
        goal_state = None
        if goal_id:
            moved = self.set_goal_state(goal_id, "rolled_back")
            goal_state = moved.get("goal", {}).get("state")
        return {"ok": True, "card": self.skill_card(skill_id),
                "changed": True, "goal_id": str(goal_id or ""),
                "goal_state": goal_state,
                "reason": str(reason or "rolled back")}

    def _goal_for_skill(self, skill_id):
        rows = self._rows("SELECT goal_id FROM validation_runs "
                          "WHERE skill_id=? ORDER BY rowid DESC LIMIT 1",
                          (str(skill_id),))
        return rows[0]["goal_id"] if rows else None

    # -- classes + attendance ----------------------------------------------
    def create_class(self, skill_id, goal_id, teacher, members,
                     state="open") -> dict:
        """Open a class. `skill_id`/`goal_id` may be empty (no lesson yet);
        any id that IS given must exist."""
        teacher = str(teacher or "").strip()
        if not teacher:
            return {"ok": False, "reason": "a class needs a teacher"}
        skill_id = str(skill_id or "").strip()
        goal_id = str(goal_id or "").strip()
        if skill_id and not self.skill_card(skill_id):
            return {"ok": False, "reason": "unknown skill %s" % skill_id}
        if goal_id and not self.goal(goal_id):
            return {"ok": False, "reason": "unknown goal %s" % goal_id}
        member_list = _normalize_case_ids(members)
        class_id = _new_id("cls")
        self._write(
            "INSERT INTO classes (id,skill_id,goal_id,teacher,members,state,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (class_id, skill_id, goal_id, teacher, json.dumps(member_list),
             str(state or "open"), _now()))
        return {"ok": True, "class": self.get_class(class_id)}

    def get_class(self, class_id) -> dict:
        row = self._one("SELECT * FROM classes WHERE id=?",
                        (str(class_id or ""),))
        return self._class_row(row)

    def classes(self, limit=None) -> list:
        """Real classes only - tutoring rows are surfaced by
        `tutor_requests()`, never mixed in here."""
        return [self._class_row(row) for row in self._rows(
            "SELECT * FROM classes WHERE state<>'tutoring' ORDER BY rowid "
            "LIMIT ?", (_clamp_limit(limit),))]

    def set_char_flags(self, char_id, flags) -> dict:
        """Mark a character (`npc`, `manual`, ...) for attendance gating.

        Flags live in-process on purpose: the authority system owns the real
        truth, so an injected `char_flags` mapping/callable is consulted
        first and this is the local override on top of it.
        """
        char_id = str(char_id or "").strip()
        if not char_id:
            return {"ok": False, "reason": "empty char_id"}
        normalized = _normalize_flags(flags)
        self._local_flags[char_id] = normalized
        return {"ok": True, "char_id": char_id,
                "flags": self._flags_for(char_id)}

    def char_flags(self, char_id) -> list:
        return self._flags_for(char_id)

    def _flags_for(self, char_id) -> list:
        merged = []
        source = self._char_flags
        raw = None
        try:
            if callable(source):
                raw = source(char_id)
            elif isinstance(source, dict):
                raw = source.get(char_id)
                if raw is None:
                    raw = source.get(str(char_id))
        except Exception:
            raw = None
        merged.extend(_normalize_flags(raw))
        merged.extend(_normalize_flags(self._local_flags.get(str(char_id))))
        seen, out = set(), []
        for flag in merged:
            if flag not in seen:
                seen.add(flag)
                out.append(flag)
        return out

    def record_attendance(self, class_id, char_id, question, answer,
                          attempt=None, gap="", joined_late=False) -> dict:
        """Record one real exchange in a class.

        Characters flagged `npc` or `manual` are REFUSED: their words are not
        evidence of anyone learning anything. A refused attempt writes
        nothing, so counts only ever reflect recorded sessions.
        """
        cls = self.get_class(class_id)
        if not cls:
            return {"ok": False, "reason": "unknown class %s" % class_id}
        if cls.get("state") == "tutoring":
            return {"ok": False,
                    "reason": "not a class; use record_tutor_session"}
        char_id = str(char_id or "").strip()
        if not char_id:
            return {"ok": False, "reason": "empty char_id"}
        flags = self._flags_for(char_id)
        blocking = [flag for flag in flags
                    if flag in ATTENDANCE_BLOCKING_FLAGS]
        if blocking:
            return {"ok": False,
                    "reason": "attendance refused: char flagged %s"
                              % ", ".join(blocking),
                    "flags": flags}
        question = str(question if question is not None else "")
        answer = str(answer if answer is not None else "")
        if not question.strip() or not answer.strip():
            return {"ok": False,
                    "reason": "an attendance row needs the question and the "
                              "answer that were actually exchanged"}
        if attempt is None:
            attempt = self._count_sessions(str(class_id)) + 1
        session_id = _new_id("ses")
        self._write(
            "INSERT INTO class_sessions (id,class_id,char_id,question,answer,"
            "attempt,gap,joined_late,at) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, str(class_id), char_id, question, answer,
             _as_int(attempt), str(gap or ""), 1 if joined_late else 0,
             _now()))
        return {"ok": True,
                "session": self._session_row(
                    self._one("SELECT * FROM class_sessions WHERE id=?",
                              (session_id,)))}

    def attendance_count(self, class_id) -> int:
        """Rows actually recorded for this class - refused attempts never
        become rows, so the count cannot be inflated."""
        return self._count_sessions(str(class_id))

    def sessions(self, class_id, limit=None) -> list:
        return [self._session_row(row) for row in self._rows(
            "SELECT * FROM class_sessions WHERE class_id=? ORDER BY rowid "
            "LIMIT ?", (str(class_id or ""), _clamp_limit(limit)))]

    def late_join_recap(self, class_id) -> list:
        """What a latecomer missed: the recorded rows, in order, verbatim.

        Built ONLY from `class_sessions` rows for this class. With no rows
        there is no recap at all - nothing is ever written here on the
        character's behalf.
        """
        return [{"question": row.get("question") or "",
                 "answer": row.get("answer") or "",
                 "attempt": int(row.get("attempt") or 0),
                 "at": row.get("at")}
                for row in self.sessions(class_id, limit=LIST_CAP)]

    def lesson_available(self, class_id) -> bool:
        """True when the class's skill card is PROMOTED (the lesson exists).

        Deliberately independent of `mastery_demonstrated`: a lesson can be
        available while nobody has passed it, and a character can have
        passed earlier material whose lesson was since rolled back.
        """
        cls = self.get_class(class_id)
        if not cls:
            return False
        skill_id = cls.get("skill_id") or ""
        if not skill_id:
            return False
        return self.skill_card(skill_id).get("state") == "promoted"

    def mastery_demonstrated(self, class_id, char_id) -> bool:
        """True when THIS character has passed held-out material."""
        cls = self.get_class(class_id)
        if not cls:
            return False
        char_id = str(char_id or "").strip()
        if not char_id:
            return False
        sql = ("SELECT 1 FROM practice_runs pr "
               "JOIN exercises e ON e.id = pr.exercise_id "
               "WHERE pr.char_id=? AND pr.correct=1 AND e.held_out=1")
        params = [char_id]
        goal_id = cls.get("goal_id") or ""
        if goal_id:
            sql += " AND pr.goal_id=?"
            params.append(goal_id)
        sql += " LIMIT 1"
        return self._one(sql, tuple(params)) is not None

    def record_held_out_result(self, goal_id, char_id, exercise_id, correct,
                               latency_ms=0, model_calls=0, provider="",
                               model="") -> dict:
        """Score ONE held-out case for a character.

        The mirror image of `record_practice`: drills go through
        `record_practice` (which REFUSES held-out material) and held-out
        evaluations come through here (which refuses anything that is not
        held out). This is what `mastery_demonstrated` reads - without it a
        held-out pass could never be recorded without breaking the split.
        """
        goal = self.goal(goal_id)
        if not goal:
            return {"ok": False, "reason": "unknown goal %s" % goal_id}
        exercise = self.exercise(exercise_id)
        if not exercise:
            return {"ok": False, "reason": "unknown exercise %s" % exercise_id}
        if str(exercise.get("goal_id")) != str(goal_id):
            return {"ok": False, "reason": "exercise belongs to another goal"}
        held_ids = set(str(item) for item in goal.get("held_out_case_ids") or [])
        is_held = bool(exercise.get("held_out")) or str(exercise_id) in held_ids
        if not is_held:
            return {"ok": False,
                    "reason": "not a held-out case; use record_practice: %s"
                              % exercise_id}
        run_id = _new_id("run")
        self._write(
            "INSERT INTO practice_runs (id,goal_id,char_id,exercise_id,"
            "answer,correct,latency_ms,model_calls,provider,model,at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, str(goal_id), str(char_id or ""), str(exercise_id),
             "held-out evaluation",
             1 if _as_bool(correct) else 0, _as_int(latency_ms),
             _as_int(model_calls), str(provider or ""), str(model or ""),
             _now()))
        return {"ok": True, "run": self.practice_run(run_id)}

    # -- usage ledger + caps ------------------------------------------------
    def record_usage(self, bucket, char_id="", calls=0, model_calls=0,
                     tokens_in=0, tokens_out=0, cost_estimate=0.0,
                     price_known=True) -> dict:
        """Append one usage row. When the price is unknown the cost is stored
        as NULL - never as 0 - so no report can turn ignorance into `$0.00`."""
        bucket = str(bucket or "").strip()
        if bucket not in USAGE_BUCKETS:
            return {"ok": False, "reason": "unknown bucket %r" % bucket,
                    "allowed": list(USAGE_BUCKETS)}
        known = bool(price_known)
        cost = _as_float(cost_estimate) if known else None
        usage_id = _new_id("led")
        self._write(
            "INSERT INTO usage_ledger (id,bucket,char_id,calls,model_calls,"
            "tokens_in,tokens_out,cost_estimate,price_known,at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (usage_id, bucket, str(char_id or ""), _as_int(calls),
             _as_int(model_calls), _as_int(tokens_in), _as_int(tokens_out),
             cost, 1 if known else 0, _now()))
        row = self._one("SELECT * FROM usage_ledger WHERE id=?", (usage_id,))
        data = self._row(row)
        data["price_known"] = bool(data.get("price_known"))
        return {"ok": True, "usage": data}

    def usage_totals(self, bucket) -> dict:
        """Aggregated spend for a bucket. `cost_estimate` is None whenever
        ANY row in the bucket has an unknown price."""
        bucket = str(bucket or "").strip()
        row = self._one(
            "SELECT COALESCE(SUM(calls),0) AS calls, "
            "COALESCE(SUM(model_calls),0) AS model_calls, "
            "COALESCE(SUM(tokens_in),0) AS tokens_in, "
            "COALESCE(SUM(tokens_out),0) AS tokens_out, "
            "COALESCE(SUM(cost_estimate),0) AS cost_sum, "
            "COALESCE(SUM(CASE WHEN price_known=0 THEN 1 ELSE 0 END),0) "
            "AS unpriced, COUNT(*) AS rows "
            "FROM usage_ledger WHERE bucket=?", (bucket,))
        if row is None:
            return {"bucket": bucket, "calls": 0, "model_calls": 0,
                    "tokens_in": 0, "tokens_out": 0, "cost_estimate": None,
                    "price_known": True, "rows": 0}
        unpriced = int(row["unpriced"] or 0)
        priced_out = unpriced == 0
        return {"bucket": bucket, "calls": int(row["calls"] or 0),
                "model_calls": int(row["model_calls"] or 0),
                "tokens_in": int(row["tokens_in"] or 0),
                "tokens_out": int(row["tokens_out"] or 0),
                "cost_estimate": (float(row["cost_sum"] or 0.0)
                                  if priced_out else None),
                "price_known": priced_out, "rows": int(row["rows"] or 0)}

    def set_cap(self, bucket, metric, value) -> dict:
        """Set a per-bucket cap on `calls`, `tokens` or `cost`.

        Caps are process-local on purpose: the mandated schema has no cap
        table, and a cap is an operating limit, not a fact about the world.
        """
        bucket = str(bucket or "").strip()
        if bucket not in USAGE_BUCKETS:
            return {"ok": False, "reason": "unknown bucket %r" % bucket,
                    "allowed": list(USAGE_BUCKETS)}
        if isinstance(metric, dict):
            results = [self.set_cap(bucket, key, item)
                       for key, item in metric.items()]
            return {"ok": all(item.get("ok") for item in results),
                    "bucket": bucket,
                    "caps": {str(key): val for key, val in metric.items()},
                    "results": results}
        metric = str(metric or "").strip().lower()
        if metric not in CAP_METRICS:
            return {"ok": False, "reason": "unknown cap metric %r" % metric,
                    "allowed": list(CAP_METRICS)}
        try:
            numeric = float(value)
        except Exception:
            return {"ok": False, "reason": "cap value must be a number"}
        if numeric < 0:
            return {"ok": False, "reason": "cap value must be >= 0"}
        stored = int(numeric) if metric in ("calls", "tokens") else numeric
        self._caps.setdefault(bucket, {})[metric] = stored
        return {"ok": True, "bucket": bucket, "metric": metric,
                "cap": stored}

    def check_cap(self, bucket, metric=None) -> dict:
        """{remaining, exceeded, cap, price_known} for a bucket.

        No cap set -> `cap: None`. Unknown price -> the cost `remaining` is
        None (never 0, never `$0.00`) and `price_known` is False: an unknown
        price is reported as unknown, not as free.
        """
        bucket = str(bucket or "").strip()
        if bucket not in USAGE_BUCKETS:
            return {"ok": False, "reason": "unknown bucket %r" % bucket,
                    "bucket": bucket, "metric": None, "cap": None,
                    "remaining": None, "exceeded": False,
                    "price_known": False}
        totals = self.usage_totals(bucket)
        caps = dict(self._caps.get(bucket) or {})
        price_known = bool(totals["price_known"])
        if metric is None:
            chosen = self._tightest_metric(caps, totals, price_known)
            if chosen is None and caps:
                # Every set cap is currently unmeasurable (unknown price):
                # still REPORT the cap that exists instead of pretending no
                # cap was ever set - with `remaining: None`, never 0.
                chosen = next((name for name in CAP_METRICS
                               if name in caps), None)
        else:
            metric = str(metric).strip().lower()
            chosen = metric if metric in caps else None
        if chosen is None:
            return {"ok": True, "bucket": bucket, "metric": None, "cap": None,
                    "used": None, "remaining": None, "exceeded": False,
                    "price_known": price_known}
        cap_value = caps[chosen]
        if chosen == "calls":
            used = totals["calls"]
        elif chosen == "tokens":
            used = totals["tokens_in"] + totals["tokens_out"]
        else:
            used = totals["cost_estimate"]   # None when price is unknown
        if chosen == "cost" and used is None:
            return {"ok": True, "bucket": bucket, "metric": "cost",
                    "cap": cap_value, "used": None, "remaining": None,
                    "exceeded": False, "price_known": False,
                    "reason": "price unknown; cost cannot be reported"}
        remaining = float(cap_value) - float(used)
        exceeded = float(used) > float(cap_value)
        if chosen in ("calls", "tokens"):
            remaining = int(remaining) if float(remaining).is_integer() \
                else remaining
            used = int(used)
        return {"ok": True, "bucket": bucket, "metric": chosen,
                "cap": cap_value, "used": used, "remaining": remaining,
                "exceeded": bool(exceeded), "price_known": price_known}

    @staticmethod
    def _tightest_metric(caps, totals, price_known):
        """The cap closest to being hit (smallest remaining ratio)."""
        best, best_ratio = None, None
        for metric in CAP_METRICS:
            if metric not in caps:
                continue
            if metric == "cost":
                if not price_known or totals["cost_estimate"] is None:
                    continue          # unmeasurable caps are never "tightest"
                used = totals["cost_estimate"]
            elif metric == "calls":
                used = totals["calls"]
            else:
                used = totals["tokens_in"] + totals["tokens_out"]
            cap_value = float(caps[metric])
            remaining = cap_value - float(used)
            ratio = (remaining / cap_value) if cap_value > 0 else (
                0.0 if remaining >= 0 else -1.0)
            if best_ratio is None or ratio < best_ratio:
                best, best_ratio = metric, ratio
        return best

    # -- measured benefit ---------------------------------------------------
    def record_outcome(self, skill_id, task_fingerprint, ok, latency_ms=0,
                       model_calls=0) -> dict:
        """One observed outcome for a task fingerprint.

        `skill_id` empty/None records the WITHOUT case (the same task done
        without the skill) - that is the only baseline `measure_benefit`
        compares against.
        """
        fingerprint = str(task_fingerprint or "").strip()
        if not fingerprint:
            return {"ok": False, "reason": "a task fingerprint is required"}
        skill = str(skill_id or "").strip()
        if skill and not self.skill_card(skill):
            return {"ok": False, "reason": "unknown skill %s" % skill}
        outcome_id = _new_id("out")
        self._write(
            "INSERT INTO skill_outcomes (id,skill_id,task_fingerprint,ok,"
            "latency_ms,model_calls,at) VALUES (?,?,?,?,?,?,?)",
            (outcome_id, skill or None, fingerprint,
             1 if _as_bool(ok) else 0, _as_int(latency_ms),
             _as_int(model_calls), _now()))
        row = self._one("SELECT * FROM skill_outcomes WHERE id=?",
                        (outcome_id,))
        data = self._row(row)
        data["ok"] = bool(data.get("ok"))
        data["skill_id"] = data.get("skill_id") or ""
        return {"ok": True, "outcome": data}

    def outcomes(self, skill_id=None, task_fingerprint=None,
                 limit=None) -> list:
        sql = "SELECT * FROM skill_outcomes"
        clauses, params = [], []
        if skill_id is not None:
            if str(skill_id):
                clauses.append("skill_id=?")
                params.append(str(skill_id))
            else:
                clauses.append("(skill_id IS NULL OR skill_id='')")
        if task_fingerprint is not None:
            clauses.append("task_fingerprint=?")
            params.append(str(task_fingerprint))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid LIMIT ?"
        params.append(_clamp_limit(limit))
        out = []
        for row in self._rows(sql, tuple(params)):
            data = self._row(row)
            data["ok"] = bool(data.get("ok"))
            data["skill_id"] = data.get("skill_id") or ""
            out.append(data)
        return out

    @staticmethod
    def _outcome_summary(rows) -> dict:
        rows = list(rows)
        if not rows:
            return {"n": 0, "ok_rate": None, "mean_latency_ms": None,
                    "mean_model_calls": None}
        latencies = [float(row.get("latency_ms") or 0) for row in rows]
        active = [value for value in latencies if value > 0]
        return {"n": len(rows),
                "ok_rate": _mean([1.0 if _as_bool(row.get("ok")) else 0.0
                                  for row in rows]),
                "mean_latency_ms": (_mean(active) if active else None),
                "mean_model_calls": _mean(
                    [float(row.get("model_calls") or 0) for row in rows])}

    def measure_benefit(self, skill_id, task_fingerprint) -> dict:
        """WITH vs WITHOUT the skill on the same task fingerprint.

        Returns exactly `{"improved", "reason"}`: `improved` is False with
        reason `no measurable difference` when the two sides are the same.
        """
        skill = str(skill_id or "").strip()
        fingerprint = str(task_fingerprint or "").strip()
        with_rows = self.outcomes(skill_id=skill,
                                  task_fingerprint=fingerprint,
                                  limit=LIST_CAP)
        without_rows = self.outcomes(skill_id="", task_fingerprint=fingerprint,
                                     limit=LIST_CAP)
        if not with_rows or not without_rows:
            return {"improved": False, "reason": "insufficient data"}
        with_summary = self._outcome_summary(with_rows)
        without_summary = self._outcome_summary(without_rows)
        with_rate = with_summary["ok_rate"]
        without_rate = without_summary["ok_rate"]
        if with_rate > without_rate:
            return {"improved": True, "reason": "higher success rate"}
        if with_rate < without_rate:
            return {"improved": False, "reason": "lower success rate"}
        with_latency = with_summary["mean_latency_ms"]
        without_latency = without_summary["mean_latency_ms"]
        if with_latency and without_latency:
            if with_latency < without_latency:
                return {"improved": True,
                        "reason": "same success rate, lower latency"}
            if with_latency > without_latency:
                return {"improved": False,
                        "reason": "same success rate, higher latency"}
        return {"improved": False, "reason": "no measurable difference"}
