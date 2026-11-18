"""Passive rollout evidence from already attached Light runtime components."""

import math
import os
import sys
import time
from collections import Counter


_HISTORY_AGE = 24 * 60 * 60
_BUDGET_AGE = 30
_MCP_STATUS = {
    "not_connected", "disabled", "connecting", "discovering", "discovered",
    "connection_failed", "discovery_failed", "schema_changed", "disconnected",
    "call_timeout", "call_failed",
}


def _age(timestamp, now, limit):
    if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
            or not 0 < timestamp <= now):
        return "unavailable"
    return "stale" if now - timestamp > limit else "recorded"


def runtime_readiness(state, *, diagnostic=False):
    """Audio transport attachment proves neither input nor audible playback."""
    audio = state.get("audio_link")
    try:
        observation = audio.voice_snapshot() if audio is not None else {}
    except Exception:
        observation = {}
    return {
        "mode": "audio-startup-check" if diagnostic else "normal-light",
        "audio_transport": ("v2-connected" if observation.get("connected") is True
                            and observation.get("supported") is True else
                            "legacy-connected" if observation.get("connected") is True else
                            "disconnected" if observation.get("connected") is False else "unavailable"),
        "normal_readiness": "not-applicable" if diagnostic else "unverified",
    }


def _gate(name):
    value = os.environ.get(name, "")
    return "enabled" if value == "1" else "disabled" if value in ("", "0") else "invalid"


def _mcp(state, now):
    runtime = state.get("mcp_runtime")
    if runtime is None:
        return {"state": "error" if state.get("mcp_startup_failed") else "unavailable", "servers": []}
    from .capability_inventory import _name

    try:
        inventory = runtime.cached_inventory()
        servers = []
        for entry in inventory[:30]:
            status = entry.get("status")
            attached = entry.get("session_attached") is True
            tools = entry.get("tools")
            servers.append({
                "label": _name(entry.get("server")),
                "enabled": entry.get("enabled") is True,
                "observed_connection": status if status in _MCP_STATUS else "unknown",
                "session_attached": attached,
                "cache": ("unavailable" if tools is None else "stale" if not attached
                          else _age(entry.get("discovered_at"), now, _HISTORY_AGE)),
                "cached_tool_count": len(tools) if isinstance(tools, list) else None,
            })
        return {"state": "configured" if inventory else "unconfigured", "servers": servers,
                "server_count": len(inventory)}
    except Exception:
        return {"state": "error", "servers": []}


def _town(state, routing, reader, now):
    roster = routing.get("readiness_roster", {})
    if routing.get("catalog_revision") is None:
        return {"state": "unavailable"}, {
            "state": "disabled" if reader == "disabled" else "unavailable", "budget": "unavailable",
        }
    characters = {row["id"]: row for row in roster.get("characters", ())}
    opted_in = [row for row in characters.values() if row.get("life_binding_id")]
    lab = sys.modules.get(f"{__package__}.lab_state")
    world, linked = {"state": "unavailable"}, set()
    try:
        if lab is not None:
            world, linked = lab.resident_readiness(characters, state.get("identity_integration"))
    except Exception:
        world = {"state": "error"}
    if world["state"] == "loaded":
        world["configured_without_verified_link"] = sum(
            row["status"] == "configured" and row["id"] not in linked for row in characters.values()
        )
    budget_enabled = world.pop("life_budget_enabled", None)
    budget_age = _age(world.pop("life_budget_observed_at", None), now, _BUDGET_AGE)
    budget = ("stale" if budget_age == "stale" else "unavailable" if budget_age != "recorded"
              else "enabled" if budget_enabled is True else "disabled")
    brain_bindings = Counter(row.get("binding_id") for row in routing["routes"]
                             if row.get("capability") == "brain" and row.get("enrollment") == "verified"
                             and row.get("catalog_status") == "configured")
    candidates = sum(row["status"] == "configured" and row["id"] in linked
                     and brain_bindings[row["life_binding_id"]] == 1 for row in opted_in)
    life_state = ("disabled" if reader == "disabled" else
                  "unavailable" if reader != "enabled" or not world.get("scheduler_attached") or world["state"] != "loaded" else
                  "paused" if world["paused"] or not world["ticking"] or budget == "disabled" else
                  "unconfirmed" if budget != "enabled" else "awaiting-scheduler-checks" if candidates else
                  "no-candidates")
    return world, {"state": life_state, "budget": budget, "saved_ai": len(opted_in),
                   "saved_npc": len(characters) - len(opted_in),
                   "linked_exact_candidates": candidates}


def rollout_readiness(state):
    """Do not load configuration, open world stores or probe any adapter."""
    from .capability_inventory import text
    from .routing_view import routing_snapshot

    now = time.time()
    integration = _gate("FRIDAY_IDENTITY_INTEGRATION")
    reader = _gate("FRIDAY_IDENTITY_READER")
    inventory_state = "available"
    try:
        routing = routing_snapshot(state, readiness=True)
    except Exception:
        inventory_state = "unavailable"
        routing = {"identity": "error", "routes": []}
    rows = []
    for route in routing["routes"]:
        if route["kind"] == "observation":
            continue
        row = {key: route.get(key) for key in (
            "capability", "kind", "position", "configuration", "catalog_status",
            "enrollment", "binding_id", "saved_serving", "eligible", "cooldown",
        )}
        for key in ("provider", "model", "deployment"):
            value = route.get(key)
            row[key] = value if text(value) == value else None
        rows.append(row)
    town, life = _town(state, routing, reader, now)
    revision = routing.get("catalog_revision")
    catalog_state = "empty" if routing["identity"] == "ready" and revision is None else routing["identity"]
    history = {cap: _age(((routing.get("capabilities") or {}).get(cap, {}).get("last_success")
                          or {}).get("at"), now, _HISTORY_AGE)
               for cap in ("brain", "vision")}
    return {
        "available": True, "runtime": runtime_readiness(state),
        "identity": {"integration_gate": integration, "reader_gate": reader,
                     "state": catalog_state, "revision": revision,
                     "unresolved_aliases": (sum(row.get("kind") == "alias" for row in
                                                routing["readiness_roster"]["unresolved"])
                                            if catalog_state in ("ready", "empty") else None)},
        "route_inventory": inventory_state, "route_count": len(rows), "routes": rows[:100],
        "legacy_unmatched_routing": "possible", "live_route_eligibility": "unverified",
        "serving_history": history, "town": town, "life": life, "mcp": _mcp(state, now),
    }
