"""Synthetic route inventory and owned shadow observations for offline acceptance."""

import os
import time
from types import SimpleNamespace

from source.server import api_pools, brain_router, cloud_stt, gemini_tts, n8n_runtime, screen_understanding
from source.server.identity_integration import IdentityIntegration, discover_inventory
from source.server.world.identity_catalog import IdentityCatalog, Route
from source.server.world.identity_shadow import DutyShadow


def fixture_inventory(area, runtime):
    first, fallback = brain_router.resolve("")[:2]
    assert first[0] == "brain_modal" and fallback[0] == "brain_groq"
    manifest = {"complete": True, "deployments": {
        first[0]: {"base": first[2], "deployment": "studio-main"},
        fallback[0]: {"base": fallback[2], "deployment": "studio-fallback"},
        "vision_modal": {"base": first[2], "deployment": "studio-main"},
    }, "services": [{"provider": "n8n", "deployment": "workflow-local", "model": "workflows",
                    "kind": "service", "capabilities": ["workflow"]}]}
    rows, matchers = discover_inventory(manifest, brain_router, screen_understanding)
    alternate = Route("modal", "studio-secondary", first[1])
    disabled = Route("gemini", "disabled-lab", "fixture-disabled")
    retired = Route("openrouter", "retired-lab", "fixture-retired")
    unbound = Route("modal", "unmapped-lab", first[1])
    catalog = IdentityCatalog(area / "catalog.db")
    history = area / "duty.db"
    rows.extend([{**alternate.public(), "kind": "reasoning", "capabilities": ["brain"]},
                 {**disabled.public(), "kind": "reasoning", "capabilities": ["brain"], "enabled": False}])
    catalog.reconcile([*rows, {**retired.public(), "kind": "reasoning", "capabilities": ["brain"]}], complete=True)
    catalog.reconcile(rows, complete=True)
    integration = IdentityIntegration(catalog, DutyShadow(catalog.path, history, enabled=True), matchers)
    runtime.server_state.update(brain_router=brain_router.BrainRouter(), identity_integration=integration,
                                local_stt_model="base.en", tts_engine=object())
    return SimpleNamespace(catalog=catalog, history=history, integration=integration, alternate=alternate,
                           unbound=unbound, first=first, fallback=fallback)


class RoutingData:
    def __init__(self, area, runtime):
        self.runtime = runtime
        self.calls = []
        self.fixture = fixture_inventory(area, runtime)
        self.modal_base = brain_router.MODAL_BASE
        self.groq_model = brain_router.GROQ_STRONG_MODEL
        self.shadow = self.fixture.integration.shadow
        self.primary = self.fallback = self.vision = self.request = None
        os.environ["FRIDAY_IDENTITY_READER"] = "1"  # fixture process only
        api_pools._pools = {
            "brain_modal": api_pools.Pool("brain_modal", ["fixture-token-never-rendered"]),
            "brain_groq": api_pools.Pool("brain_groq", ["fixture-token-never-rendered"]),
            "brain_gemini": api_pools.Pool("brain_gemini", []),
        }
        api_pools._ensure_pools = self.forbidden
        n8n_runtime._ADAPTER = SimpleNamespace(config={"enabled": True}, status=self.forbidden)
        screen_understanding._modal_vision = self.forbidden
        cloud_stt.transcribe_wav = self.forbidden
        gemini_tts._synthesize = self.forbidden

    def forbidden(self, *_args, **_kwargs):
        self.calls.append("unexpected call")
        raise AssertionError("routing reader touched a provider or initialized pools")

    def control(self, command):
        action = command["action"].removeprefix("routing:")
        fixture = self.fixture
        if action == "modal":
            brain_router.MODAL_BASE = self.modal_base if command["value"] else None
        elif action == "gate":
            os.environ["FRIDAY_IDENTITY_READER"] = "1" if command["value"] else "0"
        elif action == "integration-gate":
            os.environ["FRIDAY_IDENTITY_INTEGRATION"] = "1" if command["value"] else "0"
        elif action == "start":
            self.request = self.shadow.begin_request("fixture-request")
            self.primary = fixture.integration.select(self.request, "brain", 1, *fixture.first)
            self.vision = fixture.integration.select(self.request, "vision", 1, "vision_modal",
                screen_understanding._modal_vision_model(), fixture.first[2])
        elif action == "fallback":
            self.fallback = fixture.integration.select(self.request, "brain", 2, *fixture.fallback)
            return {"late": self.shadow.finish(self.primary, "success")}
        elif action == "complete":
            return {"brain": self.shadow.finish(self.fallback, "success"),
                    "vision": self.shadow.finish(self.vision, "success")}
        elif action == "unbound":
            request = self.shadow.begin_request("unbound-request")
            self.shadow.observe_selected(request, "brain", 1, fixture.unbound)
        elif action == "restart":
            self.shadow = DutyShadow(fixture.catalog.path, fixture.history, enabled=True)
            fixture.integration.shadow = self.shadow
        elif action == "reader-error":
            self.runtime.routing_error = command["value"]
        elif action == "cooldown":
            api_pools._model_bans[(fixture.first[0], fixture.first[1])] = time.monotonic() + 60
        elif action == "unsafe":
            brain_router.GROQ_STRONG_MODEL = '<img onerror=window.routeMarkupRan=true>' if command["value"] else self.groq_model
        return {"calls": self.calls, "order": [model or brain_router.LOCAL_MODEL
                                               for _, model, _ in brain_router.resolve("")],
                "stores": [path.stat().st_mtime_ns for path in (fixture.catalog.path, fixture.history)]}
