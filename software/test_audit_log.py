"""WP11: JSONL audit log unit tests."""
import json
from datetime import datetime, timezone

from source.server.audit import AuditLog, retention_days


def test_write_rotated_file_and_redaction(tmp_path):
    log = AuditLog(str(tmp_path))
    entry = log.log({
        "actor": "voice", "risk": "reversible", "operation": "type",
        "command": "type my password hunter2 behind the barn",
        "args": {"email": "snuffy@example.com"},
    })
    assert entry is not None
    files = list(tmp_path.glob("friday-audit-*.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text(encoding="utf-8"))
    assert line["operation"] == "type"
    assert line["actor"] == "voice"
    assert line["args"]["email"] == "<EMAIL>"
    assert "hunter2" in line["command"]  # plain dictation text, not auto-masked


def test_log_never_raises_on_junk(tmp_path):
    log = AuditLog(str(tmp_path))
    assert log.log(None) is None
    assert log.log("junk") is None
    assert log.log({"operation": "x", "payload": object()}) is not None


def test_list_recent_returns_newest(tmp_path):
    log = AuditLog(str(tmp_path))
    for index in range(3):
        log.log({"actor": "system", "operation": "event", "index": index})
    events = log.list_recent(limit=10)
    assert len(events) == 3
    assert events[-1]["index"] == 2


def test_prune_removes_old_days(tmp_path):
    log = AuditLog(str(tmp_path))
    now = datetime.now(timezone.utc).date()
    day = log._path(now.isoformat()).stem.replace("friday-audit-", "")
    fresh = tmp_path / f"friday-audit-{day}.jsonl"
    fresh.write_text("{}\n", encoding="utf-8")
    old = tmp_path / "friday-audit-20000101.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    removed = log.prune(keep_days=30)
    assert removed == 1
    assert fresh.exists()
    assert not old.exists()


def test_retention_days_env(monkeypatch):
    monkeypatch.setenv("FRIDAY_AUDIT_RETENTION_DAYS", "7")
    assert retention_days() == 7