"""Provider succeeds, artifact write FAILS: task must FAIL, not approve."""
import pytest

from source.server import lab_authority as authority_mod
from source.server import lab_budget as budget_mod
from source.server import lab_artifacts as artifacts_mod
from source.server import lab_state as lab
from source.server.world.executor import AgentExecutor


class FakeRunner:
    def __call__(self, goal, **kw):
        return {"ok": True, "text": "provider did succeed",
                "provider": "fake_pool", "model": "fake/model-F",
                "tokens_in": 10, "tokens_out": 5, "cached": False,
                "error": "", "latency": 0.01}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    authority_mod.reset_shared()
    budget_mod.reset_shared()
    artifacts_mod.reset_shared()
    authority_mod.configure(tmp_path / "authority.db")
    database = tmp_path / "world.db"
    monkeypatch.setattr(lab, "db_path", lambda: str(database))
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


class TestArtifactWriteFailureIsAHonestTaskFailure:
    def test_provider_success_then_broken_artifact_writes_fails_the_task(
            self, env):
        roster = env["roster"]
        exec_one = AgentExecutor(roster, runner=FakeRunner(), note=lab.note)
        created = roster.create_task("provider ok, artifact broken",
                                     owner="research")
        roster.assign(created["task"]["id"], "research")

        class Broken:
            def record(self, **kw):
                return {"ok": False, "reason": "disk exploded (test)"}
        import source.server.lab_artifacts as _art_mod
        _art_mod._shared = Broken()

        result = exec_one.execute(created["task"]["id"])
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
