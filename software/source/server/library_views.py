"""Bounded, read-only sources for the Field Notes library."""

from contextlib import closing
from datetime import datetime, timezone
import heapq
import json
from pathlib import Path
import sqlite3

from .memory import _has_table, _timestamp, live_memory_clause
from . import light_archive


LIMIT = 100
TEXT_BYTES = 256 * 1024
ARCHIVE_BYTES = 4 * 1024 * 1024
MESSAGE_LIMIT = 200


def _source(reader):
    try:
        return {"state": "ready", **reader()}
    except FileNotFoundError:
        return {"state": "unavailable"}
    except (OSError, sqlite3.Error, ValueError, RecursionError):
        return {"state": "error"}


def _database(path, reader):
    def read():
        if path is None or not Path(path).is_file():
            raise FileNotFoundError
        with closing(sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro",
                                     uri=True, timeout=1.0)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            return reader(conn)

    return _source(read)


def _limited(rows):
    records = [dict(row) for row in rows]
    return {"rows": records[:LIMIT], "truncated": len(records) > LIMIT}


def _canonical(conn):
    result = {}
    now = _timestamp()
    correction = "NULL AS correction_reason"
    if _has_table(conn, "memory_corrections"):
        correction = """(SELECT reason FROM memory_corrections
            WHERE corrected_id = m.id OR memory_id = m.id
            ORDER BY (corrected_id IS NOT NULL) DESC, id DESC LIMIT 1) AS correction_reason"""
    for group, condition in (("current", live_memory_clause("m")),
                             ("history", f"NOT ({live_memory_clause('m')})")):
        page = _limited(conn.execute(
            f"SELECT m.*, {correction}, "
            "(SELECT id FROM memories WHERE superseded_by = m.id LIMIT 1) AS previous_id "
            f"FROM memories m WHERE {condition} "
            "ORDER BY m.updated_at DESC, m.id DESC LIMIT ?", (now, LIMIT + 1)))
        for row in page["rows"]:
            row["ref"] = f"mem-{row['id']}"
        result[group] = page
    return result


def _experiences(conn):
    return {group: _limited(conn.execute(
        "SELECT * FROM character_experiences WHERE deleted = ? "
        "ORDER BY id DESC LIMIT ?", (deleted, LIMIT + 1)))
        for group, deleted in (("current", 0), ("history", 1))}


def _events(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    stamp = "created_at" if "created_at" in columns else "NULL AS created_at"
    events = _limited(conn.execute(
        f"SELECT id, actor, tick, kind, text, {stamp} FROM events ORDER BY id DESC LIMIT ?",
        (LIMIT + 1,)))
    owners = {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM characters")}
    return {**events, "owners": owners}


def memory_library(canonical, lab):
    experiences = _database(lab._memory_db if lab else None, _experiences)
    events = _database(lab._world_db if lab else None, _events)
    owners = events.pop("owners", {})
    for group in ("current", "history"):
        for row in experiences.get(group, {}).get("rows", []):
            row.update(ref=f"exp-{row['id']}", owner_name=owners.get(row["char_id"]),
                       owner_resolved=row["char_id"] in owners)
    for row in events.get("rows", []):
        row.update(ref=f"evt-{row['id']}", owner_name=owners.get(row["actor"]))
    return {"canonical": _database(canonical, _canonical),
            "experiences": experiences, "events": events}


def _knowledge(conn):
    facts = _limited(conn.execute(
        f"SELECT * FROM memories WHERE {live_memory_clause()} "
        "ORDER BY updated_at DESC, id DESC LIMIT ?", (_timestamp(), LIMIT + 1)))
    if not _has_table(conn, "memory_summaries"):
        return {"facts": facts, "summaries": {"rows": [], "state": "unavailable"}}
    summaries = _limited(conn.execute(
        f"""SELECT s.*, m.key AS memory_key, m.source AS memory_source,
            CASE WHEN s.stale = 0 AND {live_memory_clause('m')}
                 THEN 1 ELSE 0 END AS current
            FROM memory_summaries s LEFT JOIN memories m ON s.memory_id = m.id
            ORDER BY s.updated_at DESC, s.id DESC LIMIT ?""", (_timestamp(), LIMIT + 1)))
    return {"facts": facts, "summaries": {"state": "ready", **summaries}}


def _utc_stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _notes(path):
    stat = path.stat()
    with path.open("rb") as source:
        data = source.read(TEXT_BYTES + 1)
    truncated = len(data) > TEXT_BYTES
    if truncated:
        data = data[:TEXT_BYTES]
    return {"text": data.decode("utf-8-sig", errors="replace"),
            "updated_at": _utc_stamp(stat.st_mtime), "truncated": truncated}


def knowledge_library(canonical, lab, notes):
    lab_path = lab._memory_db if lab else None
    return {"canonical": _database(canonical, _knowledge),
            "lab": _database(lab_path, _knowledge) if lab_path != canonical else None,
            "notes": _source(lambda: _notes(notes))}


def _archive_id(value):
    return (isinstance(value, str) and value.endswith(".json")
            and len(value) <= 255 and not any(c in value for c in '/\\:*?"<>|\x00')
            and not value.startswith("."))


def _archive_root(interpreter):
    root = getattr(interpreter, "conversation_history_path", None)
    if not root:
        raise FileNotFoundError
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError
    return root


def _archived_file(path, active):
    if (not _archive_id(path.name) or path.name.casefold() == str(active).casefold()
            or path.is_symlink() or path.resolve() != path or not path.is_file()):
        raise FileNotFoundError
    return path.stat()


def conversation_library(interpreter):
    def read():
        root = _archive_root(interpreter)
        active = getattr(interpreter, "conversation_filename", None)

        def entries():
            for path in root.iterdir():
                try:
                    stat = _archived_file(path, active)
                except FileNotFoundError:
                    continue
                yield stat.st_mtime, path.name

        rows = heapq.nlargest(LIMIT + 1, entries())
        return {"rows": [{"id": name, "updated_at": _utc_stamp(stamp)}
                         for stamp, name in rows[:LIMIT]], "truncated": len(rows) > LIMIT}

    legacy = _source(read)
    archive = getattr(interpreter, "light_archive", None)
    if archive is None:
        return legacy
    light = _database(archive.path, lambda conn: light_archive.directory(conn, LIMIT))
    warning = getattr(archive, "warning", None)
    sources = (legacy, light)
    ready = [item for item in sources if item["state"] == "ready"]
    if not ready:
        return {"state": "error" if any(item["state"] == "error" for item in sources) else "unavailable",
                "warning": warning}
    combined = sorted((row for item in ready for row in item["rows"]),
                      key=lambda row: (row["updated_at"], row["id"]), reverse=True)
    return {"state": "ready", "rows": combined[:LIMIT],
            "truncated": len(combined) > LIMIT or any(item.get("truncated") for item in ready),
            "warning": warning or ("One archive source could not be read."
                                   if any(item["state"] == "error" for item in sources) else None)}


def conversation_transcript(interpreter, conversation_id):
    if light_archive.light_id(conversation_id):
        archive = getattr(interpreter, "light_archive", None)
        return _database(getattr(archive, "path", None),
                         lambda conn: light_archive.transcript(conn, conversation_id, MESSAGE_LIMIT))

    def read():
        if not _archive_id(conversation_id):
            raise ValueError
        root = _archive_root(interpreter)
        path = root / conversation_id
        active = getattr(interpreter, "conversation_filename", None)
        _archived_file(path, active)
        with path.open("rb") as file:
            data = file.read(ARCHIVE_BYTES + 1)
        if len(data) > ARCHIVE_BYTES:
            return {"rows": [], "truncated": True, "oversize": True}
        messages = json.loads(data)
        if not isinstance(messages, list):
            raise ValueError
        rows, incomplete = [], False
        for number, message in enumerate(messages[:MESSAGE_LIMIT], 1):
            if not isinstance(message, dict):
                incomplete = True
                continue
            kind = message.get("type", "message")
            content = message.get("content")
            textual = isinstance(content, str) and kind in ("message", "code", "console")
            rows.append({
                "position": number, "id": message.get("id", message.get("message_id")),
                "speaker": message.get("role"), "kind": kind,
                "timestamp": message.get("timestamp", message.get("created_at")),
                "source": message.get("source"), "text": content[:16000] if textual else None,
                "truncated": textual and len(content) > 16000,
            })
        return {"rows": rows, "truncated": len(messages) > MESSAGE_LIMIT,
                "incomplete": incomplete, "id": conversation_id}

    return _source(read)
