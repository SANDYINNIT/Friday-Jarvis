"""Actual HUD read boundary with isolated configuration and history."""

import json
import time
from types import SimpleNamespace

import pytest

from source.server import api_pools, brain_router, lab_authority, n8n_runtime, screen_understanding
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world.identity_shadow import DutyShadow
from tools.field_notes_acceptance.routing_data import fixture_inventory


def bridge_for(state):
    return WebviewBridge(StatusBus(), SimpleNamespace(server_state=state))


def forbidden(*_args, **_kwargs):
    pytest.fail("read tried to initialize a provider or change runtime state")


@pytest.mark.parametrize("modal", [False, True])
def test_hud_uses_resolver_order_without_initializing_providers(monkeypatch, modal):
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "0")
    monkeypatch.setattr(brain_router, "MODAL_BASE", "https://private-fixture.invalid/v1" if modal else None)
    monkeypatch.setattr(api_pools, "_pools", None)
    monkeypatch.setattr(api_pools, "_ensure_pools", forbidden)
    monkeypatch.setattr(n8n_runtime, "get_adapter", forbidden)
    monkeypatch.setattr(n8n_runtime, "_ADAPTER", None)
    state = {"brain_router": brain_router.BrainRouter(),
             "identity_integration": SimpleNamespace(read_routing=forbidden)}
    result = bridge_for(state).get_routing_view()
    ordered = [item for item in result["routes"] if item["position"]]
    assert [item["position"] for item in ordered] == list(range(1, len(ordered)+1))
    assert [item["model"] for item in ordered] == [model or brain_router.LOCAL_MODEL
                                                  for _, model, _ in brain_router.resolve("")]
    assert any(item["provider"] == "modal" for item in ordered) is modal
    assert "private-fixture" not in json.dumps(result)
    assert result["capabilities"]["brain"]["current"] is None


def test_cache_projection_does_not_allocate_credentials_or_conflate_cooldown_with_health(monkeypatch):
    now = time.monotonic()
    pool = api_pools.Pool("brain_groq", ["fixture-secret"])
    pool._banned_until[0] = now + 30
    monkeypatch.setattr(api_pools, "_pools", {"brain_groq": pool})
    monkeypatch.setattr(api_pools, "_model_bans", {("brain_modal", "test/model"): now + 30})
    monkeypatch.setattr(api_pools, "_ensure_pools", forbidden)
    assert api_pools.cached_route_state("brain_groq", "test/model") == ("loaded", "pool_cooldown")
    assert api_pools.cached_route_state("brain_modal", "test/model") == ("unknown", "model_cooldown")
    with pool._lock:
        assert api_pools.cached_route_state("brain_groq", "test/model") == ("unknown", None)
    assert pool._cursor == 0 and not pool._last_used


def test_cached_policy_requires_a_fresh_exact_legacy_binding(monkeypatch):
    authority = SimpleNamespace(_cast_at=time.time(), _cast_cache=[
        {"pool": "brain_groq", "model": "fixture-a", "char_id": "a"},
        {"pool": "brain_groq", "model": "fixture-b", "char_id": "b"},
    ], _eligibility_cache={"a": {"eligible": False}}, _refresh_cast=forbidden)
    monkeypatch.setattr(lab_authority, "_shared", authority)
    assert lab_authority.cached_route_eligibility("brain_groq", "fixture-a") is False
    assert lab_authority.cached_route_eligibility("brain_groq", "fixture-b") is True
    assert lab_authority.cached_route_eligibility("brain_groq", "fixture") is None
    authority._cast_at -= 90
    assert lab_authority.cached_route_eligibility("brain_groq", "fixture-a") is None


def test_exact_bindings_split_duty_completion_restart_and_unbound(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "1")
    monkeypatch.setattr(brain_router, "MODAL_BASE", "https://private-fixture.invalid/v1")
    monkeypatch.setattr(api_pools, "_ensure_pools", forbidden)
    runtime = SimpleNamespace(server_state={})
    fixture = fixture_inventory(tmp_path, runtime)
    integration, shadow = fixture.integration, fixture.integration.shadow
    bridge = bridge_for(runtime.server_state)
    # Exact resolver outcomes remain visible even after catalog enrollment.
    runtime.server_state['brain_router'].route_observations = lambda: [
        {'pool':fixture.first[0],'model':fixture.first[1],
         'success_at':None,'failure_at':1700000001.}]
    request = shadow.begin_request("admitted")
    first = integration.select(request, "brain", 1, *fixture.first)
    vision = integration.select(request, "vision", 1, "vision_modal",
                                screen_understanding._modal_vision_model(), fixture.first[2])
    fallback = integration.select(request, "brain", 2, *fixture.fallback)
    assert not shadow.finish(first, "success")
    payload = bridge.get_routing_view()
    exact = [r for r in payload['routes'] if r.get('capability')=='brain'
             and r.get('deployment')=='studio-main']
    assert exact and exact[0]['failure_at']==1700000001.
    current = payload["capabilities"]
    assert current["brain"]["current"]["deployment"] == "studio-fallback"
    assert current["vision"]["current"]["deployment"] == "studio-main"
    assert current["brain"]["last_success"] is None
    same_models = [row for row in payload["routes"] if row["capability"] == "brain"
                   and row.get("model") == fixture.first[1]]
    assert {row["deployment"] for row in same_models} == {"studio-main", "studio-secondary"}
    assert len({row["character_id"] for row in same_models}) == 2
    assert any(row.get("catalog_status") == "disabled" for row in payload["routes"])
    assert any(row.get("catalog_status") == "retired" for row in payload["routes"])
    assert shadow.finish(fallback, "success") and shadow.finish(vision, "success")
    owner = shadow.begin_request("unknown")
    shadow.observe_selected(owner, "brain", 1, fixture.unbound)
    fresh = bridge.get_routing_view()
    assert fresh["capabilities"]["brain"]["current"]["resolution"] == "unbound"
    assert any(row.get("serving") and row["kind"] == "observation"
               and not row.get("character_id") for row in fresh["routes"])
    integration.shadow = DutyShadow(fixture.catalog.path, fixture.history, enabled=True)
    after = bridge.get_routing_view()
    assert after["capabilities"]["brain"]["current"] is None
    assert after["capabilities"]["brain"]["last_success"]["deployment"] == "studio-fallback"


def test_selection_is_not_success_and_unsafe_configuration_is_not_exported(monkeypatch):
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "0")
    monkeypatch.setattr(brain_router, "MODAL_MODEL", '<img onerror="private">')
    monkeypatch.setattr(brain_router, "MODAL_BASE", "https://private-fixture.invalid/v1")
    monkeypatch.setattr(api_pools, "_ensure_pools", forbidden)
    router = brain_router.BrainRouter(enabled=False)
    runtime = SimpleNamespace(messages=[], llm=SimpleNamespace())
    bridge = bridge_for({"brain_router": router})
    router.apply_for("", runtime)
    data = bridge.get_routing_view()
    assert "private" not in json.dumps(data)
    assert data["capabilities"]["brain"]["last_success"] is None
    router.mark_success()
    assert bridge.get_routing_view()["capabilities"]["brain"]["last_success"]["model"] == brain_router.LOCAL_MODEL
    router.apply_for("", runtime)
    router.mark_failure("PRIVATE response")
    after = bridge.get_routing_view()
    assert "PRIVATE" not in json.dumps(after)
    assert after["capabilities"]["brain"]["last_success"]["model"] == brain_router.LOCAL_MODEL
    assert next(row for row in after["routes"] if row["provider"] == "ollama" and row["capability"] == "brain")["failure_at"]


def test_catalog_failure_keeps_runtime_sources_and_does_not_disclose_error(monkeypatch):
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "1")
    def fail(_candidates):
        raise ValueError("private credential fixture")
    state = {"brain_router": brain_router.BrainRouter(),
             "identity_integration": SimpleNamespace(read_routing=fail)}
    payload = bridge_for(state).get_routing_view()
    assert payload["identity"] == "error" and payload["brain_order_known"]
    assert not payload["capabilities"]["vision"]["duty_observable"]
    assert "credential" not in json.dumps(payload)
