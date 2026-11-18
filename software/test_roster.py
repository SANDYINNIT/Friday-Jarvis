"""Roster tests: assignment, bounded subtasks, review, budgets, rest-safety."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

import pytest

from source.server.world import Store, Roster
from source.server import lab_artifacts


@pytest.fixture()
def roster():
    directory = tempfile.mkdtemp(prefix="friday-roster-")
    store = Store(os.path.join(directory, "world.db"))
    r = Roster(store)
    r.add_agent("friday", "FRIDAY", "coordinator", model="modal/glm",
                permissions=["assign", "review"],
                budget={"max_steps": 20, "max_model_calls": 10})
    r.add_agent("research", "Research", "researcher", model="modal/glm",
                permissions=["read", "search"],
                budget={"max_steps": 5, "max_model_calls": 2})
    r.add_agent("code", "Code", "builder", model="modal/glm",
                permissions=["read"], budget={"max_steps": 3, "max_model_calls": 1})
    yield r
    store.close()


def _claim(roster, task_id):
    attempt = roster.admit(task_id)["attempt"]
    assert roster.mark_running(task_id, attempt=attempt)["ok"]
    return attempt


def _complete(roster, task_id, text):
    attempt = _claim(roster, task_id)
    result = roster.complete(task_id, artifact=text, attempt=attempt,
                             writer=lab_artifacts.for_store(roster.store))
    assert result["ok"], result


class TestAssignment:
    def test_create_and_assign(self, roster):
        created = roster.create_task("Compare two workspace options")
        assert created["ok"] is True
        result = roster.assign(created["task"]["id"], "research")
        assert result["ok"] is True
        # Assignment does not reserve an execution queue slot.
        assert result["task"]["state"] == "assigned"
        assert result["task"]["owner"] == "research"
        assert roster.agent("research")["state"] == "idle"

    def test_assign_unknown_ids_refuse(self, roster):
        task = roster.create_task("something")["task"]
        assert roster.assign(task["id"], "nobody")["ok"] is False
        assert roster.assign("nope", "research")["ok"] is False

    def test_running_task_has_exactly_one_owner(self, roster):
        first = roster.create_task("first")["task"]
        roster.assign(first["id"], "research")
        second = roster.create_task("second")["task"]
        roster.assign(second["id"], "code")
        _claim(roster, first["id"])
        running = [t for t in roster.tasks() if t["state"] == "running"]
        for task in running:
            assert sum(1 for t in roster.tasks() if t["id"] == task["id"]) == 1
        # A second agent cannot silently steal a running task.
        conflict = roster.assign(first["id"], "code")
        assert conflict["ok"] is False
        assert "running" in conflict["reason"]

    def test_an_empty_goal_is_refused(self, roster):
        assert roster.create_task("   ")["ok"] is False
        assert roster.create_task("")["ok"] is False


class TestBoundedSubtasks:
    def test_subtask_requires_an_explicit_goal(self, roster):
        result = roster.request_subtask("friday", "research", "",
                                        budget={"max_steps": 3})
        assert result["ok"] is False
        assert "goal" in result["reason"]

    def test_subtask_requires_a_positive_budget(self, roster):
        result = roster.request_subtask("friday", "research", "survey sources",
                                        budget={"max_steps": 0})
        assert result["ok"] is False
        assert "budget" in result["reason"] or "max_steps" in result["reason"]

    def test_subtask_with_goal_and_budget_creates_a_real_edge(self, roster):
        result = roster.request_subtask(
            "friday", "research", "Survey three sources",
            budget={"max_steps": 4, "max_model_calls": 2},
            permissions=["search"])
        assert result["ok"] is True, result
        assert result["task"]["state"] == "assigned"
        edges = roster.edges()
        subtask_edges = [e for e in edges if e["kind"] == "subtask"]
        assert len(subtask_edges) == 1
        assert subtask_edges[0]["from"] == "friday"
        assert subtask_edges[0]["to"] == "research"
        assert "Survey three sources" in subtask_edges[0]["message"]
        # permissions were granted, bounded
        assert "search" in roster.agent("research")["permissions"]
        assert roster.agent("research")["budget"]["max_steps"] == 4

    def test_unknown_agent_in_request_refuses(self, roster):
        result = roster.request_subtask("friday", "ghost", "x",
                                        budget={"max_steps": 2})
        assert result["ok"] is False


class TestReviewAndCompletion:
    def test_review_approve_and_reject(self, roster):
        task = roster.create_task("draft a proposal", approval_required=True)["task"]
        roster.assign(task["id"], "code")
        _complete(roster, task["id"], "proposal.md")
        assert roster.task(task["id"])["state"] == "awaiting_review"

        approved = roster.review(task["id"], "approve")
        assert approved["ok"] is True
        assert approved["task"]["state"] == "approved"

        other = roster.create_task("second", approval_required=True)["task"]
        roster.assign(other["id"], "research")
        _complete(roster, other["id"], "second proposal")
        rejected = roster.review(other["id"], "reject")
        assert rejected["ok"] is True
        assert rejected["task"]["state"] == "rejected"

    def test_review_vocabulary_is_checked(self, roster):
        task = roster.create_task("x")["task"]
        roster.assign(task["id"], "research")
        assert roster.review(task["id"], "maybe")["ok"] is False
        assert "approve or reject" in roster.review(task["id"], "maybe")["reason"]

    def test_fail_records_an_honest_error(self, roster):
        task = roster.create_task("call the provider")["task"]
        roster.assign(task["id"], "research")
        failed = roster.fail(task["id"], "model_unavailable", attempt=_claim(roster, task["id"]))
        assert failed["ok"] is True
        assert failed["task"]["state"] == "failed"
        assert failed["task"]["error"] == "model_unavailable"
        assert roster.agent("research")["state"] == "error"
        assert roster.agent("research")["last_error"] == "model_unavailable"

    def test_mark_error_is_the_budget_fallback(self, roster):
        task = roster.create_task("t")["task"]
        roster.assign(task["id"], "code")
        roster.mark_error(task["id"], attempt=_claim(roster, task["id"]))
        assert roster.task(task["id"])["state"] == "failed"
        assert roster.task(task["id"])["error"] == "model_unavailable"


class TestBudgets:
    def test_exceeds_budget_compares_steps_to_the_agent_budget(self, roster):
        task = roster.create_task("t")["task"]
        roster.assign(task["id"], "code")           # code has max_steps 3
        assert roster.exceeds_budget(task["id"]) is False
        roster._tasks[task["id"]]["steps"] = 3
        assert roster.exceeds_budget(task["id"]) is True

    def test_unknown_task_is_not_over_budget(self, roster):
        assert roster.exceeds_budget("missing") is False


class TestRestSafety:
    def test_resting_pauses_and_never_drops_a_real_task(self, roster):
        task = roster.create_task("explain the position")["task"]
        roster.assign(task["id"], "research")
        paused = roster.pause_for_rest(task["id"], "research")
        assert paused["ok"] is True
        assert paused["task"]["state"] == "paused"
        assert paused["task"]["paused_by"] == "research"
        # The task still exists with its goal intact.
        stored = roster.task(task["id"])
        assert stored["goal"] == "explain the position"
        assert stored["owner"] == "research"
        assert "not dropped" in paused["note"]

    def test_resume_returns_it_to_admission(self, roster):
        task = roster.create_task("t")["task"]
        roster.assign(task["id"], "research")
        roster.pause_for_rest(task["id"], "research")
        resumed = roster.resume(task["id"])
        assert resumed["ok"] is True
        assert resumed["task"]["state"] == "assigned"
        assert resumed["task"]["paused_by"] is None

    def test_resuming_a_non_paused_task_refuses(self, roster):
        task = roster.create_task("t")["task"]
        roster.assign(task["id"], "research")
        assert roster.resume(task["id"])["ok"] is False

    def test_paused_task_survives_a_restart(self, roster):
        task = roster.create_task("keep me")["task"]
        roster.assign(task["id"], "research")
        roster.pause_for_rest(task["id"], "research")
        revived = Roster(roster.store)          # reload from the same sqlite
        stored = revived.task(task["id"])
        assert stored["state"] == "paused"
        assert stored["goal"] == "keep me"
        assert revived.resume(task["id"])["ok"] is True


class TestSafetyAndShape:
    def test_every_return_is_json_safe(self, roster):
        created = roster.create_task("json check")
        roster.assign(created["task"]["id"], "research")
        _complete(roster, created["task"]["id"], "out.md")
        payload = json.dumps(roster.snapshot())
        assert "agents" in payload and "edges" in payload

    def test_bad_ids_never_raise(self, roster):
        assert roster.assign("nope", "nobody")["ok"] is False
        assert roster.review("nope", "approve")["ok"] is False
        assert roster.fail("nope", "x")["ok"] is False
        assert roster.pause_for_rest("nope", "c")["ok"] is False
        assert roster.resume("nope")["ok"] is False
        assert roster.task("nope") == {}
        assert roster.agent("nope") == {}

    def test_roster_state_persists(self, roster):
        task = roster.create_task("persist me")["task"]
        roster.assign(task["id"], "research")
        roster.request_subtask("friday", "code", "check the draft",
                               budget={"max_steps": 2})
        reloaded = Roster(roster.store)
        assert [t["id"] for t in reloaded.tasks()] == [t["id"] for t in roster.tasks()]
        assert len(reloaded.agents()) == 3
        assert len(reloaded.edges()) == len(roster.edges())

    def test_edges_keep_from_to_keys_across_a_reload(self, roster):
        """Regression: the table stores src/dst but the graph reads from/to.

        Measured defect: `_save()` wrote `from`/`to` while `_load()` read the
        raw `src`/`dst` columns, so after a restart every edge round-tripped to
        BOTH ends being None - the AI Roster drew no handoffs at all, and the
        corrupted rows persisted.
        """
        task = roster.create_task("edge round trip")["task"]
        roster.assign(task["id"], "research")
        roster.request_subtask("friday", "code", "sub", budget={"max_steps": 2})
        before = [dict(e) for e in roster.edges()]
        assert before, "no edges were recorded"

        reloaded = Roster(roster.store)
        after = reloaded.edges()
        assert len(after) == len(before)
        for original, restored in zip(before, after):
            assert set(original) == set(restored), (original, restored)
            assert restored["from"], "an edge lost its source after reload"
            assert restored["to"], "an edge lost its target after reload"
            assert restored["from"] == original["from"]
            assert restored["to"] == original["to"]

        # ...and saving again must not grow or null anything.
        reloaded._save()
        again = Roster(roster.store).edges()
        assert len(again) == len(before)
        assert all(e["from"] and e["to"] for e in again)
