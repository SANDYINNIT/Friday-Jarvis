"""Isolated catalog writes and real dispatch/vision routing with fake transports."""
import asyncio
from contextlib import closing
from copy import deepcopy
import sqlite3

from source.server import brain_router as br, identity_integration as ii, lab_state
from source.server import screen_understanding as vision
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world import sim
from source.server.world.identity_reader import ResidentLink
from test_identity_integration import MODAL_BASE, answer, manifest, wired
from test_request_reply_ownership import drain, isolated_server


def bindings(bridge):
    pair = bridge.get_identity_views()
    return pair["catalog_revision"], {
        (b["provider"], b["capability"], b["model"]): b
        for character in pair["roster"]["characters"] for b in character["bindings"]
    }


def set_serving(bridge, row, revision, allowed):
    return bridge.set_catalog_serving(row["character_id"], row["id"],
                                      row["capability"], allowed, revision)


def fixture_view(wired, monkeypatch):
    interpreter, integration, pools = wired
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "1")
    world = sim.new_world(seed=10)
    world.characters[0].memory = ["Original fixture memory"]
    monkeypatch.setattr(lab_state, "_world", world)
    bridge = WebviewBridge(StatusBus(), interpreter)
    revision, rows = bindings(bridge)
    modal = rows["modal", "brain", br.MODAL_MODEL]
    world.identity_links = [ResidentLink("town", world.characters[0].id,
                                        modal["character_id"], "fixture-verified", True).__dict__]
    return interpreter, integration, pools, bridge, world


def test_npc_serves_until_disabled_and_vision_stays_independent(wired, monkeypatch):
    interpreter, integration, _, bridge, world = fixture_view(wired, monkeypatch)
    revision, rows = bindings(bridge)
    brain = rows["modal", "brain", br.MODAL_MODEL]
    visual = rows["modal", "vision", br.MODAL_MODEL]
    assert brain["character_id"] == visual["character_id"]
    original_memories = deepcopy([c.memory for c in world.characters])
    posts = []
    monkeypatch.setattr(vision.requests, "post", lambda url, **kwargs: posts.append(url) or answer())
    state = interpreter.server_state

    async def scenario():
        await interpreter.server.app.router.startup()
        assert (await asyncio.to_thread(interpreter.submit_text, "fixture one"))["accepted"]
        assert state["brain_router"].current_pool == "brain_modal"
        before = integration.shadow.snapshot()["duty"]["current"]["brain"]
        result = set_serving(bridge, brain, revision, False)
        assert result["ok"]
        assert integration.shadow.snapshot()["duty"]["current"]["brain"] == before
        assert state["brain_router"].current_pool == "brain_modal"
        interpreter.complete("First answer.")
        await drain(interpreter)
        assert (await asyncio.to_thread(interpreter.submit_text, "fixture two"))["accepted"]
        assert state["brain_router"].current_pool == "brain_groq"
        assert vision._cloud_vision(b"fixture", "Describe")[2] == "vision_modal"
        interpreter.complete("Fallback answer.")
        await drain(interpreter)
        _, updated = bindings(bridge)
        assert updated["modal", "brain", br.MODAL_MODEL]["saved_serving"] is False
        assert updated["modal", "vision", br.MODAL_MODEL]["serving_allowed"] is True

    asyncio.run(scenario())
    assert len(posts) == 1 and posts[0].startswith(MODAL_BASE)
    for _ in range(24):
        sim.tick(world)
    assert world.planner_calls == 0 and len(posts) == 1
    assert [c.memory for c in world.characters] == original_memories
    town = bridge.get_town_view()
    resident = next(c for c in town["residents"] if c["character_id"] == brain["character_id"])
    assert resident["background_life"]["active_mode"] == "npc"
    assert {b["capability"]: b["serving_allowed"] for b in resident["bindings"]} == {
        "brain": False, "vision": True}


def test_modal_vision_retry_rechecks_preference_without_banning_or_switching_brain(wired, monkeypatch):
    interpreter, integration, _, bridge, _ = fixture_view(wired, monkeypatch)
    revision, rows = bindings(bridge)
    visual = rows["modal", "vision", br.MODAL_MODEL]
    attempts = []
    monkeypatch.setattr(vision, "MODAL_VISION_REASONING_EFFORT", "low")

    def post(url, **kwargs):
        attempts.append(url)
        if url.startswith(MODAL_BASE):
            assert set_serving(bridge, visual, revision, False)["ok"]
            return type("BadRequest", (), {"status_code": 400})()
        return answer()

    monkeypatch.setattr(vision.requests, "post", post)
    ok, _, provider, _ = vision._cloud_vision(b"fixture", "Describe")
    assert ok and provider == "vision_gemini/vision-model"
    assert len(attempts) == 2 and attempts[0].startswith(MODAL_BASE)
    assert not integration.route_disabled("brain", "brain_modal", br.MODAL_MODEL, MODAL_BASE)
    assert interpreter.server_state["brain_router"].apply_for("next request", interpreter)[0] == "brain_modal"


def test_write_fences_and_reopens_after_reorder_rotation_and_retirement(wired, monkeypatch):
    interpreter, integration, pools, bridge, _ = fixture_view(wired, monkeypatch)
    revision, rows = bindings(bridge)
    brain = rows["modal", "brain", br.MODAL_MODEL]
    other = rows["modal", "vision", br.MODAL_MODEL]
    assert not bridge.set_catalog_serving("incorrect", brain["id"], "brain", False, revision)["ok"]
    assert not bridge.set_catalog_serving(brain["character_id"], other["id"], "brain", False, revision)["ok"]
    assert not set_serving(bridge, brain, revision, "false")["ok"]
    with closing(sqlite3.connect(integration.catalog.path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM catalog_revision").fetchone()[0]
        conn.execute("""CREATE TRIGGER fixture_reject_head BEFORE UPDATE ON catalog_head
                        BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END""")
        assert not set_serving(bridge, brain, revision, False)["ok"]
        assert conn.execute("SELECT COUNT(*) FROM catalog_revision").fetchone()[0] == count
        conn.execute("DROP TRIGGER fixture_reject_head")
    assert bindings(bridge)[0] == revision
    saved = set_serving(bridge, brain, revision, False)
    assert saved["ok"] and saved["catalog_revision"] > revision
    assert not set_serving(bridge, brain, revision, True)["ok"]
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "0")
    assert not set_serving(bridge, brain, saved["catalog_revision"], True)["ok"]
    assert not bridge.get_identity_views()["available"]
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "1")
    pools["brain_modal"].key = "rotated-fixture-key"
    order = br.resolve("")
    monkeypatch.setattr(br, "resolve", lambda _: list(reversed(order)))
    assert integration.refresh(manifest(), br, vision)
    assert bindings(bridge)[0] == saved["catalog_revision"]
    assert integration.refresh(manifest(modal=False), br, vision)
    archived_revision, archived_rows = bindings(bridge)
    archived = archived_rows["modal", "brain", br.MODAL_MODEL]
    assert not archived["serving_editable"]
    assert not set_serving(bridge, archived, archived_revision, True)["ok"]
    assert integration.refresh(manifest(), br, vision)
    reopened = ii.from_environment(br, vision)
    assert reopened is not None
    interpreter.server_state["identity_integration"] = reopened
    _, reopened_rows = bindings(bridge)
    assert reopened_rows["modal", "brain", br.MODAL_MODEL]["id"] == brain["id"]
    assert reopened_rows["modal", "brain", br.MODAL_MODEL]["saved_serving"] is False
    assert reopened.route_disabled("brain", "brain_modal", br.MODAL_MODEL, MODAL_BASE)


def test_explicitly_disabled_local_is_not_an_emergency_bypass(wired, monkeypatch):
    interpreter, integration, _, bridge, _ = fixture_view(wired, monkeypatch)
    inventory = manifest()
    inventory["deployments"]["local"] = {"deployment": "fixture-local", "base": br.LOCAL_BASE}
    assert integration.refresh(inventory, br, vision)
    router = interpreter.server_state["brain_router"]
    router._enabled = False
    assert router.apply_for("offline", interpreter)[0] == "local"
    revision, rows = bindings(bridge)
    assert set_serving(bridge, rows["ollama", "brain", br.LOCAL_MODEL], revision, False)["ok"]
    producer_calls = []
    monkeypatch.setattr(interpreter, "_respond_and_store", lambda: producer_calls.append(True))

    async def scenario():
        await interpreter.server.app.router.startup()
        result = await asyncio.to_thread(interpreter.submit_text, "fixture offline request")
        assert result["status"] == "unavailable"
        assert not producer_calls and router.current is None
        await drain(interpreter)

    asyncio.run(scenario())


def test_brain_failover_skips_disabled_next_binding(wired, monkeypatch):
    interpreter, _, _, bridge, _ = fixture_view(wired, monkeypatch)
    revision, rows = bindings(bridge)
    router = interpreter.server_state["brain_router"]
    assert router.apply_for("fixture", interpreter)[0] == "brain_modal"
    assert set_serving(bridge, rows["groq", "brain", br.GROQ_FAST_MODEL], revision, False)["ok"]
    router.failover_to_next(interpreter, "fixture", reason="503 fixture")
    assert router.current_pool == "brain_groq"
    assert router.current_model == br.GROQ_STRONG_MODEL
