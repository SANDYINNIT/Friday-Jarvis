"""Real Light dispatch, brain router and vision transport with fake I/O."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from source.server import brain_router as br, identity_integration as ii, screen_understanding as vision, server
from test_request_reply_ownership import admit_local, drain, isolated_server

MODAL_BASE = "https://private-fixture.invalid/v1"


def manifest(*, modal=True):
    deployments = {"brain_groq": {"deployment": "groq-main", "base": br.GROQ_BASE},
                   "vision_gemini": {"deployment": "gemini-main", "base": vision.GEMINI_BASE}}
    if modal:
        for pool in ("brain_modal", "vision_modal"):
            deployments[pool] = {"deployment": "modal-main", "base": MODAL_BASE}
    return {"complete": True, "deployments": deployments, "services": []}


class Pool:
    def __init__(self, key="fake-key"):
        self.key = key

    @property
    def empty(self):
        return not self.key

    def next_key(self):
        return self.key

    def succeed(self, key):
        return None


def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(br, "MODAL_BASE", MODAL_BASE)
    monkeypatch.setattr(br, "MODAL_MODEL", "vendor/model")
    monkeypatch.setenv("FRIDAY_MODAL_VISION_MODEL", "vendor/model")
    monkeypatch.setattr(br, "GROQ_BASE", "https://groq-fixture.invalid/v1")
    monkeypatch.setattr(vision, "GEMINI_BASE", "https://gemini-fixture.invalid/v1")
    monkeypatch.setattr(vision, "VISION_GEMINI_CHAIN", ["vision-model"])
    monkeypatch.setenv("FRIDAY_IDENTITY_INTEGRATION", "1")
    monkeypatch.setenv("FRIDAY_IDENTITY_DEPLOYMENTS", json.dumps(manifest()))
    monkeypatch.setenv("FRIDAY_IDENTITY_CATALOG_DB", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("FRIDAY_IDENTITY_HISTORY_DB", str(tmp_path / "duty.db"))
    pools = {name: Pool() for name in ("brain_modal", "brain_groq", "vision_modal", "vision_gemini")}
    monkeypatch.setattr(br.api_pools, "pool", pools.get)
    monkeypatch.setattr(br.api_pools, "model_banned", lambda *_: False)
    monkeypatch.setattr(br.api_pools, "fail_model", lambda *_, **__: None)
    monkeypatch.setattr(br.api_pools, "model_succeed", lambda *_: None)
    return pools


@pytest.fixture
def wired(monkeypatch, tmp_path, request):
    pools = configure(monkeypatch, tmp_path)
    monkeypatch.setitem(br._LIVE_ROUTER, "router", None)
    interpreter, _ = request.getfixturevalue("isolated_server")
    # The shared Light fixture stubs its router to test transport. Restore the
    # production router here, keeping provider pools and transport synthetic.
    interpreter.server_state["brain_router"] = br.BrainRouter(enabled=True)
    tracker = interpreter.server_state["identity_integration"]
    assert tracker is not None
    interpreter.server_state["brain_router"].identity_integration = tracker
    return interpreter, tracker, pools


def answer(text="Fixture screen"):
    return SimpleNamespace(status_code=200, json=lambda: {"choices": [
        {"message": {"content": text}, "finish_reason": "stop"}]})


def test_real_light_fallback_and_generation_fence(wired, monkeypatch):
    interpreter, integration, _ = wired
    state = interpreter.server_state
    shadow = integration.shadow
    observations = []

    def fake_post(url, **kwargs):
        observations.append(shadow.snapshot()["duty"]["current"])
        return answer()

    monkeypatch.setattr(vision.requests, "post", fake_post)
    original = interpreter._respond_and_store

    def respond_with_vision():
        assert vision._cloud_vision(b"fixture-image", "Describe")[0]
        yield from original()

    interpreter._respond_and_store = respond_with_vision

    async def scenario():
        await interpreter.server.app.router.startup()
        await admit_local(interpreter, "hud")
        old = state["turn_attempt"]
        assert shadow.snapshot()["duty"]["current"]["brain"]["route"]["provider"] == "modal"
        interpreter.frames = [{"role": "server", "type": "error", "content": "503 fixture"}]
        interpreter.producer()
        interpreter.respond_thread = None
        await drain(interpreter)
        assert state["turn_attempt"] is not old
        both = observations[0]
        assert set(both) == {"brain", "vision"}
        assert both["brain"]["character_id"] == both["vision"]["character_id"]
        assert both["brain"]["request_id"] == both["vision"]["request_id"]
        current = shadow.snapshot()["duty"]
        assert current["current"]["brain"]["route"]["provider"] == "groq"
        assert current["current"]["brain"]["generation"] > both["brain"]["generation"]
        assert current["last_success"]["vision"]["route"]["provider"] == "modal"
        assert "brain" not in current["last_success"]
        failures = shadow.snapshot()["route_failures"]
        assert [(row["route"]["provider"], row["capability"]) for row in failures] == [("modal", "brain")]
        assert failures[0]["character_id"] != current["current"]["brain"]["character_id"]

        stale = server._AttemptOutput(old, {"role": "server", "type": "status", "content": "complete"})
        interpreter.output_queue.sync_q.put(stale)
        await drain(interpreter)
        assert shadow.snapshot()["duty"] == current
        assert shadow.snapshot()["route_failures"] == failures
        interpreter._respond_and_store = original
        interpreter.complete("Fixture answer from fallback.")
        await drain(interpreter)
        done = shadow.snapshot()["duty"]
        assert not done["current"]
        assert done["last_success"]["brain"]["route"]["provider"] == "groq"
        assert done["last_success"]["vision"] == current["last_success"]["vision"]

    asyncio.run(scenario())


def test_real_vision_fallback_only_records_verified_transport(wired, monkeypatch):
    interpreter, integration, pools = wired
    shadow = integration.shadow
    state = interpreter.server_state
    seen = []

    def fake_post(url, **kwargs):
        active = shadow.snapshot()["duty"]["current"]["vision"]
        seen.append(active["route"]["provider"])
        if url.startswith(MODAL_BASE):
            return SimpleNamespace(status_code=503)
        return answer()

    monkeypatch.setattr(vision.requests, "post", fake_post)
    original = interpreter._respond_and_store

    def work():
        assert vision._cloud_vision(b"fixture", "Describe")[0]
        yield from original()

    interpreter._respond_and_store = work

    async def scenario():
        await interpreter.server.app.router.startup()
        await admit_local(interpreter, "hud")
        interpreter.complete("Fixture vision response.")
        await drain(interpreter)
        assert seen == ["modal", "gemini"]
        history = shadow.snapshot()["duty"]["last_success"]
        assert history["vision"]["route"]["provider"] == "gemini"
        assert history["brain"]["route"]["provider"] == "modal"
        # Same process path with no admitted request: no duty or new history.
        pools["vision_modal"].key = None
        monkeypatch.setattr(vision.requests, "post", lambda *args, **kwargs: answer())
        assert vision._cloud_vision(b"fixture", "Unowned")[0]
        assert shadow.snapshot()["duty"]["last_success"] == history

    asyncio.run(scenario())


def test_partial_scan_key_rotation_removal_and_reappearance(monkeypatch, tmp_path):
    pools = configure(monkeypatch, tmp_path)
    integration = ii.from_environment(br, vision)
    original = integration.shadow.snapshot()
    resolver = br.resolve
    routes = resolver("")
    monkeypatch.setattr(br, "resolve", lambda text: list(reversed(routes)))
    pools["brain_modal"].key = ""
    pools["vision_modal"].key = "rotated-key"
    assert integration.refresh(manifest(), br, vision)
    assert integration.shadow.snapshot() == original
    monkeypatch.setattr(br, "resolve", lambda text: (_ for _ in ()).throw(RuntimeError()))
    assert not integration.refresh(manifest(), br, vision)
    assert not integration.refresh({**manifest(), "complete": False}, br, vision)
    assert integration.shadow.snapshot() == original
    monkeypatch.setattr(br, "resolve", resolver)
    monkeypatch.setattr(br, "MODAL_BASE", None)
    assert not integration.refresh(manifest(), br, vision)
    assert integration.shadow.snapshot() == original
    assert integration.refresh(manifest(modal=False), br, vision)
    archived = integration.shadow.snapshot()
    primary = next(c for c in original["roster"]["characters"]
                   if any(b["provider"] == "modal" for b in c["bindings"]))
    assert next(c for c in archived["roster"]["characters"] if c["id"] == primary["id"])["status"] == "archived"
    monkeypatch.setattr(br, "MODAL_BASE", MODAL_BASE)
    assert integration.refresh(manifest(), br, vision)
    returned = integration.shadow.snapshot()
    assert next(c for c in returned["roster"]["characters"] if c["id"] == primary["id"]) == primary
    assert MODAL_BASE.encode() not in (tmp_path / "catalog.db").read_bytes()
    assert b"rotated-key" not in (tmp_path / "catalog.db").read_bytes()


def test_gate_off_never_touches_paths_and_partial_start_leaves_store(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path)
    assert ii.from_environment(br, vision, {"FRIDAY_IDENTITY_INTEGRATION": "0"}) is None
    assert not list(tmp_path.glob("*.db"))
    active = ii.from_environment(br, vision)
    original = active.shadow.snapshot()
    monkeypatch.setattr(br, "MODAL_BASE", None)
    assert ii.from_environment(br, vision) is None
    assert active.shadow.snapshot() == original


def test_restart_after_real_light_success_and_observer_faults(wired, monkeypatch):
    interpreter, integration, _ = wired
    state = interpreter.server_state
    shadow = integration.shadow
    select = integration.select

    def broken(*args):
        raise OSError("synthetic observer failure")

    async def scenario():
        await interpreter.server.app.router.startup()
        monkeypatch.setattr(integration, "select", broken)
        await admit_local(interpreter, "hud")
        interpreter.complete("Still answered.")
        await drain(interpreter)
        assert state["turn_ownership"] is server.LOCAL_OWNERSHIP
        assert not shadow.snapshot()["duty"]["last_success"]
        monkeypatch.setattr(integration, "select", select)
        assert (await asyncio.to_thread(interpreter.submit_text, "next request"))["accepted"]
        interpreter.complete("Recorded reply.")
        await drain(interpreter)
        restarted = ii.from_environment(br, vision)
        current = restarted.shadow.snapshot()["duty"]
        assert not current["current"]
        assert current["last_success"]["brain"]["route"]["provider"] == "modal"

    asyncio.run(scenario())


def test_bare_completion_does_not_claim_brain_success(wired):
    interpreter, integration, _ = wired

    async def scenario():
        await interpreter.server.app.router.startup()
        await admit_local(interpreter, "hud")
        interpreter.frames = [{"role": "assistant", "type": "message", "end": True}]
        interpreter.producer()
        interpreter.respond_thread = None
        await drain(interpreter)
        assert integration.shadow.snapshot()["duty"] == {"current": {}, "last_success": {}}

    asyncio.run(scenario())


def test_stopped_vision_transport_cannot_publish_late_success(wired, monkeypatch):
    interpreter, integration, _ = wired
    shadow = integration.shadow
    requests = []

    def fake_post(url, **kwargs):
        active = shadow.snapshot()["duty"]["current"]
        assert set(active) == {"brain", "vision"}
        requests.append(active["vision"]["request_id"])
        if len(requests) == 1:
            assert interpreter.cancel_owned_turn("fixture Stop")[0]
            assert not shadow.snapshot()["duty"]["current"]
        return answer()

    monkeypatch.setattr(vision.requests, "post", fake_post)
    original = interpreter._respond_and_store

    def work():
        assert vision._cloud_vision(b"fixture", "Describe")[0]
        yield from original()

    interpreter._respond_and_store = work

    async def scenario():
        await interpreter.server.app.router.startup()
        await admit_local(interpreter, "hud")
        interpreter.complete("This result belongs to the stopped request.")
        await drain(interpreter)
        assert shadow.snapshot()["duty"] == {"current": {}, "last_success": {}}
        assert (await asyncio.to_thread(interpreter.submit_text, "next request"))["accepted"]
        interpreter.complete("This result belongs to the next request.")
        await drain(interpreter)
        assert len(requests) == 2 and requests[0] != requests[1]
        history = shadow.snapshot()["duty"]["last_success"]
        assert {cap: result["request_id"] for cap, result in history.items()} == {
            "brain": requests[1], "vision": requests[1],
        }

    asyncio.run(scenario())


def test_shadow_disabled_binding_does_not_change_real_routing(wired):
    interpreter, integration, _ = wired
    inventory = manifest()
    inventory["deployments"]["brain_modal"]["enabled"] = False
    assert integration.refresh(inventory, br, vision)

    async def scenario():
        await interpreter.server.app.router.startup()
        await admit_local(interpreter, "hud")
        assert interpreter.server_state["brain_router"].current_pool == "brain_modal"
        assert not integration.shadow.snapshot()["duty"]["current"]
        interpreter.complete("The configured router still answered.")
        await drain(interpreter)
        assert not integration.shadow.snapshot()["duty"]["last_success"]

    asyncio.run(scenario())
