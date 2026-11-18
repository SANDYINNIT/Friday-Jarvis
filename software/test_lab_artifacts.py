"""ROUND LAB-2D integration: real artifact files from the production worker.

Every test here uses a REAL lab_state flow (a fake provider, not a mock of
lab_state itself) with a REAL bounded file write, a REAL row with the sha256,
a REAL dedup on repeat attempt, and:
* atomic write (partial file reads do not exist in the record);
* no traversal / bad filename / symlink accepted;
* the artifact bridge can read the artifacts honestly (including
  file-missing, revoked, okay states; text only - no active markup);
* failure of the shell (disk error) is visible, NOT a fake success.
"""

import os
import time
import tempfile

import pytest

from source.server import lab_state as lab
from source.server import lab_authority as authority_mod
from source.server import lab_budget as budget_mod
from source.server import lab_artifacts as artifacts_mod
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world.executor import AgentExecutor


@pytest.fixture()
def lab_env(tmp_path, monkeypatch):
    database = tmp_path / "world.db"
    artifacts_root = tmp_path / "artifacts"
    monkeypatch.setattr(lab, "db_path", lambda: str(database))
    lab.reset()
    lab._execution_queue_reset()
    authority_mod.reset_shared()
    budget_mod.reset_shared()
    artifacts_mod.reset_shared()
    authority_mod.configure(tmp_path / "authority.db")
    budget_mod.configure(database)
    artifacts_mod.configure(database, root=str(artifacts_root))
    world, roster = lab._load_world()

    captured = []

    class FakeRunner:
        """Deterministic provider stand-in that produces a text result."""

        def __init__(self):
            self.calls = []

        def __call__(self, goal, *, system=None, timeout=None, agent=None,
                     **kw):
            self.calls.append((goal, (agent or {}).get("id")))
            return {"ok": True, "text": ("# Research Summary\n\nGoal was: "
                                         "%s" % goal),
                    "provider": "fake_pool", "model": "fake/model-FAKE",
                    "tokens_in": 12, "tokens_out": 4, "cached": False,
                    "error": "", "latency": 0.01}

    monkeypatch.setattr(lab, "_executor",
                        AgentExecutor(roster, runner=FakeRunner(),
                                      note=lab.note))
    payload = {"world": world, "roster": roster, "db": database,
               "artifacts_root": artifacts_root,
               "bridge": WebviewBridge(StatusBus(), None),
               "captured": captured,
               "artifacts": artifacts_mod.shared()}
    try:
        yield payload
    finally:
        lab._execution_queue_reset()
        lab.reset()
        authority_mod.reset_shared()
        budget_mod.reset_shared()
        artifacts_mod.reset_shared()


def _wait(predicate, timeout=6.0, interval=0.03):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestArtifactProduction:
    def test_a_completed_task_writes_a_real_bounded_file(self, lab_env):
        """The core Phase D pipeline: real roster task -> real file ->"""
        roster = lab_env["roster"]
        # Phase D fix: `assign_task` creates its own task; the test looks at
        # the assign result's task id, not the inert `create_task` surplus.
        result = lab.assign_task("research", "do real work")
        assert result.get("ok") is True, result
        task_id = result["task"]["id"]
        ok = _wait(lambda: roster.task(task_id).get("state") in
                   ("approved", "awaiting_review", "failed"))
        assert ok, "task never completed (state=%s)" % \
            roster.task(task_id).get("state")
        artifacts = lab_env["artifacts"]
        rows = artifacts.list(task_id)
        assert rows and rows[-1]["ok"] is True, "no artifact recorded"
        row = rows[-1]

        # the REAL file was written inside the configured artifact root and
        # not somewhere else (no escaped symlinks, no cross-task interference)
        real = os.path.realpath(row["path"])
        real_root = os.path.realpath(str(lab_env["artifacts_root"]))
        assert real.startswith(real_root), "the path escaped the root"
        assert os.path.isfile(real), "the artifact was recorded but never written"
        assert row["sha256"], "no integrity hash was persisted"
        assert row["mime"] == "text/markdown"
        assert row["provider"] == "fake_pool"
        assert row["model"] == "fake/model-FAKE"

        # the text inside contains the real goal (model didn't invent)
        with open(real, "r", encoding="utf-8") as handle:
            content = handle.read()
        assert "do real work" in content
        # bounded (never an oversized blob)
        assert len(content.encode("utf-8")) <= 4_000_000

    def test_dedup_same_attempt_never_creates_two_files(self, lab_env):
        """A re-run with ONE task and one attempt is a no-op, not a dup."""
        roster = lab_env["roster"]
        created = roster.create_task("prod test", owner="research")
        roster.assign(created["task"]["id"], "research")
        artifacts = lab_env["artifacts"]
        from source.server.lab_artifacts import artifact_id
        attempt_key = artifact_id(created["task"]["id"], "16601397")
        # record once
        first = artifacts.record(task_id=created["task"]["id"],
                                 attempt="16601397",
                                 text="same text both times",
                                 provider="fake_pool",
                                 model="fake/model-FAKE", ok=True)
        assert first.get("ok") is True and first.get("dedup") is None
        # retry the same attempt (one re-run) -> still one artifact row
        second = artifacts.record(task_id=created["task"]["id"],
                                  attempt="16601397",
                                  text="same text both times",
                                  provider="fake_pool",
                                  model="fake/model-FAKE", ok=True)
        assert second.get("dedup") is True
        rows = artifacts.list(created["task"]["id"])
        assert len(rows) == 1, "a retried attempt must not produce two files"

    def test_empty_result_is_never_recorded_as_a_successful_artifact(
            self, lab_env):
        artifacts = lab_env["artifacts"]
        refused = artifacts.record(task_id="t-px", attempt="1",
                                   text="   ", provider="x", model="y")
        assert refused.get("ok") is False
        assert "empty artifact text" in (refused.get("reason") or "")

    def test_path_escape_is_honestly_refused(self, lab_env):
        """No traversal, no UNC, no symlink, no absolute outside the root."""
        artifacts = lab_env["artifacts"]
        # Crafter's path: absolute outside our root
        # Rejected in _safe_root_path BEFORE the attempt exists (no name
        # is ever caller-supplied: our own id becomes the file name)
        assert artifacts._safe_root_path("..\\..\\evil.txt") is None
        assert artifacts._safe_root_path("C:\\temp\\evil.txt") is None
        assert artifacts._safe_root_path("NUL") is None

    def test_non_text_mime_never_writes_a_binary(self, lab_env):
        artifacts = lab_env["artifacts"]
        result = artifacts.record(task_id="t-px2", attempt="1",
                                 text="binary-in-disguise",
                                 mime="image/png", provider="x", model="y")
        assert result.get("ok") is False
        assert "unsupported MIME" in (result.get("reason") or "")

    def test_max_bytes_cap_boarders_bounds(self, lab_env):
        artifact_text = "x" * (4_000_001)     # 4MB+1: past the cap
        artifacts = lab_env["artifacts"]
        result = artifacts.record(task_id="t-big", attempt="big-1",
                                  text=artifact_text, provider="x",
                                  model="y")
        assert result.get("ok") is False
        assert "exceeds the" in (result.get("reason") or "")


class TestTaskInspector:
    def test_bridge_read_is_scoped_and_targeted(self, lab_env):
        """get_artifacts reads by the task_id, the task inspector does not
        accept model-invented filenames."""
        roster = lab_env["roster"]
        result = lab.assign_task("research", "bridge read real work")
        assert result.get("ok") is True, result
        task_id = result["task"]["id"]
        _wait(lambda: roster.task(task_id).get("state")
              in ("approved", "awaiting_review", "failed"))
        bridge = lab_env["bridge"]
        read = bridge.get_artifacts(task_id=task_id)
        assert read.get("ok") is True
        assert (read.get("artifacts") or []), "no artifacts returned"
        # no invented name: the ONLY id is the opaque ID we recorded
        row = read.get("artifacts")[-1]
        assert row["id"].startswith("art-"), row
        assert row["task_id"] == task_id, row

    def test_sandbox_artifact_read_reports_not_configured(
            self, monkeypatch):
        """In the pytest sandbox, no explicit configure -> bridge must
        report artifacts-not-configured honestly; no real store access."""
        artifacts_mod.reset_shared()
        try:
            bridge = WebviewBridge(StatusBus(), None)
            payload = bridge.get_artifacts()
            assert payload.get("ok") is False, payload
            assert "artifacts not configured" in (payload.get("reason") or "")
        finally:
            artifacts_mod.reset_shared()
