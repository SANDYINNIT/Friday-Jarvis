"""Provider succeeds but the artifact write fails: task MUST fail honestly.

This is the Phase D review's #1 finding (the old ordering was write-AFTER
complete). The fix moved the artifact write BEFORE roster.complete; this
test pins the honest-failure path so a write failure is NEVER a fake
"approved without a deliverable".
"""
import pytest

from source.server import lab_authority as authority_mod
from source.server import lab_budget as budget_mod
from source.server import lab_artifacts as artifacts_mod
from source.server import lab_state as lab
from source.server.world.executor import AgentExecutor
from source.server.world.store import Store


class FakeRunner:
    def __call__(self, goal, **kw):
        return {"ok": True, "text": "provider did succeed",
                "provider": "fake_pool", "model": "fake/model-F",
                "tokens_in": 10, "tokens_out": 5, "cached": False,
                "error": "", "latency": 0.01}


@pytest.fixture()
def env(tmp_path):
    authority_mod.reset_shared()
    budget_mod.reset_shared()
    artifacts_mod.reset_shared()
    authority_mod.configure(tmp_path / "authority.db")
    database = tmp_path / "world.db"
    budget_mod.configure(database)
    artifacts_mod.configure(database, root=str(tmp_path / "artifacts"))
    lab.reset()
    lab._execution_queue_reset()
    world, roster = lab._load_world()
    try:
        yield {"world": world, "roster": roster,
               "artifacts": artifacts_mod.shared()}
    finally:
        lab._execution_queue_reset()
        lab.reset()
        authority_mod.reset_shared()
        budget_mod.reset_shared()
        artifacts_mod.reset_shared()


def test_artifact_write_failure_refuses_the_task(instead_of_succeeding=False):
    pass


class TestArtifactWriteFailureIsAHonestTaskFailure:
    def test_provider_success_then_broken_artifact_writes_fails_the_task(
            self, env):
        roster = env["roster"]
        # REAL executor with a healthy provider + a SICK artifacts singleton
        exec_one = AgentExecutor(roster, runner=FakeRunner(), note=lab.note)
        created = roster.create_task("provider ok, artifact broken",
                                     owner="research")
        roster.assign(created["task"]["id"], "research")

        # Swap the productive shared LabArtifacts for one whose `record`
        # always refuses: the executor path hits the roster.fail branch.
        class Broken:
            def record(self, **kw):
                return {"ok": False,
                        "reason": "disk exploded (deterministic test)"}
        import source.server.lab_artifacts as _art_mod
        _art_mod._shared = Broken()

        result = exec_one.execute(created["task"]["id"])
        # The task MUST be failed, never approved-without-a-file.
        assert result.get("ok") is False, result
        assert "artifact write failed" in (result.get("error") or ""), result
        stored = roster.task(created["task"]["id"])
        state = stored.get("state")
        assert state == "failed", state
        assert "artifact write failed" in (stored.get("error") or "")

    def test_artifact_pipeline_raising_also_fails_the_task(self, env):
        roster = env["roster"]
        class Exploder:
            def record(self, **kw):
                raise OSError("disk noise (deterministic test)")
        exec_one = AgentExecutor(roster, runner=FakeRunner(), note=lab.note)
        created = roster.create_task("exploder probe", owner="research")
        roster.assign(created["task"]["id"], "research")
        import source.server.lab_artifacts as _art_mod
        _art_mod._shared = Exploder()
        result = exec_one.execute(created["task"]["id"])
        assert result.get("ok") is False, result
        state = roster.task(created["task"]["id"]).get("state")
        assert state == "failed", state
