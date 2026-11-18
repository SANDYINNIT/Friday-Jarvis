"""One Light-runtime transcript writer. No inference, replay or legacy writes."""
import asyncio
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
from threading import RLock
from functools import wraps
from uuid import UUID, uuid4, uuid5

from .redaction import redact

APP_ID = 0x46524356  # FRCV, dedicated Light conversation database
CHECKPOINT_SECONDS = 1.0
MAX_TEXT = 12000
WARNING = "Some conversation changes could not be saved. Previously saved text is still available if the archive can be read."


def _serialized(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self.lock:return method(self,*args,**kwargs)
    return call
_SCHEMA = """
CREATE TABLE writer (singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch TEXT NOT NULL);
CREATE TABLE conversation (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, owner TEXT NOT NULL,
    title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX conversation_recent ON conversation(updated_at DESC, id);
CREATE TABLE turn (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversation(id),
    created_at TEXT NOT NULL, outcome TEXT NOT NULL
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turn(id),
    position INTEGER NOT NULL, speaker TEXT NOT NULL, created_at TEXT NOT NULL,
    text TEXT NOT NULL, state TEXT NOT NULL, truncated INTEGER NOT NULL,
    UNIQUE(turn_id, position)
);
"""
_CREDENTIAL = re.compile(
    r"(?im)(\b(?:password|passwd|secret|(?:access[_ -]?)?token|api[_ -]?key|authorization)"
    r"""["']?\s*(?:[:=]|\bis\b)\s*)[^\r\n]+"""
)
_BEARER = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;]+")
_API_KEY = re.compile(r"(?i)\b(?:sk-|wk-|ws-|AIza|gsk_|hf_|gh[pousr]_|github_pat_|xox[baprs]-)[\w.=-]+")
_REASONING = re.compile(r"(?is)<(?:think|reasoning)\b[^>]*>.*?(?:</(?:think|reasoning)>|\Z)")
_BINARY = re.compile(r"(?i)data:[^\s]+;base64,[^\s]+")
_URL_SECRET = re.compile(r"(?i)https?://[^\s/]+@[^\s]+|https?://[^\s?]+\?[^\s]+")


def archive_path():
    configured = os.environ.get("FRIDAY_LIGHT_ARCHIVE_DB")
    return Path(configured) if configured else Path.home() / ".friday" / "light_conversations.db"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def visible_text(text, *, assistant=False, checkpoint=False):
    """Never write raw stream fragments to disk before privacy filtering."""
    if assistant:
        # Admission supplies user-visible message provenance; JSON is also
        # legitimate answer text. Never infer tool/internal content by syntax.
        text = _REASONING.sub("", text)
    if checkpoint:
        # A partial tag, URL, key or word is not committed until delimited.
        text = re.sub(r"<[^>]*\Z|[^\s]+\Z", "", text)
    text = _BINARY.sub("[Media omitted]", text)
    text = _URL_SECRET.sub("<PRIVATE_URL>", text)
    text = _CREDENTIAL.sub(r"\1<TOKEN>", text)
    text = _BEARER.sub("<TOKEN>", text)
    text = _API_KEY.sub("<KEY>", text)
    return redact(text, force=True).strip()


def check_schema(conn):
    if conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
        raise ValueError("Not a Light conversation archive")


@dataclass
class Message:
    speaker: str
    text: str
    state: str
    at: str = field(default_factory=utc_now)
    id: str = field(default_factory=lambda: uuid4().hex)
    truncated: bool = False


@dataclass
class Turn:
    conversation: str
    source: str
    owner_tag: str
    owner: object
    generation: object
    messages: list
    id: str = field(default_factory=lambda: uuid4().hex)
    at: str = field(default_factory=utc_now)
    outcome: str = "in_progress"
    stream: Message | None = None
    seen: set = field(default_factory=set)
    dirty: dict = field(default_factory=dict)
    scheduled: object = None
    checkpoint_at: float = 0.0
    failure: str | None = None
    duplicates: set = field(default_factory=set)


class ConversationArchive:
    """Serialized runtime admission and output hooks are the only writers."""
    def __init__(self, path):
        self.lock=RLock()
        self.removed=set()
        self.path = Path(path).absolute()
        self.epoch = uuid4().hex
        self.salt = os.urandom(32)
        self.turn = None
        self.warning = None
        self.ready = False

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self.path, timeout=0.1)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _initialize(self):
        if self.ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            if (conn.execute("PRAGMA application_id").fetchone()[0] == 0
                    and not conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()):
                for statement in _SCHEMA.strip().split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA application_id={APP_ID}")
            check_schema(conn)
            conn.execute("UPDATE turn SET outcome='interrupted' WHERE outcome='in_progress'")
            conn.execute("INSERT OR REPLACE INTO writer VALUES(1,?)", (self.epoch,))
        self.ready = True

    @_serialized
    def recover(self):
        """Called at runtime startup, including when no new message arrives."""
        try:
            self._initialize()
        except (OSError, sqlite3.Error, ValueError):
            self.warning = WARNING

    @_serialized
    def admit(self, owner, generation, source, text):
        if self.turn is not None:
            self.finish(self.turn.owner, "interrupted")
        channel = getattr(owner, "channel", "")
        if channel == "cli":
            source, identity = "cli", str(owner.chat_id)
        elif channel == "telegram":
            source, identity = "telegram", str(owner.chat_id)
        elif channel == "text":
            source = "n8n" if source == "n8n" else "http"
            identity = uuid4().hex  # HTTP has no conversation/session ID
        else:
            source = "voice" if source == "voice" else "hud"
            identity = "local"
        owner_tag = hashlib.blake2b(identity.encode(), key=self.salt, digest_size=16).hexdigest()
        conversation = uuid5(UUID(self.epoch), f"{source}:{owner_tag}").hex
        suffix=0
        while conversation in self.removed:
            suffix+=1;conversation=uuid5(UUID(self.epoch),f'{source}:{owner_tag}:{suffix}').hex
        user = Message("user", text[:MAX_TEXT], "complete", truncated=len(text) > MAX_TEXT)
        turn = self.turn = Turn(conversation, source, owner_tag, owner, generation, [user])
        turn.dirty[user.id] = user
        self._save(turn)

    @_serialized
    def mark(self, owner, outcome):
        if self.turn and self.turn.owner is owner:
            if self.turn.failure is None or outcome == "cancelled":
                self.turn.failure = outcome
                self.turn.outcome = outcome
                self._save(self.turn)

    @_serialized
    def retry(self, owner, generation):
        turn = self.turn
        if turn and turn.owner is owner:
            self._save(turn)
            turn.generation = generation
            turn.stream = None
            turn.seen.clear()

    @_serialized
    def append(self, owner, generation, event, text, *, notice=False):
        turn = self.turn
        if (not turn or turn.owner is not owner or turn.generation is not generation
                or not isinstance(text, str) or not text or event in turn.seen):
            return
        # A bounded admission transcript needs no permanent event ledger.
        if len(turn.seen) >= MAX_TEXT * 2 or len(turn.messages) >= 80:
            return
        turn.seen.add(event)
        if notice or turn.stream is None:
            item = Message("assistant", "", "complete" if notice else "partial")
            turn.messages.append(item)
            if not notice:
                turn.stream = item
        else:
            item = turn.stream
        room = MAX_TEXT - len(item.text)
        item.truncated |= len(text) > room
        item.text += text[:room]
        turn.dirty[item.id] = item
        delay = CHECKPOINT_SECONDS - (time.monotonic() - turn.checkpoint_at)
        if delay <= 0:
            self._checkpoint(turn)
        elif turn.scheduled is None:
            turn.scheduled = asyncio.get_running_loop().call_later(delay, self._checkpoint, turn)

    @_serialized
    def _checkpoint(self, turn):
        if self.turn is turn:
            self._save(turn, checkpoint=True)

    @_serialized
    def finish(self, owner, outcome="completed"):
        turn = self.turn
        if not turn or turn.owner is not owner:
            return
        turn.outcome = turn.failure or outcome
        if turn.outcome == "completed" and not any(
                item.speaker == "assistant" and visible_text(item.text, assistant=True)
                for item in turn.messages):
            turn.outcome = "failed"
        if turn.stream and turn.outcome == "completed":
            turn.stream.state = "complete"
        if turn.stream:
            final = visible_text(turn.stream.text, assistant=True)
            turn.duplicates = {item.id for item in turn.messages if item is not turn.stream
                               and item.state == "partial" and final
                               and visible_text(item.text, assistant=True) == final}
        # Replace any checkpoint with final redacted content, including the
        # last complete word of an interrupted or cancelled response.
        turn.dirty.update((item.id, item) for item in turn.messages)
        self._save(turn)
        self.turn = None

    @_serialized
    def _save(self, turn, *, checkpoint=False):
        if turn.scheduled is not None:
            turn.scheduled.cancel()
            turn.scheduled = None
        turn.checkpoint_at = time.monotonic()
        try:
            self._initialize()
            with self._transaction() as conn:
                if conn.execute("SELECT epoch FROM writer WHERE singleton=1").fetchone() != (self.epoch,):
                    raise ValueError("Archive writer has been replaced")
                now = utc_now()
                user = turn.messages[0]
                title = visible_text(user.text)[:80] or "Light conversation"
                conn.execute("INSERT OR IGNORE INTO conversation VALUES(?,?,?,?,?,?)",
                             (turn.conversation, turn.source, turn.owner_tag, title, turn.at, now))
                conn.execute("UPDATE conversation SET updated_at=? WHERE id=?", (now, turn.conversation))
                conn.execute("""INSERT INTO turn VALUES(?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET outcome=excluded.outcome""",
                             (turn.id, turn.conversation, turn.at, turn.outcome))
                for index, item in enumerate(turn.messages, 1):
                    if item.id not in turn.dirty:
                        continue
                    assistant = item.speaker == "assistant"
                    safe = visible_text(item.text, assistant=assistant,
                                        checkpoint=checkpoint and item.state == "partial")
                    if item.id in turn.duplicates or (assistant and not safe):
                        conn.execute("DELETE FROM message WHERE id=?", (item.id,))
                        continue
                    conn.execute("""INSERT INTO message VALUES(?,?,?,?,?,?,?,?)
                                 ON CONFLICT(id) DO UPDATE SET text=excluded.text,
                                 state=excluded.state, truncated=excluded.truncated""",
                                 (item.id, turn.id, index, item.speaker, item.at, safe, item.state,
                                  int(item.truncated)))
        except (OSError, sqlite3.Error, ValueError):
            self.warning = WARNING
        else:
            turn.dirty.clear()

    @_serialized
    def remove(self, reference, expected, confirm=False):
        if confirm is not True or not light_id(reference):
            return {'ok':False,'message':'Confirm the exact Light conversation; legacy archives are read-only.'}
        ident=reference[6:]
        if self.turn is not None and self.turn.conversation==ident:
            return {'ok':False,'message':'The active conversation cannot be deleted.'}
        if not self.ready or not self.path.is_file():return {'ok':False,'message':'Archive unavailable.'}
        try:
            with self._transaction() as conn:
                if conn.execute('SELECT epoch FROM writer WHERE singleton=1').fetchone()!=(self.epoch,):
                    return {'ok':False,'message':'Archive writer ownership changed; reload FRIDAY.'}
                if archive_revision(conn,ident)!=expected:
                    return {'ok':False,'conflict':True,'message':'The saved conversation changed. Reload before deleting.'}
                if conn.execute("SELECT 1 FROM turn WHERE conversation_id=? AND outcome='in_progress'",(ident,)).fetchone():
                    return {'ok':False,'message':'An unfinished conversation cannot be deleted.'}
                conn.execute('DELETE FROM message WHERE turn_id IN (SELECT id FROM turn WHERE conversation_id=?)',(ident,))
                conn.execute('DELETE FROM turn WHERE conversation_id=?',(ident,))
                conn.execute('DELETE FROM conversation WHERE id=?',(ident,))
            self.removed.add(ident)
            return {'ok':True,'deleted':reference,'message':'Saved Light conversation deleted. No tools or memories were changed.'}
        except (OSError,sqlite3.Error,ValueError):
            return {'ok':False,'message':'Conversation deletion was not confirmed. Reload before retrying.'}


def light_id(value):
    return isinstance(value, str) and re.fullmatch(r"light:[0-9a-f]{32}", value) is not None


def directory(conn, limit):
    check_schema(conn)
    rows = [dict(row) for row in conn.execute("""
        SELECT 'light:' || id AS id, title, source, created_at, updated_at
        FROM conversation ORDER BY updated_at DESC, id LIMIT ?""", (limit + 1,))]
    return {"rows": rows[:limit], "truncated": len(rows) > limit}


def transcript(conn, conversation_id, limit):
    check_schema(conn)
    conversation_id = conversation_id.removeprefix("light:")
    if not conn.execute("SELECT 1 FROM conversation WHERE id=?", (conversation_id,)).fetchone():
        raise FileNotFoundError
    rows = [dict(row) for row in conn.execute("""
        SELECT m.id, m.speaker, m.created_at AS timestamp, m.text, m.truncated,
               m.state, t.id AS turn_id, t.outcome, c.source
        FROM conversation c JOIN turn t ON c.id=t.conversation_id
        JOIN message m ON t.id=m.turn_id WHERE c.id=?
        ORDER BY t.rowid, m.position LIMIT ?""", (conversation_id, limit + 1))]
    return {"revision":archive_revision(conn,conversation_id),"rows": [{**row, "position": i, "kind": "message"}
                     for i, row in enumerate(rows[:limit], 1)],
            "truncated": len(rows) > limit, "id": "light:" + conversation_id}


def archive_revision(conn,ident):
    row=conn.execute('SELECT updated_at FROM conversation WHERE id=?',(ident,)).fetchone()
    if row is None:raise FileNotFoundError
    count=conn.execute('SELECT count(*) FROM message m JOIN turn t ON t.id=m.turn_id WHERE t.conversation_id=?',(ident,)).fetchone()[0]
    return hashlib.sha256(f'{ident}|{row[0]}|{count}'.encode()).hexdigest()
