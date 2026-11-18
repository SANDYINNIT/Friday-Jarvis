"""Bounded text artifacts and the persisted task result shelf."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional

MAX_ARTIFACTS_PER_TASK = 5
DEFAULT_MAX_BYTES = 4_000_000     # 4 MB text cap per artifact, no bogus names
DEFAULT_ROOT = "artifacts"

# Honesty contract: shapes we do NOT produce until a caller wires them.
NOT_YET_WIRED = ("image", "audio", "binary", "code-render", "pdf")

_BANNED_FILE_NAMES = (
    "NUL", "CON", "PRN", "AUX", "COM1", "COM2", "COM3", "COM4", "COM5",
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
    "LPT6", "LPT7", "LPT8", "LPT9",
)
BANNED_PATTERNS = (
    "\\",                                  # backslash: never a filename separator
    "\x00",                                # NUL: binary injection
    "..",                                  # traversal
)


def artifact_id(task_id: str, attempt: str) -> str:
    """Opaque identifier: not a filename, not a guessable table key."""
    raw = "%s|%s" % (str(task_id or ""), str(attempt or ""))
    return "art-%s" % hashlib.sha256(
        raw.encode("utf-8", "replace")).hexdigest()[:16]


class LabArtifacts:
    """Task outcomes as real, inspectable, bounded files."""

    def __init__(self, conn, *, root: Optional[str] = None,
                 max_bytes: int = None, lock: Optional[threading.RLock] = None):
        self._conn = conn
        self._lock = lock or threading.RLock()
        try:
            self._conn.row_factory = sqlite3.Row
        except Exception:
            pass
        self._root = Path(root) if root else Path(
            tempfile.gettempdir()) / "friday-lab-artifacts"
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_bytes = int(max_bytes if max_bytes is not None
                             else DEFAULT_MAX_BYTES)
        self._database = self._database_path(self._conn)
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta "
                "(key TEXT PRIMARY KEY, value TEXT)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS lab_artifacts ("
                " id TEXT PRIMARY KEY, task_id TEXT NOT NULL,"
                " attempt TEXT NOT NULL,"
                " at REAL NOT NULL, path TEXT NOT NULL, mime TEXT,"
                " size_bytes INTEGER NOT NULL, sha256 TEXT,"
                " provider TEXT, model TEXT, ok INTEGER, error TEXT,"
                " revoked INTEGER NOT NULL DEFAULT 0)")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS lab_artifacts_attempt "
                "ON lab_artifacts (task_id, attempt)")
            self._conn.commit()

    # ---- helpers ----------------------------------------------------------
    def _safe_root_path(self, filename: str) -> Optional[Path]:
        """Only ever return paths strictly inside root, never traversal.

        The file is our OWN name (from a SHA id); the caller's filename never
        becomes a real path. Canonical path resolution is used BOTH here and
        after symlink resolution, so a crafted symlink is refused.
        """
        if not isinstance(filename, str):
            return None
        if any(needle in filename for needle in BANNED_PATTERNS):
            return None
        for banned in _BANNED_FILE_NAMES:
            if filename.strip().upper() == banned or \
                    filename.strip().upper() == "%s." % banned:
                return None
        candidate = self._root / filename
        try:
            real = candidate.resolve(strict=False)
        except Exception:
            return None
        # The REAL path must be under the REAL root (symlink-resolved).
        real_root = self._root.resolve()
        try:
            real.relative_to(real_root)
        except ValueError:
            return None
        return real

    # ---- record: atomic write + metadata --------------------------------
    def record(self, task_id: str, attempt: str, *, text: str,
               provider: str = "", model: str = "", ok: bool = True,
               error: str = "", mime: str = "text/markdown",
               at: Optional[float] = None, connection=None) -> Dict[str, Any]:
        """Keep one immutable file/row per attempt; use the roster transaction when supplied."""
        task_id, attempt = str(task_id or ""), str(attempt or "")
        text = str(text or "")
        mime = str(mime or "text/markdown")
        if not task_id or not attempt:
            return {"ok": False, "reason": "task_id and attempt required"}
        try:
            if connection is not None:
                if not connection.in_transaction:
                    raise RuntimeError("artifact write requires the roster transaction")
                if self._database != self._database_path(connection):
                    raise RuntimeError("artifact and roster databases differ")
                return self._record(connection, task_id, attempt, text, provider, model,
                                    ok, error, mime, at)
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    result = self._record(self._conn, task_id, attempt, text, provider,
                                          model, ok, error, mime, at)
                    self._conn.commit()
                    return result
                except BaseException:
                    self._conn.rollback()
                    raise
        except Exception as error:
            return {"ok": False, "reason": "artifact storage failed: " + type(error).__name__}

    @staticmethod
    def _database_path(connection):
        return next(row[2] for row in connection.execute("PRAGMA database_list")
                    if row[1] == "main")

    def _record(self, conn, task_id, attempt, text, provider, model, ok, error, mime, at):
        art_id = artifact_id(task_id, attempt)
        existing = conn.execute(
            "SELECT id,path,sha256,ok,revoked,error FROM lab_artifacts WHERE task_id=? AND attempt=?",
            (task_id, attempt)).fetchone()
        if existing:
            return {"ok": True, "id": existing["id"], "dedup": True,
                    "path": existing["path"], "sha256": existing["sha256"],
                    "readable": bool(existing["ok"] and not self._flags(existing)[0]
                                     and self._file_exists(existing["path"]))}
        count = conn.execute("SELECT COUNT(*) FROM lab_artifacts WHERE task_id=?",
                             (task_id,)).fetchone()[0]
        if count >= MAX_ARTIFACTS_PER_TASK:
            return {"ok": False, "reason": "task has too many artifacts"}
        if not text.strip():
            return {"ok": False, "reason": "empty artifact text"}
        if not mime.startswith("text/"):
            return {"ok": False, "reason": "unsupported MIME: text required"}
        data = text.encode("utf-8")
        if len(data) > self._max_bytes:
            return {"ok": False, "reason": "artifact text exceeds the byte limit"}
        real_path = self._safe_root_path(art_id + ".md")
        if real_path is None:
            return {"ok": False, "reason": "resolved path escaped the artifact root"}

        digest = hashlib.sha256(data).hexdigest()
        stamp = float(at if at is not None else time.time())
        conn.execute(
            "INSERT INTO lab_artifacts (id,task_id,attempt,at,path,mime,size_bytes,"
            "sha256,provider,model,ok,error,revoked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (art_id, task_id, attempt, stamp, str(real_path), mime, len(data), digest,
             str(provider or ""), str(model or ""), int(bool(ok)), str(error or "")[:200]))
        fd, temporary = tempfile.mkstemp(dir=str(self._root), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, real_path)
        finally:
            # Only a scratch file created by this write, never an artifact.
            Path(temporary).unlink(missing_ok=True)
        return {"ok": True, "id": art_id, "path": str(real_path),
                "mime": mime, "size": len(data), "sha256": digest, "task": task_id}

    def _file_exists(self, path):
        try:
            resolved = Path(path).resolve()
            return resolved.is_relative_to(self._root.resolve()) and resolved.is_file()
        except (OSError, ValueError, RuntimeError):
            return False

    @staticmethod
    def _flags(row):
        revoked, error = row["revoked"], row["error"]
        # Older INSERTs wrote error text into the INTEGER flag. Preserve it
        # on read without changing any historical row.
        if revoked not in (0, 1):
            return False, str(revoked) if revoked else (error if error not in (0, "0") else "")
        return revoked == 1, error

    def artifact_path(self, art_id: str) -> str:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT path FROM lab_artifacts WHERE id=?",
                    (str(art_id or ""),)).fetchone()
            return str(row[0]) if row else ""
        except Exception:
            return ""

    def is_revoked(self, art_id: str) -> bool:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT revoked,error FROM lab_artifacts WHERE id=?",
                    (str(art_id or ""),)).fetchone()
            return bool(row and self._flags(row)[0])
        except Exception:
            return False

    def get_file_state(self, art_id: str) -> Dict[str, Any]:
        """Truthful state of ONE artifact by its ID - no path from a caller."""
        text = self.read_text(art_id)
        if text is None:
            return {"ok": False, "reason": "missing, revoked, or failed integrity check",
                    "id": str(art_id or "")}
        return {"ok": True, "id": str(art_id or ""), "text": text,
                "size": len(text.encode("utf-8"))}

    def read_text(self, art_id: str) -> Optional[str]:
        """Read the REAL file back by its opaque ID - scoped, never a guess.
        Returns None when the row or the file is gone; reports revocation
        honestly so the UI can say 'revoked by the user', not 'missing'."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT path, revoked, size_bytes, sha256, error FROM lab_artifacts"
                    " WHERE id=?", (str(art_id or ""),)).fetchone()
            if row is None or self._flags(row)[0]:
                return None
            path_raw = str(row["path"] or "")
            real_path = Path(path_raw)
            real_root = self._root.resolve()
            try:
                if not Path(path_raw).resolve().is_relative_to(real_root):
                    return None        # symlink escape after registration
            except Exception:
                return None
            if not real_path.is_file():
                return None
            with real_path.open("rb") as stream:
                data = stream.read(self._max_bytes + 1)
            if (len(data) > self._max_bytes or len(data) != row["size_bytes"]
                    or (row["sha256"] and hashlib.sha256(data).hexdigest() != row["sha256"])):
                return None
            return data.decode("utf-8")
        except Exception:
            return None

    def list(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return the full shelf, or only the supplied opaque task id."""
        clause, params = ("", ()) if not task_id else (" WHERE task_id=?", (str(task_id),))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,task_id,attempt,at,path,mime,size_bytes,sha256,"
                "provider,model,ok,error,revoked FROM lab_artifacts" +
                clause + " ORDER BY at DESC,id", params).fetchall()
        out = []
        for row in rows:
            path_raw = str(row["path"] or "")
            file_missing = not self._file_exists(path_raw)
            revoked, error = self._flags(row)
            out.append({
                "id": row["id"], "task_id": row["task_id"],
                "attempt": row["attempt"], "at": row["at"],
                "path": path_raw,
                "mime": row["mime"], "size_bytes": row["size_bytes"],
                "sha256": row["sha256"], "provider": row["provider"],
                "model": row["model"], "ok": bool(row["ok"]),
                "error": error,
                "revoked": revoked,
                "file_missing": file_missing,
                "file_state": ("revoked" if revoked else
                               "missing" if file_missing else "ok"),
            })
        return out

    def summary(self, task_id: str, *, rows=None) -> Dict[str, Any]:
        rows = self.list(task_id) if rows is None else rows
        active = [row for row in rows if row["ok"] and not row["revoked"]
                  and not row["file_missing"]]
        return {"task_id": str(task_id or ""),
                "artifacts": len(active),
                "attempts": len(rows),
                "real_file_answers": len(active),
                "mime_types": sorted({row["mime"] for row in rows})}

    def revoke(self, art_id: str) -> Dict[str, Any]:
        try:
            with self._lock:
                row = self._conn.execute("SELECT error,revoked FROM lab_artifacts WHERE id=?",
                                         (str(art_id or ""),)).fetchone()
                if row is None:
                    return {"ok": False, "reason": "unknown artifact"}
                self._conn.execute(
                    "UPDATE lab_artifacts SET revoked=1,error=? WHERE id=?",
                    (self._flags(row)[1], str(art_id or "")))
                self._conn.commit()
            return {"ok": True, "id": str(art_id or "")}
        except Exception as error:
            return {"ok": False, "reason": "%s: %s"
                    % (type(error).__name__, error)}

    def scan_integrity(self) -> List[Dict[str, Any]]:
        """Integrity status of every recorded artifact: real file presence,
        hash match, revoked. Never a guess."""
        try:
            with self._lock:
                rows = list(self._conn.execute(
                    "SELECT id, path, sha256, size_bytes, revoked,error"
                    " FROM lab_artifacts ORDER BY at DESC LIMIT 100"))
        except Exception:
            return []
        out = []
        root_real = self._root.resolve()
        for row in rows:
            path_raw = str(row["path"] or "")
            real_path = Path(path_raw)
            entry = {"id": row["id"], "revoked": self._flags(row)[0],
                     "size_bytes": row["size_bytes"], "path": path_raw}
            if not real_path.is_file() or not Path(path_raw).resolve().is_relative_to(root_real):
                entry["status"] = "file_missing"
            else:
                try:
                    digest = hashlib.sha256(real_path.read_bytes()).hexdigest()
                    actual_size = real_path.stat().st_size
                    if row["sha256"] and digest != row["sha256"]:
                        entry["status"] = "hash_mismatch"
                    elif row["size_bytes"] != actual_size:
                        entry["status"] = "size_mismatch"
                    else:
                        entry["status"] = "ok"
                except Exception:
                    entry["status"] = "unreadable"
            out.append(entry)
        return out


# ---------------------------------------------------------------------------
# Process singleton (same pytest-guard pattern as lab_authority/lab_budget)
# ---------------------------------------------------------------------------
_shared: Optional[LabArtifacts] = None
_shared_path = None
_shared_extra_root = None
_shared_lock = threading.RLock()


def configure(path, *, root: Optional[str] = None) -> LabArtifacts:
    """Bind the shelf to a caller-supplied database and root."""
    global _shared, _shared_path, _shared_extra_root
    with _shared_lock:
        reset_shared()
        _shared_path = str(path)
        _shared_extra_root = root
        return shared()


def reset_shared() -> None:
    global _shared, _shared_path, _shared_extra_root
    with _shared_lock:
        if isinstance(_shared, LabArtifacts):
            with _shared._lock:
                _shared._conn.close()
        _shared = _shared_path = _shared_extra_root = None


def shared(root: Optional[str] = None) -> Optional[LabArtifacts]:
    """Return the process shelf; pytest must supply an explicit path."""
    global _shared
    with _shared_lock:
        if _shared is not None:
            return _shared
        import sys
        from . import lab_state

        if _shared_path is None and "pytest" in sys.modules:
            return None
        target = _shared_path or lab_state.db_path()
        resolved_root = _shared_extra_root or root or str(Path(target).parent / DEFAULT_ROOT)
        conn = None
        try:
            conn = sqlite3.connect(str(target), check_same_thread=False,
                                   isolation_level=None, timeout=5.0)
            _shared = LabArtifacts(conn, root=resolved_root)
        except (OSError, sqlite3.Error):
            if conn is not None:
                conn.close()
        return _shared


def for_store(store):
    """Prepare a writer before opening the roster's result transaction."""
    shelf = shared()
    if shelf is not None:
        return shelf
    return LabArtifacts(store._conn, root=str(store.path.parent / DEFAULT_ROOT), lock=store._lock)
