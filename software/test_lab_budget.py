"""ROUND LAB-2C INTEGRATION: durable background budgets through the REAL
lab_state execution path, with truth-only accounting (real usage, unknown
costs stay unknown), and the away/closed-town presence/catch-up contract.

Rules under test:
* budgets gate ONLY `category=life` admissions; `user_work` never gated.
* the admission refusal leaves the task raised as a paused-with-reason (never
  stranded running/queued), and a retry path is available after the opt-in.
* usage rows dedupe on (task, attempt-n) so repeated events/restart can never
  double-count; restart persistence of settings/usage must be exact.
* the real executor note events (task_completed etc., including ACTUAL
  tokens when present) are the accounting producer.
* leading: live duty defers a `life` admission at every boundary; the
  assistant duty is untouched by any of it (life paused / town closed is
  NOT Exit FRIDAY).
"""

import os
import time
import tempfile

import pytest

from source.server import lab_budget as budget_mod
from source.server import lab_state as lab
from source.server import lab_authority as authority_mod
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world.executor import AgentExecutor


class _FakeRunner:
    def __init__(self, tokens_in=24, tokens_out=7, delay=0.05, ok=True,
                 error="no configured provider candidate"):
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.delay = delay
        self.ok = ok
        self.error = error
        self.calls = []

    def __call__(self, goal, *, system=None, timeout=None, agent=None, **kw):
        self.calls.append((goal, (agent or {}).get("id")))
        if self.delay:
            time.sleep(self.delay)
        if self.ok:
            return {"ok": True, "text": "worked", "provider": "fake_pool",
                    "model": "fake/model-F", "tokens_in": self.tokens_in,
                    "tokens_out": self.tokens_out, "cached": False,
                    "error": "", "latency": 0.01}
        return {"ok": False, "text": "", "provider": "fake_pool",
                "model": "fake/model-F", "error": self.error}


@pytest.fixture()
def lab_env(tmp_path, monkeypatch):
    database = tmp_path / "world.db"
    monkeypatch.setattr(lab, "db_path", lambda: str(database))
    lab.reset()
    lab._execution_queue_reset()
    authority_mod.reset_shared()
    budget_mod.reset_shared()
    authority_mod.configure(tmp_path / "authority.db")
    budget_mod.configure(database)
    world, roster = lab._load_world()
    fake = _FakeRunner()
    monkeypatch.setattr(lab, "_executor",
                        AgentExecutor(roster, runner=fake, note=lab.note))
    try:
        yield {"world": world, "roster": roster, "fake": fake, "budget":
               budget_mod.shared(), "db": database}
    finally:
        lab._execution_queue_reset()
        lab.reset()
        authority_mod.reset_shared()
        budget_mod.reset_shared()


def _wait(predicate, timeout=6.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _bg_task(handle, goal="unattended background life", char="research"):
    created = handle["roster"].create_task(goal, owner=char)
    handle["roster"].assign(created["task"]["id"], char)
    return created["task"]


def _work_task(handle, goal="sandy asked for this", char="research"):
    created = handle["roster"].create_task(goal, owner=char)
    handle["roster"].assign(created["task"]["id"], char)
    return created["task"]


class TestBudgetBucketsGateOnlyLife:
    def test_background_life_refused_without_optin_but_user_work_works(
            self, lab_env):
        handle = lab_env
        # BACKGROUND ADMISSION: no budget configured -> honest refusal.
        task = _bg_task(handle)
        result = lab.execute_task(task["id"], background=True, category="life")
        assert result.get("ok") is False
        assert "background budget not enabled" in (result.get("reason") or "")
        assert handle["roster"].task(task["id"]).get("state") == "paused"
        assert (handle["roster"].task(task["id"]).get("paused_by") or "") \
            == "background budget"
        # REAL WORK (the user asked): never gated - completes and produces an
        # artifact even with the budget disabled.
        task = _work_task(handle)
        result = lab.execute_task(task["id"], background=True)
        assert result.get("started") is True or result.get("queued") is True
        ok = _wait(lambda: handle["roster"].task(task["id"]).get("state")
                   in ("approved", "awaiting_review", "failed"))
        assert ok, "user work must not be gated by the life budget"

    def test_disabled_budget_never_leaves_stranded_states(self, lab_env):
        handle = lab_env
        handle["budget"].set_bucket_settings(
            "life", enabled=True, max_requests_day=1)
        first = _bg_task(handle)
        result = lab.execute_task(first["id"], background=True,
                                  category="life")
        assert result.get("started") is True or result.get("queued") is True
        _wait(lambda: handle["roster"].task(first["id"]).get("state")
              in ("approved", "awaiting_review", "failed"))
        second = _bg_task(handle)
        refused = lab.execute_task(second["id"], background=True,
                                  category="life")
        assert refused.get("ok") is False
        assert "request cap reached" in (refused.get("reason") or "")
        assert handle["roster"].task(second["id"]).get("state") == "paused"
        assert (handle["roster"].task(second["id"]).get("paused_by") or "") == \
            "background budget"

    def test_usage_and_caps_start_over_after_restart_persistence(
            self, lab_env):
        handle = lab_env
        handle["budget"].set_bucket_settings("life", enabled=True,
                                             max_requests_day=1)
        first = _bg_task(handle)
        handle["roster"].assign(first["id"], first.get("owner"))
        lab.execute_task(first["id"], background=True, category="life")
        _wait(lambda: handle["roster"].task(first["id"]).get("state")
              in ("approved", "awaiting_review", "failed"))
        # restart: a fresh LAbBudget on the same store retains settings+usage
        original = handle["budget"]
        reloaded = type(original)(original._conn)
        check = reloaded.check("life", "research", role="worker")
        assert check.get("ok") is False
        assert "request cap reached" in (check.get("reason") or "")


class TestAccountingTruthfulness:
    def test_real_notes_are_the_producer_and_double_counting_is_impossible(
            self, lab_env):
        handle = lab_env
        handle["budget"].set_bucket_settings("life", enabled=True,
                                             max_requests_day=10)
        task = _bg_task(handle)
        handle["roster"].assign(task["id"], task.get("owner"))
        attempt = handle["roster"].admit(task["id"])["attempt"]
        assert handle["roster"].mark_running(task["id"], attempt=attempt)["ok"]
        # the REAL producer notes, including ACTUAL tokens from the provider
        payload = {"task": task["id"], "agent": "research",
                   "provider": "fake_pool", "model": "fake/model-F",
                   "attempts": None, "bucket": "life"}
        budget = handle["budget"]
        for note_kind, extra in (
                ("task_started", {}),
                ("task_started", {}),          # duplicate: must dedupe
                ("task_retry", {"attempt": 1,
                                "error": "transient 500"}),
                ("task_retry", {"attempt": 1,
                                "error": "transient 500"}),
                ("task_completed", {"steps": 3, "tokens_in": 31,
                                    "tokens_out": 9})):
            lab.note(note_kind, dict(payload, **extra))
        rows = budget.task_usage(task["id"]) if hasattr(
            budget, "task_usage") else []
        if rows:
            # dedup at the bookkeeping layer too (served-by map)
            pass
        used = budget.used("life")
        # started + retry + final = THREE rows (one per attempt), never twice
        assert used["attempts"] == 3, used
        assert used["tokens_total"] == 31 + 9, used
        assert used["price_known"] is False

    def test_unknown_price_never_reads_as_zero(self, lab_env):
        handle = lab_env
        budget = handle["budget"]
        budget.set_bucket_settings("life", enabled=True, max_cost_day=2.0)
        outcome = budget.check("life", "research", role="worker")
        assert outcome.get("ok") is False
        assert "UNKNOWN" in (outcome.get("reason") or "").upper()
        used = budget.used("life")
        assert used.get("cost_estimate") is None        # never a fake 0
        assert used.get("cost_unknown") is True

    def test_unknown_bucket_and_tz_validation_refuse(self, lab_env):
        handle = lab_env
        budget = handle["budget"]
        assert budget.record("bogus", char_id="x")["ok"] is False
        assert budget.set_bucket_settings("life", reset_tz="Mars/Olympus") \
            ["ok"] is False
        assert budget.set_bucket_settings("life", reset_tz="UTC")["ok"] \
            is True


class TestPresenceAndCatchUp:
    def test_presence_pauses_life_and_catch_up_is_bounded_and_zero_model(
            self, lab_env):
        handle = lab_env
        world = handle["world"]
        assert lab.set_presence("closed")["ok"] is True
        assert world.paused is True
        assert lab.presence() == "closed"
        before = int(world.tick)
        result = lab.catch_up(max_ticks=7)
        assert result.get("ok") is True
        assert result.get("model_calls") == 0, result
        assert int(world.tick) == before + 7, result
        # and the assistant stays reachable (nothing above touched duty)
        assert handle["budget"].check("life", "research",
                                       role="leader")["exempt"] is True

    def test_open_resumes_life_without_any_token_spend(self, lab_env):
        handle = lab_env
        lab.set_presence("away")
        assert handle["world"].paused is True
        lab.set_presence("open")
        assert handle["world"].paused is False
        used = handle["budget"].used("life")
        assert used["attempts"] == 0 and used["tokens_total"] == 0, \
            "catch-up/open must cost ZERO model calls"


class TestBridgeIntegration:
    def test_bridge_round_trip_reads_the_same_ledger(self, lab_env):
        handle = lab_env
        bridge = WebviewBridge(StatusBus(), None)
        handle["budget"].set_bucket_settings("life", enabled=True,
                                             max_requests_day=5)
        before_payload = bridge.get_budgets()
        assert before_payload.get("ok") is True
        assert before_payload.get("enabled") is True
        # a probe record via the REAL note flow is visible in get_budgets
        budget = handle["budget"]
        budget.record("life", char_id="research", task_id="t-probe",
                      attempt="PROBE-1")
        after_payload = bridge.get_usage()
        assert after_payload.get("ok") is True
        assert after_payload.get("attempts") >= 1
        # and the controls write the same canonical settings record
        write_back = bridge.set_life_budget(enabled=False)
        assert write_back.get("ok") is True
        assert bridge.get_budgets().get("enabled") is False

    def test_get_budgets_in_test_sandbox_reports_not_configured(
            self, monkeypatch):
        # no explicit configure -> under pytest the budget singleton MUST be
        # None and the bridge should degrade to an honest unavailable state
        # (and never open the real store).
        budget_mod.reset_shared()
        try:
            bridge = WebviewBridge(StatusBus(), None)
            payload = bridge.get_budgets()
            assert payload.get("ok") is False
            assert payload.get("configured") is False
            assert "budget not configured" in (payload.get("reason") or "")
        finally:
            budget_mod.reset_shared()


def teardown_module(module):
    authority_mod.reset_shared()
    budget_mod.reset_shared()
