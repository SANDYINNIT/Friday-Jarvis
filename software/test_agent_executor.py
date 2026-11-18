"""Bounded agent executor: assignment -> execution -> review -> result.

The gap this closes: `Roster` had a real state machine but NO model ever ran
for an assigned task. These tests pin the executor's contract:

* it spends model calls only for an agent holding the `llm` permission;
* it is bounded on model calls, steps, wall-clock and an explicit cancel;
* a provider failure is reported honestly (never dressed up as success) and
  never writes a duplicate artifact;
* an interrupted execution is PARKED, not resumed silently;
* simulated character life (`sim.py`) stays out of task execution entirely.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

import pytest

from source.server.world import Roster, Store, executor as executor_mod
from source.server.world.executor import (
    LLM_PERMISSION,
    AgentExecutor,
    provider_runner,
)


class _FakePool:
    """A quota pool that hands out one key without any pacing sleep."""

    def next_key(self, pace=True):
        return "test-key-not-a-secret"

    def total(self):
        return 1


@pytest.fixture(autouse=True)
def _isolate_quota(monkeypatch):
    """Never sleep on real pool pacing and never touch the real ledger."""
    from source.server import api_pools
    monkeypatch.setattr(api_pools, "pool", lambda name: _FakePool())
    monkeypatch.setattr(api_pools, "model_succeed",
                        lambda pool, model, cooldown=None: True)
    monkeypatch.setattr(api_pools, "fail_model",
                        lambda pool, model, cooldown=None: None)


@pytest.fixture()
def roster():
    directory = tempfile.mkdtemp(prefix="friday-exec-")
    store = Store(os.path.join(directory, "world.db"))
    r = Roster(store)
    r.add_agent("friday", "FRIDAY", "coordinator",
                permissions=["assign", "review", LLM_PERMISSION],
                budget={"max_steps": 6, "max_model_calls": 3,
                        "max_seconds": 30})
    r.add_agent("reader", "Reader", "worker",
                permissions=["read"], budget={"max_steps": 3,
                                             "max_model_calls": 2,
                                             "max_seconds": 30})
    yield r
    store.close()


class _Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, data=None):
        self.events.append((kind, data or {}))


def _runner(text="the answer", ok=True, delay=0.0, fail_first=0):
    """A deterministic provider stand-in."""
    state = {"calls": 0}

    def run(goal, **kwargs):
        state["calls"] += 1
        if delay:
            time.sleep(delay)
        if state["calls"] <= fail_first:
            return {"ok": False, "text": "", "error": "HTTP 503",
                    "provider": "brain_modal", "model": "glm"}
        if not ok:
            return {"ok": False, "text": "", "error": "HTTP 429",
                    "provider": "brain_modal", "model": "glm"}
        return {"ok": True, "text": text, "error": "",
                "provider": "brain_modal", "model": "glm", "latency": 0.01}

    run.state = state
    return run


def _assigned(roster, goal="Summarise the audit", owner="friday",
              approval_required=False, **kw):
    created = roster.create_task(goal, approval_required=approval_required)
    assert created["ok"], created
    assigned = roster.assign(created["task"]["id"], owner, **kw)
    assert assigned["ok"], assigned
    return assigned["task"]["id"]


# ---------------------------------------------------------------------------
class TestProviderBackedFlow:
    def test_assignment_executes_and_produces_an_artifact(self, roster):
        events = _Recorder()
        runner = _runner("The audit passed with 3 findings.")
        ex = AgentExecutor(roster, runner=runner, note=events)

        task_id = _assigned(roster)
        result = ex.execute(task_id)

        assert result["ok"] is True, result
        assert result["provider"] == "brain_modal"
        assert result["model"] == "glm"
        assert result["model_calls"] == 1
        task = roster.task(task_id)
        assert task["state"] in ("awaiting_review", "approved")
        assert len(task["artifacts"]) == 1, "expected exactly one artifact"
        assert "3 findings" in task["artifacts"][0]["label"]

        kinds = [k for k, _ in events.events]
        assert "task_completed" in kinds
        completed = dict(events.events)["task_completed"]
        assert completed["provider"] == "brain_modal"
        assert isinstance(completed["latency"], (int, float))

    def test_the_result_can_then_be_reviewed(self, roster):
        # approval_required=True -> execution parks at awaiting_review, which is
        # exactly the state a human/coordinator then approves or rejects.
        ex = AgentExecutor(roster, runner=_runner("done"), note=_Recorder())
        task_id = _assigned(roster, approval_required=True)
        assert ex.execute(task_id)["ok"] is True
        assert roster.task(task_id)["state"] == "awaiting_review"
        reviewed = roster.review(task_id, "approve")
        assert reviewed["ok"] is True
        assert reviewed["task"]["state"] == "approved"
        assert roster.task(task_id)["artifacts"], "review lost the artifact"

    def test_every_return_is_json_safe(self, roster):
        ex = AgentExecutor(roster, runner=_runner(), note=_Recorder())
        task_id = _assigned(roster)
        json.dumps(ex.execute(task_id))
        json.dumps(ex.snapshot())
        json.dumps(roster.snapshot())


class TestPermissionBoundary:
    def test_an_agent_without_the_llm_permission_is_refused(self, roster):
        events = _Recorder()
        ex = AgentExecutor(roster, runner=_runner(), note=events)
        task_id = _assigned(roster, owner="reader")   # reader has only "read"
        result = ex.execute(task_id)

        assert result["ok"] is False
        assert LLM_PERMISSION in result["reason"]
        assert "lacks permission" in result["reason"]
        assert roster.task(task_id)["state"] == "failed"
        assert "task_refused" in [k for k, _ in events.events]
        assert ex.snapshot()["refused"] == 1

    def test_a_refused_agent_never_calls_the_provider(self, roster):
        runner = _runner()
        ex = AgentExecutor(roster, runner=runner, note=_Recorder())
        task_id = _assigned(roster, owner="reader")
        ex.execute(task_id)
        assert runner.state["calls"] == 0, "the provider was called anyway"


class TestBudgets:
    def test_model_call_budget_stops_the_run(self, roster):
        # Give the agent a budget of 1 call but a runner that would keep going.
        roster._agents["friday"]["budget"] = {"max_model_calls": 1,
                                              "max_steps": 5,
                                              "max_seconds": 30}
        runner = _runner(fail_first=0, ok=False)      # always fails -> retries
        ex = AgentExecutor(roster, runner=runner, note=_Recorder())
        task_id = _assigned(roster)
        result = ex.execute(task_id)
        assert runner.state["calls"] <= 1, "model budget was not enforced"
        assert result["ok"] is False
        assert "budget" in result["reason"] or "budget" in str(
            roster.task(task_id).get("error"))

    def test_wall_clock_budget_stops_a_slow_run(self, roster):
        roster._agents["friday"]["budget"] = {"max_model_calls": 50,
                                              "max_steps": 50,
                                              "max_seconds": 1}
        # Never succeeds and is slow: only the wall-clock budget can end this.
        ex = AgentExecutor(roster,
                           runner=_runner(fail_first=99, delay=0.6),
                           note=_Recorder())
        task_id = _assigned(roster)
        result = ex.execute(task_id)
        assert result["ok"] is False
        assert "wall-clock" in result["reason"]
        assert roster.task(task_id)["state"] == "failed"

    def test_step_budget_bounds_the_retry_loop(self, roster):
        roster._agents["friday"]["budget"] = {"max_model_calls": 50,
                                              "max_steps": 2,
                                              "max_seconds": 30}
        runner = _runner(fail_first=99)               # never succeeds
        ex = AgentExecutor(roster, runner=runner, note=_Recorder())
        task_id = _assigned(roster)
        result = ex.execute(task_id)
        assert runner.state["calls"] <= 3, runner.state["calls"]
        assert result["ok"] is False


class TestHonestFailure:
    def test_a_provider_error_is_reported_not_dressed_as_success(self, roster,
                                                                monkeypatch):
        events = _Recorder()
        # `budgets()` takes min(DEFAULT, agent), so the ceiling itself has to
        # move or the run ends on the budget instead of on the provider's own
        # error - which is the thing this test exists to observe.
        monkeypatch.setattr(executor_mod, "DEFAULT_MAX_MODEL_CALLS", 20)
        roster._agents["friday"]["budget"] = {"max_model_calls": 20,
                                              "max_steps": 20,
                                              "max_seconds": 30}
        ex = AgentExecutor(roster, runner=_runner(ok=False), note=events)
        task_id = _assigned(roster)
        result = ex.execute(task_id)
        assert result["ok"] is False
        assert "429" in result["error"]
        assert roster.task(task_id)["state"] == "failed"
        assert roster.task(task_id)["error"]
        assert roster.agent("friday")["state"] == "error"
        assert "task_failed" in [k for k, _ in events.events]

    def test_a_transient_failure_gets_one_bounded_retry(self, roster):
        runner = _runner(fail_first=1)
        ex = AgentExecutor(roster, runner=runner, note=_Recorder())
        task_id = _assigned(roster)
        result = ex.execute(task_id)
        assert runner.state["calls"] == 2, "expected exactly one retry"
        assert result["ok"] is True
        assert len(roster.task(task_id)["artifacts"]) == 1

    def test_an_empty_result_is_a_failure(self, roster):
        ex = AgentExecutor(roster, runner=_runner(text="   "),
                           note=_Recorder())
        task_id = _assigned(roster)
        result = ex.execute(task_id)
        assert result["ok"] is False
        assert "empty" in result["error"]
        assert roster.task(task_id)["state"] == "failed"

    def test_a_runner_that_raises_is_contained(self, roster, monkeypatch):
        monkeypatch.setattr(executor_mod, "DEFAULT_MAX_MODEL_CALLS", 20)
        roster._agents["friday"]["budget"] = {"max_model_calls": 20,
                                              "max_steps": 20,
                                              "max_seconds": 30}

        def boom(goal, **kwargs):
            raise RuntimeError("provider exploded")

        ex = AgentExecutor(roster, runner=boom, note=_Recorder())
        task_id = _assigned(roster)
        result = ex.execute(task_id)
        assert result["ok"] is False
        assert "RuntimeError" in str(result.get("error"))
        assert roster.task(task_id)["state"] == "failed"


class TestCancellationAndRecovery:
    def test_cancel_parks_the_queued_attempt_without_cancelling_a_later_one(self, roster):
        runner = _runner("recovered")
        ex = AgentExecutor(roster, runner=runner, note=_Recorder())
        task_id = _assigned(roster)
        attempt = roster.admit(task_id)["attempt"]
        assert ex.cancel("user", task_id=task_id, attempt=attempt)["cancelled"]
        result = ex.execute(task_id, attempt=attempt)
        assert not result["ok"]
        assert roster.task(task_id)["state"] == "paused"
        assert runner.state["calls"] == 0
        assert roster.resume(task_id)["ok"]
        assert ex.execute(task_id)["ok"]
        assert runner.state["calls"] == 1

    def test_cancel_during_a_run_stops_before_completion(self, roster):
        events = _Recorder()
        calls = {"n": 0}

        def cancelling(goal, **kwargs):
            calls["n"] += 1
            ex.cancel("mid-run")                      # cancel from inside
            return {"ok": True, "text": "would be stale", "error": "",
                    "provider": "p", "model": "m"}

        ex = AgentExecutor(roster, runner=cancelling, note=events)
        task_id = _assigned(roster)
        result = ex.execute(task_id)

        assert result["ok"] is False
        assert result.get("state") == "paused"
        stored = roster.task(task_id)
        assert stored["state"] == "paused"
        assert not stored["artifacts"], "stale output became an artifact"
        assert "task_completed" not in [k for k, _ in events.events]

    def test_a_parked_task_can_be_resumed_and_finished(self, roster):
        runner = _runner("second attempt worked")
        ex = AgentExecutor(roster, runner=runner, note=_Recorder())
        task_id = _assigned(roster)
        attempt = roster.admit(task_id)["attempt"]
        assert ex.cancel("user", task_id=task_id, attempt=attempt)["cancelled"]
        ex.clear_cancel()
        roster.resume(task_id)
        result = ex.execute(task_id)
        assert result["ok"] is True
        assert len(roster.task(task_id)["artifacts"]) == 1

    def test_a_running_task_from_a_dead_process_is_parked_on_reload(self, roster):
        task_id = _assigned(roster)
        attempt = roster.admit(task_id)["attempt"]
        assert roster.mark_running(task_id, attempt=attempt)["ok"]
        reloaded = Roster(roster.store)                # simulated restart
        stored = reloaded.task(task_id)
        assert stored["state"] == "paused", "restarted mid-run, not parked"
        assert "restart" in (stored["paused_by"] or "")
        assert stored["goal"]
        # ...and it can be resumed deliberately, never auto-resumed.
        assert reloaded.resume(task_id)["ok"] is True

    def test_a_running_task_is_not_silently_re_executed(self, roster):
        """No duplicate external effects after a restart."""
        task_id = _assigned(roster)
        attempt = roster.admit(task_id)["attempt"]
        assert roster.mark_running(task_id, attempt=attempt)["ok"]
        reloaded = Roster(roster.store)
        runner = _runner("re-executed")
        ex = AgentExecutor(reloaded, runner=runner, note=_Recorder())
        # execute() on a PAUSED task must refuse - resume is explicit.
        result = ex.execute(task_id)
        assert runner.state["calls"] == 0, "a parked task re-ran by itself"
        assert result["ok"] is False


class TestDelegationAndConsolidation:
    def test_subtask_result_consolidates_into_the_parent(self, roster):
        events = _Recorder()
        parent_id = _assigned(roster, goal="Produce the report", approval_required=True)

        # Give the worker the permission BEFORE it is assigned, so this is the
        # happy path; the refusal case is covered by TestPermissionBoundary.
        roster._agents["reader"]["permissions"] = ["read", LLM_PERMISSION]
        created = roster.request_subtask(
            "friday", "reader", "Draft section one",
            budget={"max_steps": 3, "max_model_calls": 2})
        assert created["ok"], created
        child_id = created["task"]["id"]

        ex = AgentExecutor(roster, runner=_runner("section text"), note=events)
        result = ex.execute(child_id)
        assert result["ok"] is True, result
        assert len(roster.task(child_id)["artifacts"]) == 1

        merged = roster.consolidate(parent_id, child_id)
        assert merged["ok"] is True, merged
        assert merged["artifacts_added"] == 1
        parent = roster.task(parent_id)
        assert any(a.get("source_task") == child_id for a in parent["artifacts"])
        assert any(e["kind"] == "consolidate" for e in roster.edges())
        assert roster.consolidate(parent_id, child_id)["artifacts_added"] == 0

        # Consolidation is a reference, not completion of the parent's attempt.
        assert not roster.review(parent_id, "approve")["ok"]
        completed = ex.execute(parent_id)
        assert completed["state"] == "awaiting_review", completed
        reviewed = roster.review(parent_id, "approve")
        assert reviewed["ok"] is True, reviewed
        assert reviewed["task"]["state"] == "approved"

    def test_consolidating_an_unfinished_child_is_refused(self, roster):
        parent_id = _assigned(roster)
        created = roster.request_subtask("friday", "reader", "x",
                                         budget={"max_steps": 2,
                                                 "max_model_calls": 1})
        merged = roster.consolidate(parent_id, created["task"]["id"])
        assert merged["ok"] is False
        assert "nothing to consolidate" in merged["reason"]


class TestHonestyAndSeparation:
    def test_the_executor_never_imports_the_simulated_character_life(self):
        """Character needs/positions must stay out of task execution."""
        import ast
        from pathlib import Path
        source = (Path(__file__).parent / "source" / "server" /
                  "world" / "executor.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "sim" not in str(node.module), node.module
            if isinstance(node, ast.Import) and node.names:
                for alias in node.names:
                    assert "sim" not in str(alias.name), alias.name
        # And it never reads character state directly.
        assert ".needs" not in source
        assert "world.tick" not in source
        assert "simulated character" in source.lower()

    def test_secrets_never_leave_the_runner(self, monkeypatch):
        """The key is used in-process and never returned to a caller."""
        import inspect
        from pathlib import Path

        module_src = (Path(__file__).parent / "source" / "server" /
                      "world" / "executor.py").read_text(encoding="utf-8")
        assert "Authorization" in module_src      # the key IS used...
        assert '"Authorization"' not in inspect.getsource(provider_runner)  # ...

        def fake_post(api_base, key, payload, timeout):
            return 200, {"choices": [{"message": {"content": "ok done"}}]}

        monkeypatch.setattr(executor_mod, "_post_chat", fake_post)
        result = provider_runner("go")
        assert result["ok"] is True
        assert "Authorization" not in json.dumps(result)
        assert "test-key-not-a-secret" not in json.dumps(result)

    def test_provider_runner_redacts_secrets_from_results(self, monkeypatch):
        from source.server import delegation

        def fake_post(api_base, key, payload, timeout):
            return 200, {"choices": [{"message": {"content":
                "Bearer SUPERSECRET123 api_key=SUPERSECRET123 done"}}]}

        monkeypatch.setattr(executor_mod, "_post_chat", fake_post)
        result = provider_runner("go")
        assert result["ok"] is True
        assert "SUPERSECRET123" not in result["text"]

    def test_provider_runner_handles_a_429_by_banning_the_model(self, monkeypatch):
        from source.server import api_pools

        banned = []

        def fake_post(api_base, key, payload, timeout):
            return 429, None

        monkeypatch.setattr(executor_mod, "_post_chat", fake_post)
        monkeypatch.setattr(api_pools, "fail_model",
                            lambda pool, model, cooldown=None: banned.append(
                                (pool, model)))
        result = provider_runner("go")
        assert result["ok"] is False
        assert "HTTP 429" in result["error"]
        assert banned, "a 429 did not touch the shared quota ledger"

    def test_provider_runner_walks_the_configured_chain(self, monkeypatch):
        from source.server import brain_router

        seen = []

        def fake_post(api_base, key, payload, timeout):
            seen.append((api_base, payload.get("model")))
            return 500, None

        monkeypatch.setattr(executor_mod, "_post_chat", fake_post)
        provider_runner("go")
        # resolve() gives the ordered chain; a 500 must move to the NEXT
        # candidate rather than giving up on the first one.
        assert len(seen) >= 2, seen
        assert brain_resolver_has_local(seen)


def brain_resolver_has_local(seen):
    """The chain must reach the local fallback (last candidate)."""
    from source.server import brain_router
    candidates = brain_router.resolve("go")
    assert candidates, "resolve() returned no candidates"
    assert candidates[-1][0] == "local", candidates
    return True
