"""Session-wide isolation of FRIDAY's on-disk state from the test suite.

WHY THIS FILE EXISTS
--------------------
`ui_probe.act()` calls the real `audit.log_action()`, which appends to
`%USERPROFILE%\.friday\audit\friday-audit-YYYYMMDD.jsonl`. Running pytest
therefore wrote entries into Sir's live audit journal: measured on
2026-10-02, **100 of 123 `uia_*` lines in the day's journal were test debris**
(fake targets `"A"`, `"OK"`, `"Seven"`). An audit trail is evidence; polluting it
with fake actions makes it useless for the thing it exists for.

WHY `pytest_configure` AND NOT AN AUTOUSE FIXTURE
-------------------------------------------------
An autouse fixture runs *after* collection, and collection is what imports the
test modules. Those modules import `source.server.*`, several of which call
`friday_logger()` at module import time. `audit.audit_log()` also memoises its
`AuditLog` singleton on the FIRST `log_action()` call and never re-reads the
environment afterwards, so a late env change would be ignored for the rest of the
session. `pytest_configure` runs before collection, which is early enough for
both.

The project already uses this env-override convention for its other stateful
subsystems - `FRIDAY_CONFIRM_STATE_DIR` (`action_confirmations.state_paths`),
`FRIDAY_AUDIT_DIR` (`audit.default_audit_dir`), `FRIDAY_LOG_DIR`
(`file_logger.log_file_path`), `FRIDAY_MEMORY_DB`, `FRIDAY_SCRATCH_DIR`. This
file simply points the audit and log ones at a temp directory for the session.

The directory lives for the process lifetime and is cleaned by the OS temp area;
it is intentionally NOT removed in a fixture teardown, because the singleton may
still be mid-write when teardown runs.
"""

import os
import tempfile

# Fixed at configure time so every test module sees the same place.
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="friday-pytest-state-")
os.environ["FRIDAY_MCP_CONFIG"] = os.path.join(_TEST_STATE_DIR, "unconfigured-mcp.json")

os.environ.setdefault("FRIDAY_AUDIT_DIR", os.path.join(_TEST_STATE_DIR, "audit"))
os.environ.setdefault("FRIDAY_LOG_DIR", os.path.join(_TEST_STATE_DIR, "logs"))
os.environ.setdefault("FRIDAY_LIGHT_ARCHIVE_DB", os.path.join(_TEST_STATE_DIR, "light_conversations.db"))
os.environ.setdefault("FRIDAY_KNOWLEDGE_DB", os.path.join(_TEST_STATE_DIR, "knowledge.db"))
os.environ.setdefault("FRIDAY_COMPANION_SETTINGS", os.path.join(_TEST_STATE_DIR, "companion.json"))
# A launcher fixture can import the real Lab before its own monkeypatches.
# Never permit that path to select the personal saved world during collection.
os.environ["FRIDAY_WORLD_DB"] = os.path.join(_TEST_STATE_DIR, "world.db")
# 2026-10-03: the exhaustion ledger was the last live-state file still reachable
# from pytest. `api_pools._persist_ledger()` is a FULL-SNAPSHOT rewrite, so a
# test writing fake keys did not merely add noise - it deleted every genuine
# provider cooldown and replaced it with fabricated entries that the next FRIDAY
# boot would restore as real. Redirected here for the same reason as the audit and
# log directories above.
os.environ.setdefault(
    "FRIDAY_EXHAUSTION_LEDGER",
    os.path.join(_TEST_STATE_DIR, "exhaustion_ledger.json"),
)

try:
    os.makedirs(os.environ["FRIDAY_AUDIT_DIR"], exist_ok=True)
    os.makedirs(os.environ["FRIDAY_LOG_DIR"], exist_ok=True)
except OSError:  # pragma: no cover - a broken temp dir must not break collection
    pass


def pytest_configure(config):
    """Recorded so a failing run shows where the state was redirected."""
    config.stash  # noqa: B018 - touch the stash so this reads as config-time work
    print("[conftest] state redirected to: %s" % _TEST_STATE_DIR)
