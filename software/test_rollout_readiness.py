"""Exercise the real bridge and exact matcher against synthetic rollout evidence."""
import json
import time
from types import SimpleNamespace

import pytest

from source.server import api_pools, brain_router, lab_state, n8n_runtime
from source.server.identity_integration import IdentityIntegration
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world.identity_catalog import AliasClaim, IdentityCatalog, Route
from source.server.world.identity_shadow import DutyShadow
from tools.field_notes_acceptance.routing_data import fixture_inventory


def forbidden(*_args, **_kwargs):
    pytest.fail("passive readiness attempted initialization or dispatch")


def report(state):
    return WebviewBridge(StatusBus(), SimpleNamespace(server_state=state)).get_rollout_readiness()


@pytest.fixture(autouse=True)
def passive(monkeypatch):
    monkeypatch.setenv("FRIDAY_IDENTITY_INTEGRATION", "0")
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "0")
    monkeypatch.setattr(brain_router, "MODAL_BASE", "https://private-fixture.invalid/v1")
    monkeypatch.setattr(api_pools, "_pools", None)
    monkeypatch.setattr(api_pools, "_ensure_pools", forbidden)
    monkeypatch.setattr(n8n_runtime, "_ADAPTER", None)
    monkeypatch.setattr(n8n_runtime, "get_adapter", forbidden)
    monkeypatch.setattr(lab_state, "_world", None)
    monkeypatch.setattr(lab_state, "_background_life", None)
    monkeypatch.setattr(lab_state, "_load_world", forbidden)
    monkeypatch.setattr(lab_state, "ensure_started", forbidden)


def test_gates_off_and_missing_runtime_attachment_do_not_open_stores(monkeypatch, tmp_path):
    for key in ("CATALOG", "HISTORY"):
        monkeypatch.setenv(f"FRIDAY_IDENTITY_{key}_DB", str(tmp_path / f"{key}.db"))
    state = {"brain_router": brain_router.BrainRouter(),
             "identity_integration": SimpleNamespace(read_routing=forbidden)}
    initial = report(state)
    assert initial["identity"] == {"integration_gate": "disabled", "reader_gate": "disabled",
                                  "state": "disabled", "revision": None, "unresolved_aliases": None}
    assert initial["life"]["state"] == "disabled"
    assert initial["runtime"]["normal_readiness"] == "unverified"
    assert initial["mcp"]["state"] == "unavailable"
    monkeypatch.setenv("FRIDAY_IDENTITY_INTEGRATION", "1")
    state.pop("identity_integration")
    absent = report(state)
    assert absent["identity"]["state"] == "unavailable"
    assert {row["enrollment"] for row in absent["routes"] if row["capability"] == "brain"} == {"unknown"}
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def enrolled(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_IDENTITY_INTEGRATION", "1")
    runtime = SimpleNamespace(server_state={})
    fixture = fixture_inventory(tmp_path, runtime)
    return runtime.server_state, fixture


def test_empty_attached_catalog_is_distinct_from_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_IDENTITY_INTEGRATION", "1")
    catalog = IdentityCatalog(tmp_path / "catalog.db")
    shadow = DutyShadow(catalog.path, tmp_path / "history.db", enabled=True)
    state = {"brain_router": brain_router.BrainRouter(),
             "identity_integration": IdentityIntegration(catalog, shadow, {})}
    result = report(state)
    assert result["identity"]["state"] == "empty" and result["identity"]["unresolved_aliases"] == 0
    assert {row["enrollment"] for row in result["routes"] if row["capability"] == "brain"} == {"unmatched"}


def test_report_distinguishes_exact_permission_from_unmatched_and_retired(enrolled, monkeypatch):
    state, fixture = enrolled
    initial = report(state)
    assert initial["identity"]["reader_gate"] == "disabled"
    assert initial["identity"]["revision"]
    first = next(row for row in initial["routes"] if row["position"] == 1)
    assert first["enrollment"] == "verified" and first["saved_serving"] is None
    revision = initial["identity"]["revision"]
    character = next(char for char in fixture.integration.shadow.snapshot()["roster"]["characters"]
                     if any(binding["id"] == first["binding_id"] for binding in char["bindings"]))
    fixture.integration.set_serving(character["id"], first["binding_id"], "brain", False, revision)
    updated = report(state)
    first = next(row for row in updated["routes"] if row["position"] == 1)
    assert first["saved_serving"] is False and first["eligible"] is False
    assert fixture.integration.route_disabled("brain", *fixture.first)
    assert updated["legacy_unmatched_routing"] == "possible"
    assert updated["live_route_eligibility"] == "unverified"
    assert any(row["enrollment"] == "retired" and row["kind"] == "catalog" for row in updated["routes"])
    monkeypatch.setattr(brain_router, "MODAL_MODEL", "fixture-model-not-enrolled")
    stale_manifest = next(row for row in report(state)["routes"] if row["position"] == 1)
    assert stale_manifest["enrollment"] == "unmatched" and stale_manifest["binding_id"] is None
    assert not fixture.integration.route_disabled(
        "brain", fixture.first[0], "fixture-model-not-enrolled", fixture.first[2])


def test_alias_conflict_stays_ambiguous_without_exposing_claims(enrolled):
    state, fixture = enrolled
    shadow = fixture.integration.shadow.snapshot()
    alternative = next(row for row in shadow["roster"]["characters"] if any(
        b["deployment"] == fixture.alternate.deployment for b in row["bindings"]))
    primary = Route("modal", "studio-main", fixture.first[1])
    fixture.catalog.reconcile([{**primary.public(), "kind": "reasoning", "capabilities": ["brain"]}],
                              complete=True, aliases=(AliasClaim(primary, alternative["id"],
                                                                  "private-claim-id", True),))
    snapshot = report(state)
    assert snapshot["identity"]["unresolved_aliases"] == 1
    assert next(row for row in snapshot["routes"] if row["position"] == 1)["enrollment"] == "ambiguous"
    assert "private-claim-id" not in json.dumps(snapshot)


def test_loaded_links_and_saved_life_do_not_establish_scheduler_dispatch(enrolled, monkeypatch):
    state, fixture = enrolled
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "1")
    catalog = fixture.integration.shadow.snapshot()
    character, binding = next((char, binding) for char in catalog["roster"]["characters"]
                              for binding in char["bindings"]
                              if binding["deployment"] == "studio-main" and binding["capability"] == "brain")
    cid = character["id"]
    fixture.integration.set_background_life(cid, binding["id"], "ai", catalog["catalog_revision"], {cid})
    link = {"namespace": "town", "resident_id": "private-resident", "character_id": cid,
            "decision_id": "private-link", "verified": True}
    world = SimpleNamespace(characters=[SimpleNamespace(id="private-resident")], identity_links=[link],
                            paused=False)
    controller = SimpleNamespace(identity=fixture.integration, enabled=False, budget_observed_at=time.time(),
                                 budget=forbidden, foreground=forbidden, cycle=forbidden)
    monkeypatch.setattr(lab_state, "_world", world)
    monkeypatch.setattr(lab_state, "_background_life", controller)
    monkeypatch.setattr(lab_state, "_thread", SimpleNamespace(is_alive=lambda: True))
    result = report(state)
    assert result["town"]["verified_links"] == 1
    assert result["life"]["state"] == "paused" and result["life"]["budget"] == "disabled"
    assert result["life"]["saved_ai"] == result["life"]["linked_exact_candidates"] == 1
    assert "private-" not in json.dumps(result)
    controller.enabled = True
    assert report(state)["life"]["state"] == "awaiting-scheduler-checks"
    controller.budget_observed_at -= 31
    assert report(state)["life"]["budget"] == "stale"
    assert report(state)["life"]["state"] == "unconfirmed"
    controller.budget_observed_at = None
    assert report(state)["life"]["budget"] == "unavailable"


def test_stale_cache_and_unsafe_route_metadata_are_not_live_evidence(monkeypatch):
    monkeypatch.setattr(brain_router, "MODAL_MODEL", "sk-private-key")
    monkeypatch.setattr(brain_router, "GROQ_STRONG_MODEL", "D:/private/model.txt")
    runtime = SimpleNamespace(
        cached_inventory=lambda: [{"server": "https://private-endpoint.invalid", "enabled": True,
                                  "session_attached": False, "status": "disconnected",
                                  "discovered_at": time.time(), "tools": [{"secret": "private-token"}]}],
        start=forbidden, invoke=forbidden,
    )
    router = brain_router.BrainRouter()
    monkeypatch.setattr(router, "route_observations", lambda: [
        {"pool": "brain_modal", "model": "sk-private-key", "success_at": time.time() - 90000}])
    result = report({"brain_router": router, "mcp_runtime": runtime})
    assert result["mcp"]["state"] == "configured"
    assert result["mcp"]["servers"] == [{"label": None, "enabled": True,
        "observed_connection": "disconnected", "session_attached": False,
        "cache": "stale", "cached_tool_count": 1}]
    assert result["serving_history"] == {"brain": "stale", "vision": "unavailable"}
    assert "private" not in json.dumps(result)


def test_read_failures_remain_unavailable_and_do_not_expose_error(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("D:/private/config.json")
    monkeypatch.setenv("FRIDAY_IDENTITY_INTEGRATION", "1")
    state = {"brain_router": brain_router.BrainRouter(),
             "identity_integration": SimpleNamespace(read_routing=unavailable),
             "mcp_runtime": SimpleNamespace(cached_inventory=unavailable)}
    result = report(state)
    assert result["identity"]["state"] == result["mcp"]["state"] == "error"
    assert result["route_inventory"] == "available"
    assert not any(row["enrollment"] == "unmatched" for row in result["routes"])
    assert "private" not in json.dumps(result)
