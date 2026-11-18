"""Local JSONL audit log for FRIDAY actions and their outcomes.

Every insert is redacted, sized, and written atomically.  Files rotate daily
under ``FRIDAY_AUDIT_DIR`` (default ``~/.friday/audit``) and old files are
pruned after ``FRIDAY_AUDIT_RETENTION_DAYS`` (default 30).
"""

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import threading

from .redaction import redact_secret_values

DEFAULT_RETENTION_DAYS = 30
MAX_LINE_BYTES = 16 * 1024
_AUDIT_KEYS = ("actor", "risk", "operation", "target", "command", "args",
               "content", "request", "text", "query", "result", "error")


def default_audit_dir():
    configured = os.environ.get("FRIDAY_AUDIT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".friday" / "audit"


def retention_days():
    try:
        return max(1, int(os.environ.get("FRIDAY_AUDIT_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def _utc_stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _redact_fields(event):
    cleaned = {}
    for key, value in event.items():
        if isinstance(value, (dict, list)):
            value = redact_secret_values(value)
        elif key in _AUDIT_KEYS and isinstance(value, str):
            value = redact_secret_values(value)
        cleaned[key] = value
    return cleaned


class AuditLog:
    def __init__(self, directory=None):
        self._dir = Path(directory) if directory else default_audit_dir()
        self._mutex = threading.Lock()
        if not self._dir.exists():
            self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, stamp):
        day = stamp[:10].replace("-", "")
        return self._dir / f"friday-audit-{day}.jsonl"

    def log(self, event):
        """Record one redacted JSON line; failures never raise."""
        if not isinstance(event, dict):
            return None
        stamp = _utc_stamp()
        payload = {
            "ts": stamp,
            "actor": str(event.get("actor") or "system"),
            "risk": str(event.get("risk") or "unknown"),
            "operation": str(event.get("operation") or "unknown"),
            "allowed": bool(event.get("allowed", True)),
            "outcome": str(event.get("outcome") or "ok"),
            **{key: event[key] for key in event if key not in ("actor", "risk", "operation", "allowed", "outcome")},
        }
        payload = _redact_fields(payload)
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str)[:MAX_LINE_BYTES]
        except (TypeError, ValueError):
            return None
        try:
            with self._mutex:
                with self._path(stamp).open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            return None
        return payload

    def prune(self, keep_days=None):
        """Delete audit files older than the retention window."""
        keep_days = keep_days or retention_days()
        now = datetime.now(timezone.utc).date()
        removed = 0
        try:
            for path in self._dir.glob("friday-audit-*.jsonl"):
                day = path.stem.replace("friday-audit-", "")
                try:
                    age = (now - datetime.strptime(day, "%Y%m%d").date()).days
                except ValueError:
                    continue
                if age > keep_days:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return removed

    def list_recent(self, limit=50):
        """Return the newest redacted events for status-UI log panels."""
        events = []
        files = sorted(self._dir.glob("friday-audit-*.jsonl"), reverse=True)
        for path in files:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError:
                continue
            if len(events) >= limit:
                break
        return events[-limit:]


_log = None
_log_lock = threading.Lock()


def audit_log():
    """Process-wide singleton; data dir is created on first use."""
    global _log
    if _log is None:
        with _log_lock:
            if _log is None:
                log = AuditLog()
                log.prune()
                _log = log
    return _log


def log_action(**kwargs):
    """Convenience: log one action through the singleton."""
    return audit_log().log(kwargs)