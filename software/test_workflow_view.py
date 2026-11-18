"""Real passive workflow bridge with isolated pending stores and fake HTTP."""

from copy import deepcopy
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from source.server import n8n_runtime
from source.server.status_ui import StatusBus, WebviewBridge
from tools.field_notes_acceptance.workflow_data import WorkflowData


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_TASK_DB", str(tmp_path / "unset.db"))
    monkeypatch.setattr(n8n_runtime, "_ADAPTER", None)
    runtime = SimpleNamespace(server_state={})
    data = WorkflowData(tmp_path, runtime)
    return data, WebviewBridge(StatusBus(), runtime)


def test_passive_reads_preserve_sources_permissions_and_files(fixture, monkeypatch):
    data, bridge = fixture
    before = data.snapshot()
    config = deepcopy(data.adapter.config)
    cache = deepcopy(data.adapter._cache)
    for method in ("status", "list_workflows", "get_adapter", "load_config"):
        monkeypatch.setattr(n8n_runtime if method in ("get_adapter","load_config") else data.adapter,
                            method, lambda *_args, **_kwargs: pytest.fail("active workflow discovery"))
    for _ in range(3):
        result = bridge.get_workflow_view()
    assert data.snapshot() == before and data.adapter.config == config and data.adapter._cache == cache
    assert result["sources"]["definitions"]["state"] == "partial"
    assert result["sources"]["definitions"]["connection"] == "disconnected"
    assert result["sources"]["definitions"]["connection_stale"]
    records = {row["id"]:row for row in result["rows"]}
    assert records["n8n-config:daily_briefing"]["name"] == records[f"n8n-pending:{data.first.id}"]["name"]
    assert records["n8n-config:daily_briefing"]["kind"] == "definition"
    assert records[f"n8n-pending:{data.first.id}"]["kind"] == "decision"
    assert records["n8n-config:daily_briefing"]["call"] is None
    assert f"n8n-pending:{data.other.id}" not in records
    assert "PRIVATE" not in json.dumps(result) and "private-fixture" not in json.dumps(result)
    assert "callback_url" not in json.dumps(result) and "webhook_url" not in json.dumps(result)


def test_exact_decision_navigation_never_resolves_or_calls(fixture):
    data, bridge = fixture
    before = data.snapshot()
    assert bridge.workflow_request_target(f"n8n-pending:{data.second.id}") == {
        "ok":True, "domain":"integration", "correlation_id":data.second.id}
    for reference in (data.first.id, f"workspace:{data.first.id}", "n8n-config:daily_briefing",
                      f"n8n-pending:{data.other.id}", "n8n-pending:pr-deadbeef", "n8n-pending:../file"):
        assert not bridge.workflow_request_target(reference)["ok"]
    assert data.snapshot() == before
    data.control({"action":"workflow:request-remove","id":data.second.id})
    assert not bridge.workflow_request_target(f"n8n-pending:{data.second.id}")["ok"]
    assert data.calls == [] and Path(data.store.path).read_bytes() == data.pending_before


@pytest.mark.parametrize("http_status,application",[(200,False),(202,True),(503,True)])
def test_call_receipt_cannot_establish_workflow_completion(fixture, http_status, application):
    data, bridge = fixture
    data.control({"action":"workflow:receipt","http_status":http_status,"application_ok":application})
    result = bridge.get_workflow_view()
    row = next(row for row in result["rows"] if row["id"] == "n8n-config:daily_briefing")
    assert row["call"]["http_status"] == http_status and row["call"]["at"] > 0
    assert row["call"]["application_ok"] is (application if http_status<400 else None)
    assert "completed" not in row and "DO_NOT_DISCLOSE" not in json.dumps(result)
    assert data.calls == []


def test_unloaded_empty_error_and_disabled_are_different(fixture):
    data, bridge = fixture
    data.control({"action":"workflow:enabled","value":False})
    assert not bridge.get_workflow_view()["sources"]["definitions"]["enabled"]
    data.control({"action":"workflow:empty"})
    result = bridge.get_workflow_view()
    assert result["sources"]["definitions"]["state"] == "ready"
    assert not any(row["kind"] == "definition" for row in result["rows"])
    data.control({"action":"workflow:unloaded"})
    assert bridge.get_workflow_view()["sources"]["definitions"]["state"] == "unavailable"
    data.control({"action":"workflow:restore"})
    data.control({"action":"workflow:requests-error"})
    result = bridge.get_workflow_view()
    assert result["sources"]["decisions"]["state"] == "error"
    assert "private error" not in json.dumps(result)
    data.adapter.config["workflows"] = []
    assert bridge.get_workflow_view()["sources"]["definitions"]["state"] == "error"


def test_missing_sources_are_not_initialized(monkeypatch):
    monkeypatch.setattr(n8n_runtime, "_ADAPTER", None)
    monkeypatch.setattr(n8n_runtime, "get_adapter", lambda *_args, **_kwargs:pytest.fail("adapter initialized"))
    from source.server import pending_requests
    monkeypatch.setattr(pending_requests, "get_pending_store", lambda *_args:pytest.fail("pending store initialized"))
    bridge = WebviewBridge(StatusBus(), SimpleNamespace(server_state={}))
    result = bridge.get_workflow_view()
    assert result["rows"] == [] and all(row["state"]=="unavailable" for row in result["sources"].values())


def test_future_completion_field_and_malformed_success_remain_unknown(fixture):
    data, bridge = fixture
    data.adapter._last_call_view = {"workflow":"daily_briefing","at":time.time(),
        "http_status":"200", "application_ok":"true", "completed":True, "payload":{"token":"PRIVATE"}}
    result = bridge.get_workflow_view()
    call = next(row for row in result["rows"] if row["id"]=="n8n-config:daily_briefing")["call"]
    assert call["http_status"] is None and call["application_ok"] is None
    assert "completed" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
