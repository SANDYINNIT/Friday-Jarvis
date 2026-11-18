"""App-foundation integration: HUD bridge contract + Future Lab wiring (ZB-APP).

These cover the seams this round introduced:

* `WebviewBridge` exposes every method the redesigned frontend calls, and
  degrades honestly (nulls / `available: False`, never a plausible guess) when
  there is no interpreter behind it.
* `stop_turn` delegates to the same owner-scoped boundary as `POST /chat/cancel`:
  lifecycle cancellation and the active turn's Stop event run on the server loop.
* `lab_state` runs the world off a temp database (never `~/.friday`), ticks
  without a planner, survives save/load, and supports the assignment → review
  flow end to end.
* No fake status: the bridge must not invent a model, a TTS stage or a queue
  depth when none is measurable.
"""

import json
import asyncio
import os
import sys
import tempfile
import time
from threading import Event

sys.path.insert(0, os.getcwd())

import pytest

from source.server import lab_state as lab
from source.server.status_ui import StatusBus, WebviewBridge
from test_request_reply_ownership import (
    isolated_server as owned_server, admit_local, drain,
)


@pytest.fixture(autouse=True)
def _isolated_world(tmp_path, monkeypatch):
    """Point the lab at a temp database and reset it between tests.

    `reset()` (not `stop()`) is required: stop() keeps the world loaded on
    purpose, which otherwise leaks ticks from one test into the next.
    """
    monkeypatch.setattr(lab, "db_path",
                        lambda: str(tmp_path / "world.db"))
    from source.server.world import executor as executor_module
    monkeypatch.setattr(executor_module, "provider_runner", lambda goal, **_: {
        "ok": True, "text": "Synthetic result: " + goal,
        "provider": "fixture", "model": "fixture",
    })
    lab.reset()
    yield
    thread = lab._executor_thread
    if thread is not None:
        thread.join(timeout=4)
        assert not thread.is_alive(), "synthetic worker did not stop before store teardown"
    lab._execution_queue_reset()
    lab.reset()


@pytest.fixture()
def bridge():
    return WebviewBridge(StatusBus(), None)      # interpreter deliberately None


# ---------------------------------------------------------------------------
class TestBridgeContract:
    REQUIRED = ("get_status", "get_chat", "get_settings", "get_skills",
                "get_memory", "add_memory", "update_memory", "delete_memory",
                "get_ai_agents", "get_schedule", "get_startup", "set_startup",
                "get_tasks", "get_roster", "get_world", "get_events",
                "get_core", "get_turn", "stop_turn", "world_command",
                "assign_task", "review_task", "send_text", "set_paused")

    def test_every_frontend_method_exists(self, bridge):
        missing = [name for name in self.REQUIRED
                   if not callable(getattr(bridge, name, None))]
        assert not missing, missing

    def test_methods_are_callable_without_an_interpreter(self, bridge):
        """The HUD must render honest nulls during boot, never raise."""
        assert bridge.get_core()["available"] is False or \
            isinstance(bridge.get_core().get("brain"), dict)
        turn = bridge.get_turn()
        assert turn["active"] is False
        assert turn["stop_available"] is False
        assert turn["queued"] == 0

    def test_no_status_is_invented_without_a_router(self, bridge):
        core = bridge.get_core()
        # No brain_router in this fixture, so no model may be claimed.
        assert core["brain"]["model"] is None
        assert core["brain"]["pool"] is None
        assert core["chain_step"] == ""

    def test_prompt_render_and_provider_truth_come_from_live_modules(self, bridge):
        core = bridge.get_core()
        assert core["prompt_render"]["available"] is True
        assert {"hits", "misses", "last_render_seconds"} <= set(
            core["prompt_render"])
        # 11 providers are configured on this machine; if the import failed the
        # list would be empty, which the UI shows as unavailable.
        assert isinstance(core["providers"], list)
        assert isinstance(core["quota"], dict)
        assert len(core["quota"]) == len(core["providers"])

    def test_turn_payload_shape_is_json_safe(self, bridge):
        json.dumps(bridge.get_turn())
        json.dumps(bridge.get_core())


class TestStopTurn:
    def test_stop_with_nothing_running_is_idempotent(self, bridge):
        result = bridge.stop_turn("test")
        assert result == {"ok": True, "stopped_active_turn": False,
                          "cancelled_requests": 0}

    def test_stop_cancels_queued_lifecycle_records(self, bridge, owned_server):
        from source.server.chat_lifecycle import CANCELLED, ChatRequest

        interpreter, _ = owned_server
        bridge._interpreter = interpreter
        registry = interpreter.server_state["chat_requests"]
        queued = registry.register(ChatRequest("tok", "hello", 30))
        async def scenario():
            await interpreter.server.app.router.startup()
            owner = interpreter.server_state["turn_ownership"]
            result = await asyncio.to_thread(bridge.stop_turn, "test")
            assert result == {"ok": True, "stopped_active_turn": False,
                              "cancelled_requests": 1}
            assert queued.state == CANCELLED and queued.may_run is False
            assert not interpreter.stop_event.is_set()
            assert interpreter.server_state["turn_ownership"] is owner
        asyncio.run(scenario())

    def test_stop_sets_the_documented_mechanism_only_when_active(self, bridge, owned_server):
        interpreter, _ = owned_server
        bridge._interpreter = interpreter

        async def scenario():
            await interpreter.server.app.router.startup()
            inactive = await asyncio.to_thread(bridge.stop_turn, "ui")
            assert inactive["ok"] and not inactive["stopped_active_turn"]
            assert not interpreter.stop_event.is_set()
            await admit_local(interpreter, "hud")
            state = interpreter.server_state
            owner = state["turn_ownership"]
            result = await asyncio.to_thread(bridge.stop_turn, "ui")
            assert result["ok"] and result["stopped_active_turn"]
            assert interpreter.stop_event.is_set()
            assert state.get("barge_in") is True
            assert state["cancelled_owner"] is owner
            assert state["turn_ownership"] is owner
            interpreter.complete("Cancelled result must not become a reply")
            await drain(interpreter)
        asyncio.run(scenario())

    @pytest.mark.parametrize("callback", ["missing", "raises"])
    def test_stop_does_not_acknowledge_an_unavailable_owner_boundary(self, bridge, callback):
        from types import SimpleNamespace

        event = Event()
        owner = object()
        state = {"turn_ownership": owner}
        interpreter = SimpleNamespace(server_state=state, stop_event=event)
        if callback == "raises":
            def fail(_reason):
                raise RuntimeError("private diagnostic must not be exposed")
            interpreter.cancel_owned_turn = fail
        bridge._interpreter = interpreter
        result = bridge.stop_turn("ui")
        assert result["ok"] is False
        assert result["stopped_active_turn"] is False
        assert result["cancelled_requests"] == 0
        assert "not confirmed" in result["reason"]
        assert "private diagnostic" not in result["reason"]
        assert state == {"turn_ownership": owner} and not event.is_set()

    def test_stop_records_an_event(self, bridge):
        bridge.stop_turn("ui")
        types = [e["type"] for e in bridge.get_events(20)["events"]]
        assert "stop" in types


class TestWorldLifecycle:
    def test_world_starts_ticks_and_stops(self, bridge):
        world = bridge.get_world()
        assert world["running"] is True
        assert len(world["characters"]) >= 4
        assert len(world["locations"]) >= 20
        first = world["tick"]
        time.sleep(2.5)
        second = bridge.get_world()["tick"]
        assert second > first, "the world tick did not advance"
        lab.stop()
        assert lab.health()["running"] is False

    def test_world_snapshot_is_json_safe_and_separates_needs(self, bridge):
        world = bridge.get_world()
        json.dumps(world)
        for character in world["characters"]:
            assert "needs" in character and "bar" in character
            assert set(character["needs"]) == {
                "energy", "nourishment", "social", "curiosity"}
            # Bar is the inverted presentation, so both must exist.
            assert character["bar"]["energy"] != character["needs"]["energy"] or \
                character["needs"]["energy"] == 50.0

    def test_world_persists_and_reloads_across_a_restart(self, bridge, tmp_path):
        lab.ensure_started()
        time.sleep(2.5)                          # let it really advance first
        world = bridge.get_world()
        assert world["tick"] >= 1, "world never ticked"
        assert lab.command("save")["ok"] is True

        lab.reset()                              # simulate a process restart
        lab.ensure_started()
        reloaded = bridge.get_world()
        assert reloaded["tick"] >= world["tick"]
        assert len(reloaded["characters"]) == len(world["characters"])

    def test_replay_reads_a_persisted_ledger(self, bridge):
        lab.ensure_started()
        time.sleep(2.5)                          # let a few beats be written
        result = bridge.world_command("replay", {"from_tick": 0})
        assert result["ok"] is True
        assert isinstance(result["events"], list)
        # Either beats were recorded, or the world genuinely produced none yet.
        assert isinstance(result["checkpoints"], list)

    def test_unknown_world_command_is_refused(self, bridge):
        assert bridge.world_command("nope")["ok"] is False

    def test_bad_speed_is_refused(self, bridge):
        assert bridge.world_command("speed", {"speed": -3})["ok"] is False
        assert bridge.world_command("speed", {"speed": 4})["ok"] is True


class TestAssignToReviewFlow:
    def test_end_to_end_assignment_review(self, bridge, monkeypatch):
        from source.server.world.executor import AgentExecutor

        lab.ensure_started()
        _, roster = lab._load_world()
        entered, release = Event(), Event()

        def answer(goal, **kwargs):
            entered.set()
            assert release.wait(4), "test did not release the synthetic worker"
            return {"ok": True, "text": "Synthetic result: " + goal,
                    "provider": "fixture", "model": "fixture"}

        monkeypatch.setattr(lab, "_executor", AgentExecutor(roster, runner=answer, note=lab.note))
        monkeypatch.setattr(lab, "_live_duty_active", lambda: False)
        try:
            # The bridge's default assignment auto-approves a persisted result.
            created = bridge.assign_task("research", "Compare two options", {"max_steps": 5})
            assert created["ok"] and entered.wait(4), created
            task_id = created["task"]["id"]
            owner = next(a for a in bridge.get_roster()["agents"] if a["id"] == "research")
            assert owner["task_id"] == task_id and owner["state"] == "working"
            attempt = roster.task(task_id)["attempt"]
            assert roster.task(task_id)["state"] == "running"
            refused = bridge.review_task(task_id, "approve", attempt)
            assert not refused["ok"] and "running" in refused["reason"]
            assert roster.task(task_id)["state"] == "running"
            thread = lab._executor_thread
            release.set()
            thread.join(timeout=4)
            assert not thread.is_alive()
            assert roster.task(task_id)["state"] == "approved"
            assert not bridge.review_task(task_id, "approve", attempt)["ok"]

            # The documented manual-review path explicitly opts into review.
            manual = lab.assign_task("research", "Review this result", auto_execute=False,
                                     approval_required=True)
            assert manual["ok"], manual
            manual_id = manual["task"]["id"]
            executed = bridge.world_command("execute", {"task_id": manual_id, "background": False})
            assert executed["ok"], executed
            task = roster.task(manual_id)
            assert task["state"] == "awaiting_review" and task["artifacts"]
            reviewed = bridge.review_task(manual_id, "approve", task["attempt"])
            assert reviewed["ok"] and reviewed["task"]["state"] == "approved"
            assert not bridge.review_task(manual_id, "reject", task["attempt"])["ok"]
            assert any(e["kind"] == "review" for e in bridge.get_roster()["edges"])
        finally:
            release.set()
            thread = lab._executor_thread
            if thread is not None:
                thread.join(timeout=4)
            lab._execution_queue_reset()

    def test_assignment_requires_an_explicit_goal(self, bridge):
        assert bridge.assign_task("code", "   ")["ok"] is False

    def test_review_verdict_vocabulary(self, bridge):
        lab.ensure_started()
        created = bridge.assign_task("code", "draft something")
        result = bridge.review_task(created["task"]["id"], "maybe")
        assert result["ok"] is False
        assert "approve or reject" in result["reason"]

    def test_roster_seeds_default_budgeted_agents(self, bridge):
        agents = bridge.get_roster()["agents"]
        assert {a["id"] for a in agents} >= {"friday", "research", "code", "vision"}
        for agent in agents:
            assert agent["budget"].get("max_steps", 0) > 0


class TestHonestyGuards:
    def test_world_db_is_its_own_file_not_personal_memory(self):
        import os
        from pathlib import Path

        path = lab.db_path()
        assert path.endswith("world.db")
        assert "friday_memory" not in path
        assert "friday_tasks" not in path
        # The PRODUCTION default must live in FRIDAY's own state dir and be
        # written down in exactly ONE place. It used to be duplicated as a
        # literal in `lab_state.db_path()`; that copy was removed 2026-11-04 in
        # favour of delegating to `world.store.default_path()` (which also
        # honours the `FRIDAY_WORLD_DB` override), so the literal now lives in
        # store.py and lab_state must call it rather than restate it.
        store_src = Path("source/server/world/store.py").read_text(
            encoding="utf-8")
        assert '".friday"' in store_src and '"world.db"' in store_src
        lab_src = Path("source/server/lab_state.py").read_text(
            encoding="utf-8")
        assert "default_path" in lab_src
        assert '".friday"' not in lab_src          # no second copy of the path
        # And with no override set, the default still resolves to that dir.
        saved = os.environ.pop("FRIDAY_WORLD_DB", None)
        try:
            from source.server.world.store import default_path
            assert str(default_path()).replace("\\", "/").endswith(
                ".friday/world.db")
        finally:
            if saved is not None:
                os.environ["FRIDAY_WORLD_DB"] = saved

    def test_get_tasks_never_fabricates_rows(self, bridge):
        payload = bridge.get_tasks()
        assert set(payload) >= {"tasks", "pending"}
        assert isinstance(payload["tasks"], list)
        assert isinstance(payload["pending"], list)
        for task in payload["tasks"]:
            assert "title" in task and "status" in task

    def test_no_bridge_method_returns_a_hardcoded_vendor_model(self, bridge):
        core = bridge.get_core()
        # brain model must be null or a real runtime value — never "qwen3:8b"
        # typed in as a constant when no router is present.
        if core["brain"]["pool"] is None:
            assert core["brain"]["model"] is None
