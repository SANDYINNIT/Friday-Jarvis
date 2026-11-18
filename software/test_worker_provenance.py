"""Real Workspace admission and provider selection against synthetic stores."""
import json
from threading import Event
from types import SimpleNamespace

import pytest

from source.server import brain_router, identity_integration, lab_authority, lab_state
from source.server import screen_understanding
from source.server.status_ui import WebviewBridge
from source.server.world import executor
from source.server.world.identity_catalog import AliasClaim, Route, read_shadow_projection
from source.server.world.roster import Roster
from source.server.world.store import Store
from test_task_attempt_lifecycle import env

MODEL = "vendor/same-model"
PRIMARY = "https://primary.private.invalid/v1"
FALLBACK = "https://fallback.private.invalid/v1"
ROUTES = [("brain_modal", MODEL, PRIMARY), ("brain_groq", MODEL, FALLBACK)]
MANIFEST = {"complete": True, "services": [], "deployments": {
    pool: {"base": base, "deployment": ref}
    for (pool, _, base), ref in zip(ROUTES, ("first", "second"))
}}


@pytest.fixture
def execution(env, tmp_path, monkeypatch):
    roster, shelf, _, path = env
    monkeypatch.setattr(brain_router, "resolve", lambda _: ROUTES)
    monkeypatch.setattr(brain_router.api_pools, "pool", lambda _: SimpleNamespace(
        next_key=lambda: "synthetic-key"))
    monkeypatch.setattr(brain_router.api_pools, "fail_model", lambda *_: None)
    monkeypatch.setattr(brain_router.api_pools, "model_succeed", lambda *_: None)
    monkeypatch.setattr(lab_authority, "pool_user_disabled", lambda *_: False)
    monkeypatch.setattr(lab_state, "_live_duty_active", lambda: False)
    integration = identity_integration.from_environment(brain_router, screen_understanding, {
        "FRIDAY_IDENTITY_INTEGRATION": "1",
        "FRIDAY_IDENTITY_DEPLOYMENTS": json.dumps(MANIFEST),
        "FRIDAY_IDENTITY_CATALOG_DB": str(tmp_path / "catalog.db"),
        "FRIDAY_IDENTITY_HISTORY_DB": str(tmp_path / "duty.db"),
    })
    assert integration is not None
    monkeypatch.setattr(lab_state, "_executor", None)
    monkeypatch.setattr(lab_state, "_executor_identity", None)
    lab_state.configure_execution_identity(integration)
    assert lab_state.executor().identity is integration
    return roster, shelf, path, integration


def response(text="fixture result"):
    return 200, {"choices": [{"message": {"content": text}}]}


def task(roster, *, review=False):
    row = roster.create_task("Fixture work", owner="FRIDAY", approval_required=review)
    task_id = row["task"]["id"]
    assert roster.assign(task_id, "research")["ok"]
    return task_id


def test_fallback_records_exact_result_route_and_keeps_historical_identity(execution, monkeypatch):
    roster, shelf, path, identity = execution
    view = read_shadow_projection(identity.catalog.path)
    by_ref = {b["deployment"]: b for b in view["bindings"].values()}
    assert by_ref["first"]["character_id"] != by_ref["second"]["character_id"]
    dispatched = []

    def post(base, key, payload, timeout):
        dispatched.append((base, payload["model"]))
        return (429, {}) if base == PRIMARY else response()

    monkeypatch.setattr(executor, "_post_chat", post)
    task_id = task(roster, review=True)
    result = lab_state.execute_task(task_id, background=False)
    assert result["ok"] and result["state"] == "awaiting_review"
    assert dispatched == [(PRIMARY, MODEL), (FALLBACK, MODEL)]
    row = roster.task(task_id)
    attempt = row["attempts"][-1]
    assert row["original_owner"] == "FRIDAY" and attempt["worker"]["id"] == "research"
    first, second = attempt["execution_calls"][0]["candidates"]
    assert first["binding_id"] == by_ref["first"]["id"] and first["outcome"] == "failed"
    assert second["character_id"] == by_ref["second"]["character_id"]
    assert second["binding_id"] == by_ref["second"]["id"]
    assert second["catalog_revision"] == view["revision"]
    assert second["capability"] == "brain" and second["outcome"] == "returned_result"
    assert attempt["result_executor"] == second
    assert "private.invalid" not in repr(attempt) and "synthetic-key" not in repr(attempt)
    assert WebviewBridge(None).review_task(task_id, "approve", result["attempt"])["ok"]
    assert shelf.read_text(result["artifact_id"]) == "fixture result"
    assert identity.shadow.snapshot()["duty"]["current"] == {}

    # A verified model rename keeps the character while the old binding retires.
    new = Route("groq", "second", "vendor/new-name")
    identity.catalog.reconcile([{**new.public(), "capabilities": ["brain"], "kind": "reasoning"}],
        complete=True, aliases=[AliasClaim(new, second["character_id"], "approved-rename", verified=True)])
    assert read_shadow_projection(identity.catalog.path)["bindings"][second["binding_id"]]["status"] == "retired"
    historical = read_shadow_projection(identity.catalog.path, revision=second["catalog_revision"])
    assert historical["bindings"][second["binding_id"]]["character_id"] == second["character_id"]
    reopened = Store(path)
    try:
        saved = Roster(reopened).task(task_id)
        assert saved["attempts"][-1]["result_executor"] == second
        assert saved["original_owner"] == "FRIDAY"
    finally:
        reopened.close()


def test_unbound_ambiguous_and_retry_keep_old_evidence(execution, monkeypatch):
    roster, _, _, identity = execution
    rows = read_shadow_projection(identity.catalog.path)["bindings"]
    primary = next(row for row in rows.values() if row["deployment"] == "first")
    other = next(row for row in rows.values() if row["deployment"] == "second")
    route = Route("modal", "first", MODEL)
    identity.catalog.reconcile([
        {**row, "capabilities": ["brain"], "kind": "reasoning"} for row in rows.values()
    ], complete=True, aliases=[AliasClaim(route, other["character_id"], "conflict", verified=True)])
    monkeypatch.setattr(brain_router, "resolve", lambda _: ROUTES[:1])
    monkeypatch.setattr(executor, "_post_chat", lambda *_: (503, {}))
    task_id = task(roster)
    failed = lab_state.execute_task(task_id, background=False)
    assert not failed["ok"]
    earlier = roster.task(task_id)["attempts"][0]
    assert len(earlier["execution_calls"]) > 1
    observed = earlier["execution_calls"][0]["candidates"][0]
    assert observed["deployment"] == primary["deployment"]
    assert observed["resolution"] == "unresolved" and "binding_id" not in observed
    assert "result_executor" not in earlier

    monkeypatch.setattr(brain_router, "resolve", lambda _: [(ROUTES[0][0], MODEL, PRIMARY + "/other")])
    monkeypatch.setattr(executor, "_post_chat", lambda *_: response())
    retried = lab_state.execute_task(task_id, retry=True, background=False)
    assert retried["ok"]
    assert not roster.record_execution(task_id, failed["attempt"], 1, [])["ok"]
    old, current = roster.task(task_id)["attempts"]
    assert old == earlier
    assert current["result_executor"]["resolution"] == "unresolved"
    assert "character_id" not in current["result_executor"]
    assert "deployment" not in current["result_executor"]


def test_cancelled_provider_cannot_record_late_result_or_dispatch_fallback(execution, monkeypatch):
    roster, shelf, _, _ = execution
    entered, release = Event(), Event()
    dispatched = []

    def blocked(base, *_):
        dispatched.append(base)
        entered.set()
        assert release.wait(4)
        return 503, {}

    monkeypatch.setattr(executor, "_post_chat", blocked)
    task_id = task(roster)
    started = lab_state.execute_task(task_id)
    thread = lab_state._executor_thread
    try:
        assert entered.wait(4)
        assert lab_state.cancel_execution(task_id=task_id, expected_attempt=started["attempt"])["cancelled"]
    finally:
        release.set()
        thread.join(timeout=4)
    assert dispatched == [PRIMARY]
    old = roster.task(task_id)["attempts"][0]
    assert "result_executor" not in old and not old.get("execution_calls")
    assert not shelf.list(task_id)
    monkeypatch.setattr(executor, "_post_chat", lambda *_: response("retry output"))
    assert lab_state.execute_task(task_id, retry=True, background=False)["ok"]
    assert roster.task(task_id)["attempts"][0] == old


def test_gate_off_and_custom_runner_do_not_infer_identity(execution, monkeypatch):
    roster, _, _, identity = execution
    monkeypatch.setattr(executor, "_post_chat", lambda *_: response())
    lab_state.configure_execution_identity(None)
    task_id = task(roster)
    assert lab_state.execute_task(task_id, background=False)["ok"]
    attempt = roster.task(task_id)["attempts"][0]
    observed = attempt["result_executor"]
    assert observed["provider"] == "modal" and observed["model"] == MODEL
    assert observed["resolution"] == "unresolved" and "character_id" not in observed

    lab_state.configure_execution_identity(identity)
    lab_state.executor()._runner = lambda *_, **__: {
        "ok": True, "text": "Custom result", "provider": "brain_modal", "model": MODEL,
        "result_executor": {**observed, "resolution": "verified", "character_id": "forged"}}
    task_id = task(roster)
    assert lab_state.execute_task(task_id, background=False)["ok"]
    attempt = roster.task(task_id)["attempts"][0]
    assert "result_executor" not in attempt and "execution_calls" not in attempt


def test_redirected_response_does_not_verify_executor(execution, monkeypatch):
    import requests

    roster, _, _, _ = execution
    _, body = response()
    monkeypatch.setattr(requests, "post", lambda *_, **__: SimpleNamespace(
        status_code=200, json=lambda: body, history=[object()]))
    task_id = task(roster)
    assert lab_state.execute_task(task_id, background=False)["ok"]
    observed = roster.task(task_id)["attempts"][0]["result_executor"]
    assert observed["redirected"] and observed["resolution"] == "unresolved"
    assert "character_id" not in observed and "binding_id" not in observed
