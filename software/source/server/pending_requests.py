"""Persistent pending requests: questions only Sir can answer.

WHY THIS EXISTS
---------------
When n8n's workflow asks FRIDAY something only the owner can decide ("shall I
schedule the call?"), the naive behaviour is to answer the workflow with the
question itself - which is what the workflow already knew. FRIDAY must instead
PARK the question, ask Sir, and hold it open until he answers.

The park has to survive:
  * the HTTP request returning immediately (a 7-minute hold is not reliable)
  * a local voice answer, or a Telegram one, or neither
  * a restart of FRIDAY
So state is a small atomic JSON file, and every transition is explicit.

CORRELATION
-----------
An owner decision names the exact opaque request ID ("n8n decision pr-ID:
answer"). Ordinary chat and other approval domains never select a request.

TIMEZONE
--------
Timestamps are Europe/Madrid, because that is where Sir is. Deadlines are
measured in wall-clock seconds from creation so a restart does not silently
extend or shorten a wait.
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

__all__ = [
    "PendingRequest",
    "PendingStore",
    "MADRID",
    "get_pending_store",
    "next_unreminded",
]

MADRID = ZoneInfo("Europe/Madrid")

# Stages, in escalation order.
STAGE_LOCAL = "local"          # Sir was told, we are waiting at the PC
STAGE_TELEGRAM = "telegram"    # no local answer, asked on Telegram
STAGE_PENDING = "pending"      # both windows elapsed, still unanswered

# Terminal states.
STATE_OPEN = "open"
STATE_ANSWERED = "answered"
STATE_IGNORED = "ignored"
STATE_EXPIRED = "expired"

STAGES = (STAGE_LOCAL, STAGE_TELEGRAM, STAGE_PENDING)
TERMINAL_STATES = frozenset({STATE_ANSWERED, STATE_IGNORED, STATE_EXPIRED})

# How long to wait at each stage, in seconds. Two minutes at the PC, then
# five more on Telegram - per Sir's spec.
LOCAL_WAIT_SECONDS = 120.0
TELEGRAM_WAIT_SECONDS = 300.0

# Hard ceiling so a flood of workflow questions cannot grow this forever.
MAX_OPEN = 10
MAX_HELD = 40

_DECISION_PREFIX = re.compile(r"^n8n\s+decision(?=\s|:|$)", re.IGNORECASE)
_REQUEST_ID = re.compile(r"pr-[0-9a-f]{8}")
_EXPIRY_GRACE_SECONDS = 3600.0


def now_madrid():
    """Current time in Europe/Madrid, second precision."""
    return datetime.now(MADRID).replace(microsecond=0)


def timestamp(value=None):
    """Madrid ISO timestamp for `value` (default: now)."""
    moment = value or now_madrid()
    return moment.astimezone(MADRID).replace(microsecond=0).isoformat()


class PendingRequest:
    """One parked question. Mutable by design - the stage advances over time."""

    __slots__ = (
        "id", "text", "kind", "payload", "source",
        "stage", "state", "answer", "reminded",
        "created_at", "local_deadline", "telegram_deadline",
        "resolved_at", "resolution_note",
    )

    def __init__(self, text, kind="email_question", payload=None, source="n8n",
                 local_wait=LOCAL_WAIT_SECONDS,
                 telegram_wait=TELEGRAM_WAIT_SECONDS, now=None):
        self.id = "pr-" + uuid.uuid4().hex[:8]
        self.text = str(text or "").strip()
        self.kind = str(kind or "email_question")[:64]
        self.payload = payload if isinstance(payload, dict) else {}
        self.source = str(source or "n8n")[:32]
        self.stage = STAGE_LOCAL
        self.state = STATE_OPEN
        self.answer = ""
        self.reminded = False
        moment = now or now_madrid()
        self.created_at = timestamp(moment)
        self.local_deadline = timestamp(
            moment.fromtimestamp(moment.timestamp() + local_wait, tz=MADRID))
        self.telegram_deadline = timestamp(
            moment.fromtimestamp(moment.timestamp() + local_wait + telegram_wait,
                                 tz=MADRID))
        self.resolved_at = ""
        self.resolution_note = ""

    # -- time -----------------------------------------------------------
    @staticmethod
    def _parse(value):
        try:
            return datetime.fromisoformat(str(value)).astimezone(MADRID)
        except Exception:
            return None

    def _elapsed_since_local_deadline(self, now=None):
        deadline = self._parse(self.local_deadline)
        if deadline is None:
            return 0.0
        return max(0.0, ((now or now_madrid()) - deadline).total_seconds())

    def _elapsed_since_telegram_deadline(self, now=None):
        deadline = self._parse(self.telegram_deadline)
        if deadline is None:
            return 0.0
        return max(0.0, ((now or now_madrid()) - deadline).total_seconds())

    def is_open(self):
        return self.state == STATE_OPEN

    def decision_deadline_passed(self, now=None):
        deadline = self._parse(self.telegram_deadline)
        return deadline is None or (
            (now or now_madrid()).timestamp() - deadline.timestamp() > _EXPIRY_GRACE_SECONDS
        )

    def overdue_for(self, stage, grace=5.0, now=None):
        """True when `stage`'s wait has elapsed (with a small grace)."""
        if stage == STAGE_LOCAL:
            return self._elapsed_since_local_deadline(now) > grace
        if stage == STAGE_TELEGRAM:
            return self._elapsed_since_telegram_deadline(now) > grace
        return True

    # -- transitions ----------------------------------------------------
    def advance_to(self, stage):
        """Move to a later stage. Never moves backwards, never leaves `open`."""
        if self.state != STATE_OPEN:
            return False
        if stage not in STAGES:
            return False
        if STAGES.index(stage) <= STAGES.index(self.stage):
            return False
        self.stage = stage
        return True

    def resolve(self, answer, note=""):
        """Record Sir's decision. Idempotent: a later answer never overwrites."""
        if self.state != STATE_OPEN:
            return False
        self.answer = str(answer or "").strip()
        self.state = STATE_ANSWERED
        self.resolved_at = timestamp()
        self.resolution_note = str(note or "")[:200]
        return True

    def ignore(self, note=""):
        if self.state != STATE_OPEN:
            return False
        self.state = STATE_IGNORED
        self.resolved_at = timestamp()
        self.resolution_note = str(note or "ignored by Sir")[:200]
        return True

    def expire(self, note=""):
        if self.state != STATE_OPEN:
            return False
        self.state = STATE_EXPIRED
        self.resolved_at = timestamp()
        self.resolution_note = str(note or "left unanswered")[:200]
        return True

    # -- serialisation --------------------------------------------------
    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, data):
        """Rebuild without re-deriving deadlines - stored values are truth."""
        if not isinstance(data, dict):
            return None
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        request = cls.__new__(cls)
        for name in cls.__slots__:
            setattr(request, name, data.get(name))
        request.text = text
        request.id = str(data.get("id") or ("pr-" + uuid.uuid4().hex[:8]))
        request.kind = str(data.get("kind") or "email_question")[:64]
        request.payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        request.source = str(data.get("source") or "n8n")[:32]
        stage = str(data.get("stage") or STAGE_LOCAL)
        request.stage = stage if stage in STAGES else STAGE_LOCAL
        state = str(data.get("state") or STATE_OPEN)
        request.state = state if state in (TERMINAL_STATES | {STATE_OPEN}) else STATE_OPEN
        request.answer = str(data.get("answer") or "")
        request.reminded = bool(data.get("reminded"))
        request.created_at = str(data.get("created_at") or timestamp())
        request.local_deadline = str(data.get("local_deadline") or timestamp())
        request.telegram_deadline = str(
            data.get("telegram_deadline") or timestamp())
        request.resolved_at = str(data.get("resolved_at") or "")
        request.resolution_note = str(data.get("resolution_note") or "")[:200]
        return request

    def public_view(self):
        """What n8n (or a poller) may see. Never leaks internal flags."""
        return {
            "correlation_id": self.id,
            "state": self.state,
            "stage": self.stage,
            "question": self.text,
            "answer": self.answer,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "kind": self.kind,
        }


def _default_path(environ=None):
    environ = os.environ if environ is None else environ
    configured = environ.get("FRIDAY_PENDING_PATH", "").strip()
    if configured:
        return os.path.expanduser(configured)
    root = environ.get("FRIDAY_HOME") or os.path.join(os.path.expanduser("~"), ".friday")
    return os.path.join(root, "pending_requests.json")


class PendingStore:
    """Bounded, persistent, thread-safe store of parked questions.

    Every mutation takes the lock and rewrites the file atomically, so a crash
    mid-write cannot leave a half-written log behind.
    """

    def __init__(self, path=None, environ=None, local_wait=LOCAL_WAIT_SECONDS,
                 telegram_wait=TELEGRAM_WAIT_SECONDS):
        self.path = os.fspath(path or _default_path(environ))
        self._lock = threading.RLock()
        self._items = []
        self._local_wait = float(local_wait)
        self._telegram_wait = float(telegram_wait)
        self._load()

    # -- persistence ----------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return
        except Exception:
            # A corrupt log must never stop FRIDAY booting. Keep it aside so
            # nothing is silently destroyed, then start clean.
            try:
                os.replace(self.path, self.path + ".corrupt")
            except Exception:
                pass
            return
        entries = raw.get("requests") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return
        loaded = []
        for entry in entries:
            request = PendingRequest.from_dict(entry)
            if request is not None:
                loaded.append(request)
        self._items = loaded[:MAX_HELD]

    def _save_locked(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": timestamp(),
            "requests": [item.to_dict() for item in self._items],
        }
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)

    def save(self):
        with self._lock:
            self._save_locked()

    # -- queries --------------------------------------------------------
    def __len__(self):
        with self._lock:
            return len(self._items)

    def all(self):
        with self._lock:
            return [item.to_dict() for item in self._items]

    def owner_views(self):
        """Consistent, payload-free display records for the local owner."""
        moment = now_madrid()
        with self._lock:
            return [{**item.public_view(),
                     "actionable": item.is_open() and not item.decision_deadline_passed(moment)}
                    for item in reversed(self._items) if item.source == "n8n"]

    def open_requests(self):
        """Open requests in display order only."""
        with self._lock:
            return [item for item in self._items if item.state == STATE_OPEN]

    def oldest_open(self):
        with self._lock:
            for item in self._items:
                if item.state == STATE_OPEN:
                    return item
        return None

    def get(self, correlation_id):
        with self._lock:
            for item in self._items:
                if item.id == correlation_id:
                    return item
        return None

    def has_open(self):
        return self.oldest_open() is not None

    # -- mutations ------------------------------------------------------
    def add(self, text, kind="email_question", payload=None, source="n8n",
            local_wait=None, telegram_wait=None, now=None):
        """Park a new question. Returns the request, or None if refused."""
        if not str(text or "").strip():
            return None
        with self._lock:
            open_count = sum(1 for item in self._items if item.state == STATE_OPEN)
            if open_count >= MAX_OPEN:
                return None
            request = PendingRequest(
                text, kind=kind, payload=payload, source=source,
                local_wait=self._local_wait if local_wait is None else local_wait,
                telegram_wait=self._telegram_wait if telegram_wait is None else telegram_wait,
                now=now,
            )
            self._items.append(request)
            # Bound total history so the file cannot grow without limit.
            while len(self._items) > MAX_HELD:
                self._items.pop(0)
            self._save_locked()
            return request

    def decide(self, correlation_id, answer, *, ignore=False, note=""):
        """Commit one live n8n decision by ID before its caller may notify."""
        if not isinstance(correlation_id, str) or not _REQUEST_ID.fullmatch(correlation_id):
            return None
        with self._lock:
            request = self.get(correlation_id)
            if (request is None or request.source != "n8n" or not request.is_open()
                    or request.decision_deadline_passed()):
                return None
            if ignore:
                request.ignore(note=note)
            else:
                request.resolve(answer, note=note)
            try:
                self._save_locked()
            except OSError:
                # Do not spend the claim, or send a callback, if persistence failed.
                request.state = STATE_OPEN
                request.answer = request.resolved_at = request.resolution_note = ""
                raise
            return request

    def resolve(self, correlation_id, answer, note=""):
        """Resolve a live request by exact ID."""
        return self.decide(correlation_id, answer, note=note) is not None

    def decision_targets(self):
        """Return eligible IDs for clarification, without selecting one."""
        moment = now_madrid()
        with self._lock:
            return [item.id for item in self._items if item.is_open()
                    and item.source == "n8n"
                    and not item.decision_deadline_passed(moment)]

    def advance(self, correlation_id, stage, now=None):
        """Advance ONE request to a later stage. Returns True when it moved."""
        with self._lock:
            request = self.get(correlation_id)
            if request is None or not request.advance_to(stage):
                return False
            self._save_locked()
            return True

    def advance_oldest(self, stage, now=None):
        """Advance the oldest open request to a later stage."""
        with self._lock:
            request = self.oldest_open()
            if request is None or not request.advance_to(stage):
                return None
            self._save_locked()
            return request

    def sweep(self, now=None):
        """Drop resolved/ignored entries older than a day; expire overdue ones.

        Returns the number of entries removed from the file.
        """
        moment = now or now_madrid()
        with self._lock:
            expired = False
            for item in self._items:
                if item.is_open() and item.decision_deadline_passed(moment):
                    expired = item.expire(note="left unanswered") or expired
            keep = []
            removed = 0
            for item in self._items:
                if item.state == STATE_OPEN:
                    keep.append(item)
                    continue
                resolved = item._parse(item.resolved_at)
                if resolved is not None and (moment - resolved).total_seconds() > 86400.0:
                    removed += 1
                    continue
                keep.append(item)
            self._items = keep[:MAX_HELD]
            if expired or removed:
                self._save_locked()
            return removed


# The one store for this process. `start_server` registers its instance here so
# the brain's `friday_pending()` reads THE SAME store the server writes, rather
# than opening a second handle on the same file - two writers on one log is how
# a pending question gets lost.
_ACTIVE_STORE = None


def get_pending_store(store=None):
    """The process-wide pending store, creating one only if none is registered."""
    global _ACTIVE_STORE
    if store is not None:
        _ACTIVE_STORE = store
    if _ACTIVE_STORE is None:
        _ACTIVE_STORE = PendingStore()
    return _ACTIVE_STORE


def next_unreminded(store):
    """The oldest parked question that has not nagged yet, or None.

    `reminded` is the duplicate guard: a parked question must not be announced
    on every single turn. It is set when the reminder is produced and survives
    a restart, so a reboot does not restart the nagging either.
    """
    with store._lock:
        for item in store._items:
            if (item.source == "n8n" and item.is_open() and item.stage == STAGE_PENDING
                    and not item.reminded and not item.decision_deadline_passed()):
                return item
    return None


def is_owner_decision_command(text):
    """Identify the reserved n8n command before interpreting channel options."""
    return _DECISION_PREFIX.match(str(text or "").strip()) is not None


def resolve_owner_decision(store, text, *, source, notify):
    """Resolve the n8n decision command. Ordinary messages pass through."""
    text = str(text or "").strip()
    prefix = _DECISION_PREFIX.match(text)
    if prefix is None:
        return None
    if source not in ("text", "voice", "telegram"):
        return "Only the owner can answer an n8n request."
    target, separator, answer = text[prefix.end():].strip().partition(":")
    target, answer = target.strip().lower(), answer.strip()
    if not target:
        ids = store.decision_targets()
        if not ids:
            return "There are no open n8n decisions."
        return ("Which n8n request? " + ", ".join(ids)
                + ". Use n8n decision <request ID>: <answer>.")
    if not _REQUEST_ID.fullmatch(target):
        return "Use an n8n request ID (pr- followed by eight hexadecimal characters)."
    if not separator or not answer:
        return f"Give an answer after the colon for {target}, or use ignore."
    return decide_owner_request(store, target, answer, source=source, notify=notify)["message"]


def decide_owner_request(store, target, answer, *, source, notify):
    """Shared owner-only exact claim for chat and the local HUD action."""
    if source not in ("text", "voice", "telegram"):
        return {"ok": False, "message": "Only the owner can answer an n8n request."}
    if not isinstance(target, str) or not _REQUEST_ID.fullmatch(target):
        return {"ok": False, "message": "Select the exact n8n request ID."}
    if not isinstance(answer, str) or not answer.strip():
        return {"ok": False, "message": f"Give an answer for {target}, or use ignore."}
    answer = answer.strip()
    ignore = answer.lower() == "ignore"
    try:
        decided = store.decide(target, answer, ignore=ignore, note=f"owner decision via {source}")
    except OSError:
        return {"ok": False, "message": f"I couldn't save the decision for {target}. No callback was attempted."}
    if decided is None:
        return {"ok": False, "message": f"No open n8n request matches {target}. It may be stale or already resolved."}
    result = {"ok": True, "request": decided.public_view()}
    if ignore:
        return {**result, "message": f"Ignored {target}. Its local pending status now shows ignored."}
    try:
        callback = notify(decided, status="answered", answer=answer)
    except Exception:
        callback = None
    return {**result, "message": f"Decision recorded for {target}. " + pending_callback_receipt(target, callback)}


def pending_callback_receipt(correlation_id, result):
    """HTTP delivery and a reported application result are separate evidence."""
    result = result if isinstance(result, dict) else {}
    status = result.get("http_status")
    payload = result.get("result")
    if type(status) is not int:
        return "Callback receipt is unconfirmed. Workflow completion is unverified."
    if not 200 <= status < 300:
        return f"The callback returned HTTP {status}. Workflow completion is unverified."
    application = "application acceptance is unconfirmed"
    if isinstance(payload, dict) and payload.get("correlation_id", correlation_id) == correlation_id:
        if payload.get("ok") is True:
            application = "the webhook reports application success"
        elif payload.get("ok") is False:
            application = "the webhook reports application failure"
    return f"The callback returned HTTP {status}; {application}. Workflow completion is unverified."
