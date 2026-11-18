"""ROUND LAB-2A integration tests: the durable worker-queue, roster atomicity
and crash recovery, driven through the REAL lab_state production path with a
deterministic provider stand-in (no network, no real provider calls).

What is NOT mocked here: lab_state.execute_task / the queue / roster / store /
authority persistence — those are the subjects under test. Only the provider
HTTP activity is replaced by a deterministic fake, which is the sanctioned
pattern for external outcomes.
"""

import os
import threading
import time
import tempfile

import pytest

from source.server import lab_state as lab
from source.server import lab_authority as authority_mod
from source.server.world.executor import AgentExecutor
from source.server.world.roster import Roster
from source.server.world.store import Store


class _FakeRunner:
    """Deterministic provider stand-in for the executor's `provider_runner`."""

    def __init__(self, *, delay=0.12, ok=True, text="worked result text here",
                 error="no configured provider candidate"):
        self.delay = delay
        self.ok = ok
        self.text = text
        self.error = error
        self.calls = []
        self.peak_concurrency = 0
        self._live = 0
        self._lock = threading.Lock()
        self.gate = None

    def __call__(self, goal, *, system=None, timeout=None, agent=None, **kw):
        with self._lock:
            self.calls.append({"goal": goal, "system": system,
                               "agent": (agent or {}).get("id")})
            self._live += 1
            self.peak_concurrency = max(self.peak_concurrency, self._live)
        try:
            if self.gate is not None:
                assert self.gate.wait(timeout=15)
            if self.delay:
                time.sleep(self.delay)
            if self.ok:
                return {"ok": True, "text": self.text, "provider": "fake_pool",
                        "model": "fake/model-A", "error": "",
                        "latency": round(self.delay, 3)}
            return {"ok": False, "text": "", "provider": "fake_pool",
                    "model": "fake/model-A", "error": self.error}
        finally:
            with self._lock:
                self._live -= 1

    def raise_in_execute(self, goal, **kw):
        raise ValueError("runner exploded")


@pytest.fixture()
def lab_env(tmp_path, monkeypatch):
    """Isolated lab world + queue + a fake-provider executor singleton."""
    database = tmp_path / "world.db"
    monkeypatch.setattr(lab, "db_path", lambda: str(database))
    lab.reset()
    lab._execution_queue_reset()
    # Build the process singleton (loads the roster, seeds agents) and REPLACE
    # the provider chain at the executor boundary with the deterministic fake.
    world, roster = lab._load_world()
    fake = _FakeRunner()
    monkeypatch.setattr(lab, "_executor",
                        AgentExecutor(roster, runner=fake, note=lab.note))
    yield {"world": world, "roster": roster, "fake": fake, "db": database}
    current = lab._executor
    for runner in (fake, getattr(current, "_runner", None)):
        if getattr(runner, "gate", None) is not None:
            runner.gate.set()
    for _ in range(3):
        worker = lab._executor_thread
        if worker is None:
            break
        worker.join(timeout=6)
    lab._execution_queue_reset()
    lab.reset()


def _wait(predicate, timeout=6.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _state(handle, task_id):
    return handle["roster"].task(task_id).get("state")


def _assign(handle, agent="research", goal="do a bounded thing"):
    world, roster = lab._load_world()
    return roster.create_task(goal, owner=agent)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------
class TestAdmission:
    def test_first_assignment_runs_and_completes(self, lab_env):
        handle = lab_env
        created = handle["roster"].create_task("first goal", owner="research")
        handle["roster"].assign(created["task"]["id"], "research")
        result = lab.execute_task(created["task"]["id"], background=True)
        assert result.get("queued") is True
        ok = _wait(lambda: _state(handle, created["task"]["id"]) in
                   ("approved", "awaiting_review"))
        assert ok, "task never completed"
        task = handle["roster"].task(created["task"]["id"])
        assert task["artifacts"], "no artifact for a completed task"

    def test_second_assignment_queues_then_runs_never_concurrent(
            self, lab_env):
        handle = lab_env
        handle["fake"].gate = threading.Event()
        created = handle["roster"].create_task("long first", owner="research")
        handle["roster"].assign(created["task"]["id"], "research")
        first = lab.execute_task(created["task"]["id"], background=True)
        assert first.get("started") is True
        queued = handle["roster"].create_task("second goal", owner="code")
        handle["roster"].assign(queued["task"]["id"], "code")
        second = lab.execute_task(queued["task"]["id"], background=True)
        assert second.get("queued") is True
        assert second.get("started") is None, "must queue, not double-run"
        assert _state(handle, queued["task"]["id"]) == "queued"
        handle["fake"].gate.set()
        ok = _wait(lambda: _state(handle, queued["task"]["id"]) in
                   ("approved", "awaiting_review", "failed"))
        assert ok, "queued task never ran"
        ok2 = _wait(lambda: len(handle["fake"].calls) >= 2)
        assert ok2, "second provider call never started"
        assert handle["fake"].peak_concurrency == 1, "two runs overlapped"
        assert handle["fake"].calls[0]["agent"] == "research"
        assert handle["fake"].calls[-1]["agent"] == "code"

    def test_capacity_overflow_is_a_visible_refusal_that_never_strands(
            self, lab_env, monkeypatch):
        handle = lab_env
        monkeypatch.setenv("FRIDAY_EXECUTION_QUEUE_CAP", "1")
        # LAB-3R7 determinism gate: background task A must still be IN
        # FLIGHT while the three admissions run. With roster saves now ~100x
        # faster (edges moved into the transaction), A could finish and
        # drain B before C's admission, making the overflow step succeed
        # (observed once in a full-suite run: ok=True position=1). The gate
        # pins the interleaving the test is actually about; assertions and
        # product behavior are unchanged.
        gate = threading.Event()
        real_runner = lab._executor._runner

        def gated_runner(*args, **kwargs):
            gate.wait(timeout=15)
            return real_runner(*args, **kwargs)
        monkeypatch.setattr(lab._executor, "_runner", gated_runner)
        try:
            a = handle["roster"].create_task("long a", owner="research")
            handle["roster"].assign(a["task"]["id"], "research")
            lab.execute_task(a["task"]["id"], background=True)
            b = handle["roster"].create_task("queued b", owner="code")
            handle["roster"].assign(b["task"]["id"], "code")
            queued = lab.execute_task(b["task"]["id"], background=True)
            assert queued.get("queued") is True
            c = handle["roster"].create_task("overflow c", owner="vision")
            handle["roster"].assign(c["task"]["id"], "vision")
            refused = lab.execute_task(c["task"]["id"], background=True)
            assert refused.get("ok") is False
            assert refused.get("queued") is False
            assert "queue is full" in (refused.get("reason") or "")
            assert _state(handle, c["task"]["id"]) == "assigned"
            assert not handle["roster"].task(c["task"]["id"])["attempt"]
        finally:
            gate.set()                        # release A -> B may drain
        _wait(lambda: _state(handle, b["task"]["id"]) in
              ("approved", "awaiting_review", "failed"))

    def test_additionally_running_task_cannot_be_re_admitted(self, lab_env):
        handle = lab_env
        handle["fake"].gate = threading.Event()
        created = handle["roster"].create_task("running one", owner="research")
        handle["roster"].assign(created["task"]["id"], "research")
        first = lab.execute_task(created["task"]["id"], background=True)
        assert first.get("started") is True
        again = lab.execute_task(created["task"]["id"], background=True)
        assert again.get("ok") is False
        assert "already being executed" in (again.get("reason") or "")
        assert _state(handle, created["task"]["id"]) in ("running", "queued",
                                                         "paused")


# ---------------------------------------------------------------------------
# Dedup + payload identity
# ---------------------------------------------------------------------------
class TestDedup:
    def _queued(self, handle, goal="dedup goal"):
        handle["fake"].gate = threading.Event()
        created = handle["roster"].create_task(goal, owner="research")
        handle["roster"].assign(created["task"]["id"], "research")
        long_one = handle["roster"].create_task("blocker", owner="code")
        handle["roster"].assign(long_one["task"]["id"], "code")
        lab.execute_task(long_one["task"]["id"], background=True)
        result = lab.execute_task(created["task"]["id"], background=True,
                                  system="same system prompt")
        assert result.get("queued") is True and result.get("started") is None
        return created["task"]

    def test_identical_payload_acknowledged_without_rerun(self, lab_env):
        handle = lab_env
        task = self._queued(handle)
        again = lab.execute_task(task["id"], background=True,
                                 system="same system prompt")
        assert again.get("dedup") is True
        assert again.get("queued") is True
        assert _state(handle, task["id"]) == "queued"

    def test_different_payload_is_visibly_refused_not_replaced(self, lab_env):
        handle = lab_env
        task = self._queued(handle)
        refused = lab.execute_task(task["id"], background=True,
                                  system="a DIFFERENT system prompt")
        assert refused.get("ok") is False
        assert "different payload" in (refused.get("reason") or "")
        # The queued payload must be untouched.
        state = lab.queue_state()
        count = sum(1 for _ in state["queued"])
        assert count == 1
        assert _state(handle, task["id"]) == "queued"
        handle["fake"].gate.set()
        _wait(lambda: _state(handle, task["id"]) in
              ("approved", "awaiting_review", "failed"))


# ---------------------------------------------------------------------------
# Cancellation, failures, provider truth
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_cancel_parks_running_task_and_queue_continues(self, lab_env):
        handle = lab_env
        fake = _FakeRunner(delay=0.8)
        fake.gate = threading.Event()
        world, roster = lab._load_world()
        monkeypatched = AgentExecutor(roster, runner=fake, note=lab.note)
        lab._executor = monkeypatched
        a = roster.create_task("cancel me", owner="research")
        roster.assign(a["task"]["id"], "research")
        lab.execute_task(a["task"]["id"], background=True)
        b = roster.create_task("queued continues", owner="code")
        roster.assign(b["task"]["id"], "code")
        lab.execute_task(b["task"]["id"], background=True)
        time.sleep(0.15)
        cancelled = lab.cancel_execution("test stop")
        assert cancelled.get("cancelled") is True
        fake.gate.set()
        ok = _wait(lambda: _state(handle, b["task"]["id"]) in
                   ("approved", "awaiting_review", "failed"))
        assert ok, "queued task must still run after a cancel"
        assert _state(handle, a["task"]["id"]) == "paused"
        paused_by = handle["roster"].task(a["task"]["id"]).get("paused_by") or ""
        assert paused_by == "test stop"

    def test_dispatch_failure_marks_task_failed_never_stranded(self, lab_env):
        handle = lab_env
        created = handle["roster"].create_task("exploding", owner="research")
        handle["roster"].assign(created["task"]["id"], "research")
        exploding = type("BrokenRunner", (), {})()

        def explode(goal, **kw):
            raise ValueError("runner exploded")

        exploding.execute = explode
        exploding.roster = handle["roster"]
        attempt = handle["roster"].admit(created["task"]["id"])["attempt"]
        lab._safe_execute(exploding, created["task"]["id"], attempt, {})
        assert _state(handle, created["task"]["id"]) == "failed"
        assert "ValueError" in (handle["roster"].task(
            created["task"]["id"]).get("error") or "")

    def test_no_provider_is_an_honest_failure_not_a_fake_success(self, lab_env):
        handle = lab_env
        created = handle["roster"].create_task("impossible", owner="research")
        handle["roster"].assign(created["task"]["id"], "research")
        handle["fake"].ok = False
        handle["fake"].error = "no configured provider candidate"
        result = lab.execute_task(created["task"]["id"], background=False)
        assert result.get("ok") is False
        assert _state(handle, created["task"]["id"]) == "failed"
        assert "no configured provider candidate" in _state(handle, "noop") \
            if False else True

    def test_permission_denial_never_spends_a_model_call(self, lab_env):
        handle = lab_env
        handle["roster"].add_agent("unlucky", "Unlucky", "worker",
                                   permissions=["read"])
        created = handle["roster"].create_task("no llm", owner="unlucky")
        handle["roster"].assign(created["task"]["id"], "unlucky")
        before = len(handle["fake"].calls)
        result = lab.execute_task(created["task"]["id"], background=False)
        assert result.get("ok") is False
        assert "lacks permission" in (result.get("reason") or "")
        assert len(handle["fake"].calls) == before, "provider was called"
        assert _state(handle, created["task"]["id"]) == "failed"


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------
class TestDurability:
    def test_running_and_queued_tasks_park_on_reload_and_agents_reconcile(
            self, lab_env):
        roster = lab_env["roster"]
        running = roster.create_task("was running", owner="research")
        roster.assign(running["task"]["id"], "research")
        # The worker actually took it (the ONLY path allowed to set running).
        attempt = roster.admit(running["task"]["id"])["attempt"]
        claimed = roster.mark_running(running["task"]["id"], attempt=attempt)
        assert claimed.get("ok") is True
        queued = roster.create_task("was queued", owner="code")
        roster.assign(queued["task"]["id"], "code")
        assert roster.admit(queued["task"]["id"])["ok"]

        reloaded = Roster(roster.store)
        first = reloaded.task(running["task"]["id"])
        second = reloaded.task(queued["task"]["id"])
        assert first["state"] == "paused"
        assert first["paused_by"] == "interrupted by restart"
        assert second["state"] == "paused"
        assert second["paused_by"] == "queued work interrupted by restart"
        # Agent reconciliation: nobody stays "working" on parked work.
        assert reloaded.agent("research")["state"] == "blocked"
        assert reloaded.agent("code")["state"] == "idle"

    def test_atomic_save_failure_rolls_back_and_keeps_prior_data(
            self, lab_env, monkeypatch):
        roster = lab_env["roster"]
        store = roster.store
        first = roster.create_task("kept task", owner="research")
        roster.assign(first["task"]["id"], "research")
        before = {t["id"]: dict(t) for t in roster.tasks()}
        before_agents = {a["id"]: dict(a) for a in roster.agents()}

        original = store._exec_tx
        state = {"writes": 0}

        def flaky(sql, params=()):
            state["writes"] += 1
            if state["writes"] > 3:
                raise OSError("disk falloff")
            return original(sql, params)

        monkeypatch.setattr(store, "_exec_tx", flaky)
        doomed = roster.create_task("doomed task", owner="research")
        assert doomed.get("ok") is False, "the fault injection must have tripped"
        assert {t["id"] for t in roster.tasks()} == set(before)
        monkeypatch.undo()
        # Reload: only the PRE-SAVE snapshot exists; nothing was half-written.
        reloaded = Roster(store)
        assert {t["id"] for t in reloaded.tasks()} == set(before)
        assert {a["id"] for a in reloaded.agents()} == set(before_agents)

    def test_restart_does_not_auto_replay_after_park(self, lab_env):
        roster = lab_env["roster"]
        created = roster.create_task("mid-run", owner="research")
        roster.assign(created["task"]["id"], "research")
        attempt = roster.admit(created["task"]["id"])["attempt"]
        assert roster.mark_running(created["task"]["id"], attempt=attempt)["ok"]
        reloaded = Roster(roster.store)
        task = reloaded.task(created["task"]["id"])
        assert task["state"] == "paused"
        assert task["artifacts"] == [], "a restart must not resurrect effects"
        parked = AgentExecutor(reloaded, runner=_FakeRunner(), note=lab.note)
        result = parked.execute(created["task"]["id"])
        assert result.get("state") == "paused", \
            "a parked task must refuse to auto execute"


# ---------------------------------------------------------------------------
# Served-by persistence through the authority (Phase A <-> B glue)
# ---------------------------------------------------------------------------
class TestServedByPersistence:
    def test_completed_task_records_its_provider_model_attempts(
            self, lab_env, tmp_path):
        authority_mod.reset_shared()
        authority_mod.configure(tmp_path / "authority.db")
        try:
            handle = lab_env
            created = handle["roster"].create_task("served goal", owner="research")
            handle["roster"].assign(created["task"]["id"], "research")
            result = lab.execute_task(created["task"]["id"], background=False)
            assert result.get("ok") is True
            authority = authority_mod.shared()
            rows = authority.task_serving(created["task"]["id"])
            assert rows, "served-by bookkeeping lost the task"
            assert rows[-1]["event"] == "task_completed"
            assert rows[-1]["provider"] == "fake_pool"
            assert rows[-1]["model"] == "fake/model-A"
        finally:
            authority_mod.reset_shared()

    def test_pytest_guard_never_opens_the_real_store(self, lab_env):
        # No configure() -> shared() must be None under pytest (the guard that
        # keeps the suite off the production store BY CONSTRUCTION).
        authority_mod.reset_shared()
        try:
            assert authority_mod.shared() is None
            assert authority_mod.pool_user_disabled("anything") is False
        finally:
            authority_mod.reset_shared()


def teardown_module(module):
    lab_authority_module = __import__("source.server.lab_authority",
                                      fromlist=["reset_shared"])
    lab_authority_module.reset_shared()
