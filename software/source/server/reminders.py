"""Safe, optional persistent reminders.

Reminders contain only text and timing data. They never execute commands.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading


LOGGER = logging.getLogger(__name__)
MAX_TITLE_CHARS = 512
MAX_PENDING_NOTIFICATIONS = 100
MAX_DUE_PER_POLL = 100
_RECURRENCE_RE = re.compile(r"^(?:daily|weekly|interval:(\d+))$")


def default_db_path():
    configured = os.environ.get("FRIDAY_REMINDERS_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "friday_reminders.db"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _timestamp(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("due_at must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def validate_recurrence(recurrence):
    if recurrence in (None, ""):
        return None
    value = str(recurrence).strip().lower()
    match = _RECURRENCE_RE.fullmatch(value)
    if not match:
        raise ValueError("recurrence must be daily, weekly, or interval:<seconds>")
    if match.group(1) is not None:
        seconds = int(match.group(1))
        if not 1 <= seconds <= 365 * 24 * 60 * 60:
            raise ValueError("interval must be between 1 second and 365 days")
    return value


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty one": 21, "twenty two": 22, "twenty three": 23,
    "twenty four": 24, "twenty five": 25, "thirty": 30, "forty": 40, "fifty": 50,
}
_NUMBER_WORDS = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))
_AMOUNT = r"(\d+|" + _NUMBER_WORDS + r")\s+(minute|minutes|hour|hours)"


def _parse_number_token(token):
    if token is None:
        return None
    token = str(token).strip().casefold()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def _parse_absolute(value, now):
    """Parse 'remind me to X at 5pm' / 'at 17:30' (deterministic, next occurrence)."""
    match = re.fullmatch(r"remind me to (.+?) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value, re.IGNORECASE)
    if not match:
        return None
    title = match.group(1).strip()
    hour = int(match.group(2))
    minute = int(match.group(3) or 0)
    meridiem = (match.group(4) or "").casefold()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None
    if not 0 <= minute <= 59 or not title or len(title) > MAX_TITLE_CHARS:
        return None
    base = _parse_timestamp(now) if now is not None else _now()
    local_now = datetime.now().astimezone()
    when = base.astimezone(local_now.tzinfo).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= base.astimezone(local_now.tzinfo):
        when += timedelta(days=1)
    return when, title


def parse_reminder(text, *, now=None):
    """Parse deterministic relative/absolute reminder or timer phrases.

    Returns ``(due_at, title)`` or ``None``.  Accepts integer or simple
    word-numbers, "remind me in/…to …", "remind me to … in …", "set a timer
    for …", "… at 5pm / 17:30", and a best-effort "before I log off" window.
    Natural-language dates (next Tuesday, etc.) are intentionally not parsed.
    """
    value = " ".join(str(text or "").strip().split())
    if not value:
        return None
    logoff_match = re.fullmatch(r"remind me to (.+) before i (?:log off|shut down|switch off)", value, re.IGNORECASE)
    if logoff_match:
        base = _parse_timestamp(now) if now is not None else _now()
        return base + timedelta(minutes=15), logoff_match.group(1).strip()
    match = re.fullmatch(rf"remind me in {_AMOUNT} to (.+)", value, re.IGNORECASE)
    title = None
    amount_raw = None
    unit = None
    if match:
        amount_raw, unit, title = match.group(1), match.group(2), match.group(3)
    else:
        match = re.fullmatch(rf"remind me to (.+?) in {_AMOUNT}", value, re.IGNORECASE)
        if match:
            title, amount_raw, unit = match.group(1), match.group(2), match.group(3)
        else:
            match = re.fullmatch(rf"set a timer for {_AMOUNT}(?: to (.+))?", value, re.IGNORECASE)
            if match:
                amount_raw, unit = match.group(1), match.group(2)
                title = match.group(3) or "Timer"
            else:
                return _parse_absolute(value, now)
    if title is None:
        return None
    amount = _parse_number_token(amount_raw)
    if amount is None or amount < 1 or len(title.strip()) > MAX_TITLE_CHARS:
        return None
    seconds = amount * (3600 if unit.startswith("hour") else 60)
    base = _parse_timestamp(now) if now is not None else _now()
    return base + timedelta(seconds=seconds), title.strip()


class ReminderStore:
    """SQLite persistence and deterministic CRUD for reminders."""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path).expanduser() if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=1.0)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    recurrence TEXT,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled')),
                    created_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders(status, due_at);
            """)

    def create(self, title, due_at, *, recurrence=None):
        title = " ".join(str(title or "").strip().split())
        if not title or len(title) > MAX_TITLE_CHARS:
            raise ValueError("title is required and must be at most 512 characters")
        due = _parse_timestamp(due_at)
        recurrence = validate_recurrence(recurrence)
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO reminders (title, due_at, recurrence, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                (title, _timestamp(due), recurrence, _timestamp(_now())),
            )
            row = connection.execute("SELECT * FROM reminders WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def list(self, *, status=None, limit=100):
        limit = max(0, min(int(limit), 500))
        if status is not None and status not in ("pending", "fired", "cancelled"):
            raise ValueError("invalid reminder status")
        query = "SELECT * FROM reminders"
        values = []
        if status:
            query += " WHERE status = ?"
            values.append(status)
        query += " ORDER BY due_at, id LIMIT ?"
        values.append(limit)
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(query, values).fetchall()]

    def cancel(self, reminder_id):
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (int(reminder_id),),
            )
        return cursor.rowcount == 1

    def claim_due(self, *, now=None):
        """Atomically claim one due row, preventing duplicate worker firing."""
        due_now = _timestamp(_parse_timestamp(now) if now is not None else _now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at, id LIMIT 1",
                (due_now,),
            ).fetchone()
            if row is None:
                return None
            recurrence = validate_recurrence(row["recurrence"])
            if recurrence:
                due = _parse_timestamp(row["due_at"])
                if recurrence == "daily":
                    step = timedelta(days=1)
                elif recurrence == "weekly":
                    step = timedelta(days=7)
                else:
                    step = timedelta(seconds=int(recurrence.split(":", 1)[1]))
                next_due = due + step
                current = _parse_timestamp(due_now)
                while next_due <= current:
                    next_due += step
                connection.execute("UPDATE reminders SET due_at = ? WHERE id = ?", (_timestamp(next_due), row["id"]))
            else:
                connection.execute("UPDATE reminders SET status = 'fired' WHERE id = ?", (row["id"],))
            return dict(row)


def create_reminder(title, due_at, *, recurrence=None, db_path=None):
    return ReminderStore(db_path).create(title, due_at, recurrence=recurrence)


def list_reminders(*, status=None, limit=100, db_path=None):
    return ReminderStore(db_path).list(status=status, limit=limit)


def cancel_reminder(reminder_id, *, db_path=None):
    return ReminderStore(db_path).cancel(reminder_id)


class ReminderWorker:
    """Bounded polling worker with graceful, idempotent shutdown."""

    def __init__(self, store, *, status_bus=None, notification_hook=None, quiet_predicate=None, poll_interval=5.0):
        self.store = store
        self.status_bus = status_bus
        self.notification_hook = notification_hook
        self.quiet_predicate = quiet_predicate or (lambda: True)
        self.poll_interval = max(0.05, min(float(poll_interval), 3600.0))
        self.stop_event = threading.Event()
        self._thread = None
        self._pending = []
        self._lock = threading.Lock()

    def _publish(self, message):
        LOGGER.info(message)
        if self.status_bus is not None:
            # Preserve the active voice/UI state; the detail is the event.
            state = self.status_bus.snapshot().state
            self.status_bus.publish(state, message)

    def poll_once(self, *, now=None):
        fired = 0
        while fired < MAX_DUE_PER_POLL:
            reminder = self.store.claim_due(now=now)
            if reminder is None:
                break
            fired += 1
            message = f"Reminder: {reminder['title']}"
            self._publish(message)
            with self._lock:
                if len(self._pending) < MAX_PENDING_NOTIFICATIONS:
                    self._pending.append(message)
            self._deliver_pending()
        self._deliver_pending()
        return fired

    def _deliver_pending(self):
        if not self.notification_hook or not self.quiet_predicate():
            return
        with self._lock:
            pending = self._pending[:]
            self._pending.clear()
        for message in pending:
            try:
                self.notification_hook(message)
            except Exception as error:
                LOGGER.warning("Reminder notification hook failed: %s", error)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as error:
                LOGGER.warning("Reminder worker poll failed: %s", error)
            self.stop_event.wait(self.poll_interval)

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self.stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="friday-reminders", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=2.0):
        self.stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, float(timeout)))
        return not self._thread or not self._thread.is_alive()
