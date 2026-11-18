"""Local workflow metadata and exact owner-decision navigation. No discovery."""

import re
import sys
import time
from datetime import datetime

from .capability_inventory import _fields, text


_WORKFLOW = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")
_REQUEST = re.compile(r"pr-[0-9a-f]{8}")


def _stamp(value):
    return value if type(value) in (int, float) and 0 < value < 1e11 else None


def _iso(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _adapter():
    module = sys.modules.get(f"{__package__}.n8n_runtime")
    adapter = _fields(module).get("_ADAPTER")
    adapter_type = _fields(module).get("N8NAdapter")
    return adapter if adapter_type is not None and type(adapter) is adapter_type else None


def _definitions():
    adapter = _adapter()
    if adapter is None:
        return {"state": "unavailable", "rows": [], "enabled": None, "connection": "unknown"}
    config = adapter.config
    if type(config) is not dict or type(config.get("workflows")) is not dict or type(config.get("enabled")) is not bool:
        return {"state": "error", "rows": [], "enabled": None, "connection": "unknown"}
    cache = _fields(adapter).get("_cache") or {}
    cache_at = cache.get("at")
    age = max(0, time.monotonic()-cache_at) if _stamp(cache_at) else None
    connection = "unknown"
    if age is not None and type(cache.get("connected")) is bool:
        connection = "connected" if cache["connected"] else "disconnected"
    observation = _fields(adapter).get("_last_call_view")
    rows, rejected = [], 0
    for name, spec in list(config["workflows"].items())[:50]:
        if (not isinstance(name, str) or not _WORKFLOW.fullmatch(name)
                or text(name) != name or type(spec) is not dict
                or not isinstance(spec.get("description", ""), str)
                or not isinstance(spec.get("webhook_url"), str)
                or not spec["webhook_url"].startswith(("http://", "https://"))):
            rejected += 1
            continue
        call = None
        if type(observation) is dict and observation.get("workflow") == name:
            code = observation.get("http_status")
            application = observation.get("application_ok")
            call = {"at": _stamp(observation.get("at")),
                    "http_status": code if type(code) is int and 100 <= code <= 599 else None,
                    "application_ok": application if type(application) is bool else None}
        rows.append({
            "id": f"n8n-config:{name}", "name": name, "kind": "definition",
            "source": "Local n8n configuration", "description": text(spec.get("description")),
            "enabled": config["enabled"], "call": call,
        })
    return {"state": "partial" if rejected else "ready", "rows": rows, "enabled": config["enabled"],
            "connection": connection, "connection_age": age,
            "connection_stale": age is not None and age >= 60,
            "truncated": len(config["workflows"]) > 50}


def _pending(interpreter):
    store = (_fields(interpreter).get("server_state") or {}).get("pending_store")
    if store is None:
        return {"state": "unavailable", "rows": []}
    rows, rejected = [], 0
    for item in store.owner_views()[:40]:
        correlation = item.get("correlation_id")
        state = item.get("state")
        if not isinstance(correlation, str) or not _REQUEST.fullmatch(correlation) or state not in (
                "open", "answered", "ignored", "expired"):
            rejected += 1
            continue
        rows.append({"id": f"n8n-pending:{correlation}", "correlation_id": correlation,
                     "name": text(item.get("question"),500) or "Owner decision",
                     "kind": "decision", "source": "Local n8n owner-decision store", "state": state,
                     "created_at": _iso(item.get("created_at")),
                     "resolved_at": _iso(item.get("resolved_at"))})
    return {"state": "partial" if rejected else "ready", "rows": rows}


def workflow_snapshot(interpreter):
    sources, rows = {}, []
    for name, reader in (("definitions", _definitions), ("decisions", lambda: _pending(interpreter))):
        try:
            payload = reader()
            rows.extend(payload.pop("rows"))
            sources[name] = payload
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            sources[name] = {"state": "error"}
    return {"available": True, "sources": sources, "rows": rows, "read_at": time.time()}


def request_target(interpreter, reference):
    if not isinstance(reference, str) or not reference.startswith("n8n-pending:"):
        return {"ok": False, "reason": "An exact n8n decision reference is required."}
    correlation = reference.removeprefix("n8n-pending:")
    if not _REQUEST.fullmatch(correlation):
        return {"ok": False, "reason": "Invalid n8n correlation ID."}
    try:
        pending = _pending(interpreter)
        if pending["state"] not in ("ready", "partial") or not any(
                row["id"] == reference for row in pending["rows"]):
            return {"ok": False, "reason": "This decision is no longer available."}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return {"ok": False, "reason": "The decision could not be checked."}
    return {"ok": True, "domain": "integration", "correlation_id": correlation}
