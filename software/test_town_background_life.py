"""Isolated production scheduler, catalog, budget, simulator and HUD paths."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from source.server import lab_budget, lab_state as lab
from source.server.identity_integration import IdentityIntegration
from source.server.status_ui import WebviewBridge
from source.server.world import Store, sim
from source.server.world.background_life import RouteUnavailable, installed_links, provider_plan
from source.server.world.identity_catalog import IdentityCatalog, Route
from source.server.world.identity_reader import IdentityReader, ResidentLink
from source.server.world.identity_shadow import DutyShadow


@pytest.fixture
def life(tmp_path, monkeypatch):
    catalog = IdentityCatalog(tmp_path / "catalog.db")
    routes = [Route("modal", "primary", "model-a"), Route("groq", "second", "model-b"),
              Route("ollama", "workshop", "ollama_chat/model-c")]
    inventory = [{**route.public(), "capabilities": ["brain"], "kind": "reasoning"} for route in routes]
    catalog.reconcile(inventory, complete=True)
    shadow = DutyShadow(catalog.path, tmp_path / "duty.db", enabled=True)
    integration = IdentityIntegration(catalog, shadow, {
        ("brain", pool): ("http://fixture.invalid/v1", route.provider, route.deployment, {route.model})
        for pool, route in zip(("brain_modal", "brain_groq", "local"), routes)})
    store = Store(tmp_path / "world.db")
    world = sim.new_world()
    assert store.save(world)["ok"]
    monkeypatch.setattr(lab, "_world", world)
    monkeypatch.setattr(lab, "_store", store)
    monkeypatch.setattr(lab, "_load_world", lambda: (lab._world, None))
    monkeypatch.setattr(lab, "_background_life", None)
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "1")
    budget = lab_budget.configure(tmp_path / "world.db")
    busy = [False]
    lab.configure_background_life(integration, lambda: busy[0])
    controller = lab.background_life()
    controller.interval = 2
    controller.global_interval = 0
    bridge = WebviewBridge(None, SimpleNamespace(server_state={"identity_integration": integration}))
    reader = IdentityReader(shadow, lab.capture_identity_world, enabled=True)
    view = controller.catalog_view()
    bindings = {row["deployment"]: row for row in view["bindings"].values()}
    cid = {key: row["character_id"] for key, row in bindings.items()}
    link = ResidentLink("town", "friday", cid["primary"], "fixture-decision", True)
    proposal = reader.snapshot(links=[link])["installation_proposal"]
    assert lab.install_catalog_residents(catalog, reader, proposal, links=[link])["ok"]
    for resident in installed_links(lab._world, view).values():
        resident.needs["energy"] = 45
    assert store.save(lab._world)["ok"]
    calls = []

    def planner(route, prompt, timeout, allowed):
        assert not lab._lock._is_owned()
        assert allowed() and timeout <= controller.timeout
        assert "memory" not in prompt
        calls.append(route)
        return '{"action":"rest"}'

    controller.planner = planner
    try:
        yield SimpleNamespace(catalog=catalog, store=store, inventory=inventory, controller=controller,
                              bridge=bridge, budget=budget, busy=busy, calls=calls, ids=cid, bindings=bindings)
    finally:
        controller.invalidate()
        if controller.job is not None:
            controller.job.future.result(timeout=5)
        store.close()
        lab_budget.reset_shared()


def preference(data, name="primary", mode="ai"):
    row = data.bindings[name]
    return data.bridge.set_catalog_background_life(
        row["character_id"], row["id"] if mode == "ai" else None, mode,
        data.controller.catalog_view()["revision"])


def pulse(data):
    lab._service_background_life()
    job = data.controller.job
    assert job is not None
    result = job.future.result(timeout=4)
    lab._service_background_life()
    return job, result


def test_npc_zero_calls_then_exact_ai_routine_and_reopen(life):
    assert life.bridge.set_catalog_life_budget(True)["ok"]
    resident = lab._world.characters[0]
    memory = list(resident.memory)
    for _ in range(3):
        lab._advance_world(lab._world)
        lab._service_background_life()
    assert life.calls == [] and lab._world.planner_calls == 0 and resident.memory == memory
    assert preference(life)["ok"]
    job, result = pulse(life)
    assert job.character == life.ids["primary"] and result[0] == "rest"
    assert life.calls[0][2:] == ("model-a", "modal")
    assert resident.goal == "rest" and resident.state == "idle"
    previous = resident.needs["energy"]
    for _ in range(80):
        old = (resident.x, resident.y)
        lab._advance_world(lab._world)
        assert abs(old[0]-resident.x) + abs(old[1]-resident.y) <= 1
        assert len({(c.x,c.y) for c in lab._world.characters}) == len(lab._world.characters)
        if any(e["kind"] == "completed" and e["actor"] == resident.id and "rest" in e["text"]
               for e in lab._world.events):
            break
    else:
        pytest.fail("validated AI routine did not complete")
    assert resident.needs["energy"] < previous and resident.memory == memory
    events = life.store.events(limit=150)
    assert len([row for row in events if row["kind"] == "life_plan"]) == 1
    assert lab._reload_world().planner_calls == 1
    assert lab._world.characters[0].memory == memory
    assert life.controller.catalog_view()["characters"][life.ids["primary"]]["life_binding_id"] == job.binding


def test_bulk_local_individual_and_independent_serving_survive_reconcile(life):
    revision = life.controller.catalog_view()["revision"]
    assert life.bridge.set_catalog_background_life(None, None, "ai", revision)["changed"] == 2
    latest = life.controller.catalog_view()
    local = latest["characters"][life.ids["workshop"]]
    assert not local.get("life_binding_id")
    assert not life.bridge.set_catalog_background_life(None, None, "ai", revision)["ok"]
    assert preference(life, "workshop")["ok"]
    binding = life.bindings["primary"]
    assert life.bridge.set_catalog_serving(binding["character_id"], binding["id"], "brain", False,
                                         life.controller.catalog_view()["revision"])["ok"]
    life.catalog.reconcile(list(reversed(life.inventory)), complete=True)
    view = life.controller.catalog_view()
    assert view["bindings"][binding["id"]]["can_serve"] is False
    assert view["characters"][binding["character_id"]]["life_binding_id"] == binding["id"]
    assert view["characters"][life.ids["workshop"]]["life_binding_id"] == life.bindings["workshop"]["id"]
    life.catalog.reconcile(life.inventory[1:], complete=True)
    assert life.controller.catalog_view()["characters"][binding["character_id"]]["life_binding_id"] == binding["id"]
    life.catalog.reconcile(life.inventory, complete=True)
    assert life.controller.catalog_view()["characters"][binding["character_id"]]["life_binding_id"] == binding["id"]
    assert not life.calls


def test_fair_single_slot_foreground_and_durable_budget(life):
    assert life.bridge.set_catalog_life_budget(True)["ok"]
    assert life.bridge.set_catalog_background_life(None, None, "ai",
               life.controller.catalog_view()["revision"])["changed"] == 2
    life.controller.daily_total = 2
    life.controller.daily_character = 1
    life.busy[0] = True
    lab._service_background_life()
    assert not life.calls and life.controller.job is None
    life.busy[0] = False
    first, _ = pulse(life)
    second, _ = pulse(life)
    assert first.character != second.character
    assert len(life.calls) == 2
    for char in lab._world.characters:
        char.goal = "work"
    lab._world.tick += 3
    third, result = pulse(life)
    assert result[2] == "budget_exhausted" and not result[1]
    assert len(life.calls) == 2 and life.budget.used("life")["attempts"] == 2
    assert life.controller.reasons[third.character][0] == "budget_exhausted"


def test_pending_budget_reservations_are_atomic_across_connections(life):
    import sqlite3

    assert life.bridge.set_catalog_life_budget(True)["ok"]
    with sqlite3.connect(life.store.path, check_same_thread=False) as conn:
        other = lab_budget.LabBudget(conn)
        def reserve(index):
            budget = (life.budget, other)[index % 2]
            return budget.reserve_town(str(index), "same-character", "modal", "model",
                                       daily_total=2, daily_character=1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(reserve, range(8)))
    assert outcomes.count("reserved") == 1 and outcomes.count("budget_exhausted") == 7
    assert life.budget.used("life")["outcomes"]["pending"] == 1


@pytest.mark.parametrize("change", ["npc", "remove", "load", "foreground", "revision", "endpoint"])
def test_late_result_cannot_mutate_replaced_or_opted_out_world(life, change):
    assert preference(life)["ok"] and life.bridge.set_catalog_life_budget(True)["ok"]
    entered, release = Event(), Event()
    def blocked(*_):
        entered.set()
        assert release.wait(4)
        return '{"action":"rest"}'
    life.controller.planner = blocked
    lab._service_background_life()
    job = life.controller.job
    try:
        assert entered.wait(3)
        old_tick = lab._world.tick
        lab._advance_world(lab._world)
        assert lab._world.tick == old_tick + 1
        lab._service_background_life()
        assert life.controller.job is job
        if change == "npc":
            assert preference(life, mode="npc")["ok"]
        elif change == "remove":
            lab._world.characters.remove(job.resident)
        elif change == "load":
            lab._reload_world()
        elif change == "foreground":
            lab.invalidate_background_life()  # the real admission hook
        elif change == "endpoint":
            matchers = life.controller.identity._matchers
            old = matchers[("brain", "brain_modal")]
            matchers[("brain", "brain_modal")] = ("http://changed.invalid/v1", *old[1:])
        else:
            life.catalog.reconcile(life.inventory[1:], complete=True)
    finally:
        release.set()
        job.future.result(timeout=4)
    lab._service_background_life()
    assert not any(event["kind"] == "life_plan" for event in lab._world.events)
    assert life.budget.used("life")["attempts"] == 1


@pytest.mark.parametrize("result,reason", [
    ('{"action":"rest","x":99}', "Invalid plan"), (TimeoutError(), "timed out"),
    (RuntimeError("private endpoint"), "unavailable")])
def test_invalid_timeout_or_failure_falls_back_procedurally(life, result, reason):
    assert preference(life)["ok"] and life.bridge.set_catalog_life_budget(True)["ok"]
    def planner(*_):
        if isinstance(result, Exception):
            raise result
        return result
    life.controller.planner = planner
    job, observed = pulse(life)
    assert observed[0] is None and observed[1]
    assert reason.lower() in life.controller.reasons[job.character][1].lower()
    assert "private endpoint" not in str(life.controller.reasons)
    assert not any(event["kind"] == "life_plan" for event in lab._world.events)
    assert life.budget.used("life")["outcomes"]["failed"] == 1


@pytest.mark.parametrize("local", [False, True])
def test_provider_wire_has_exact_target_and_no_tools(monkeypatch, local):
    import json
    import httpx
    from source.server import api_pools

    route = ("local", "http://127.0.0.1:11434", "ollama_chat/model-c", "ollama") if local else (
        "brain_modal", "https://fixture.invalid/v1", "model-a", "modal")
    monkeypatch.setattr(api_pools, "pool", lambda _: SimpleNamespace(background_key=lambda: "fixture"))
    monkeypatch.setattr(api_pools, "model_banned", lambda *_: False)
    calls = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            message = {"content": '{"action":"rest"}'}
            yield json.dumps({"message": message} if local else {"choices": [{"message": message}]}).encode()

    def transport(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, stream=Body())

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(transport)))
    assert provider_plan(route, '{"room":"hall"}', 2, lambda: True) == '{"action":"rest"}'
    [(path, payload)] = calls
    assert path == ("/api/chat" if local else "/v1/chat/completions")
    assert payload["model"] == ("model-c" if local else "model-a")
    assert "tools" not in payload and payload["stream"] is False


def test_background_key_does_not_move_foreground_cursor_or_pacing(monkeypatch):
    from source.server import api_pools

    monkeypatch.setattr(api_pools.time, "monotonic", lambda: 100)
    pool = api_pools.Pool("fixture", ["a", "b"], min_interval=4)
    assert pool.next_key() == "a"
    assert pool.background_key() == "b"
    assert pool._cursor == 1 and pool._last_used == {0: 100}
    assert pool.next_key() == "b"
    assert pool.background_key() is None


def test_unavailable_before_dispatch_is_not_reported_as_a_model_call(life):
    assert preference(life)["ok"] and life.bridge.set_catalog_life_budget(True)["ok"]

    def unavailable(*_):
        raise RouteUnavailable()

    life.controller.planner = unavailable
    _, result = pulse(life)
    assert result[1] and not result[5]
    assert lab._world.planner_calls == 0
    usage = life.budget.used("life")
    assert usage["attempts"] == 1 and usage["model_calls"] == 0
