"""Read-only HUD routing projection. Provider modules must already be loaded."""

import math
import json
import os
import re
import sys
from collections import Counter
from uuid import UUID

from .world.identity_catalog import Route, _MODEL


CAPABILITIES = ("brain", "vision", "listen", "voice_out", "workflow")
POOLS = {
    "brain_modal": "modal", "vision_modal": "modal",
    "brain_groq": "groq", "stt_groq": "groq",
    "brain_gemini": "gemini", "vision_gemini": "gemini", "gemini_stt": "gemini", "gemini_tts": "gemini",
    "brain_openrouter": "openrouter", "vision_openrouter": "openrouter",
    "local": "ollama", "vision_local": "ollama", "stt_deepgram": "deepgram",
    "local_stt": "faster-whisper", "local_tts": "edge-tts", "windows_tts": "windows-sapi",
    "workflow_n8n": "n8n",
}
_LOCAL = {"local", "vision_local", "local_stt", "local_tts", "windows_tts"}
_POOL_POLICY = {"vision_modal", "vision_openrouter", "stt_groq", "stt_deepgram", "gemini_stt", "gemini_tts"}
_SECRET_LIKE = re.compile(r"(?i)(?:^|/)(?:sk-|wk-|ws-|gsk_|AIza|xox[baprs]-|gh[pousr]_)")


def _module(name):
    return sys.modules.get(f"{__package__}.{name}")


def _model(value):
    if isinstance(value, str) and len(value) <= 160 and _MODEL.fullmatch(value) and not _SECRET_LIKE.search(value):
        return value
    return None


def _timestamp(value):
    return value if type(value) in (int, float) and 0 < value < 1e11 and math.isfinite(value) else None


def _uuid(value):
    try:
        return value if str(UUID(value)) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _candidates(state):
    rows = []
    router, vision, stt, tts = (_module(name) for name in
                                ("brain_router", "screen_understanding", "cloud_stt", "gemini_tts"))
    order_known = router is not None and state.get("brain_router") is not None
    if order_known:
        for pool, model, base in router.resolve(""):
            rows.append(("brain", pool, router.LOCAL_MODEL if pool == "local" and model is None else model,
                         (base or router.LOCAL_BASE) if pool == "local" else base))
    if vision is not None:
        from .user_routes import candidates
        configured = candidates("vision")
        if configured is not None:
            rows.extend(("vision", pool, model, base) for pool, model, base in configured)
        else:
            if router is not None and router.MODAL_BASE:
                rows.append(("vision", "vision_modal", vision._modal_vision_model(), router.MODAL_BASE))
            rows.extend(("vision", "vision_gemini", model, vision.GEMINI_BASE)
                        for model in vision.VISION_GEMINI_CHAIN)
            rows.append(("vision", "vision_openrouter", vision.OPENROUTER_VISION_MODEL, vision.OPENROUTER_BASE))
            rows.extend(("vision", "vision_local", model, vision.LOCAL_CHAT_URL)
                        for model in dict.fromkeys([*vision.LOCAL_VISION_CHAIN, vision.select_vision_model()]))
    if stt is not None:
        models = {"stt_groq": stt.GROQ_STT_MODEL, "stt_deepgram": stt.DEEPGRAM_STT_MODEL,
                  "gemini_stt": stt.GEMINI_STT_MODEL}
        rows.extend(("listen", pool, models[pool], None) for pool in stt.STT_CHAIN)
    if state.get("local_stt_model"):
        rows.append(("listen", "local_stt", state["local_stt_model"], None))
    if tts is not None and state.get("tts_engine") is not None:
        rows.extend(("voice_out", "gemini_tts", model, None) for model in tts.GEMINI_TTS_MODEL_CHAIN)
        rows.extend((("voice_out", "local_tts", tts.LOCAL_TTS_MODEL, None),
                     ("voice_out", "windows_tts", None, None)))
    return order_known, list(dict.fromkeys(rows))


def _route_key(cap, route):
    return (cap, route.provider, route.deployment, route.model)


def _public_route(record):
    model = _model(record.get("model"))
    provider, deployment = record.get("provider"), record.get("deployment")
    if model is None or any(isinstance(value, str) and _SECRET_LIKE.search(value)
                            for value in (provider, deployment)):
        return None
    try:
        return Route(provider, deployment, model)
    except ValueError:
        return None


def _observation(record, cap, catalog_routes):
    if not record:
        return None
    route = _public_route(record.get("route") or {})
    if route is None:
        return {"provider": None, "model": None, "resolution": "unbound",
                "at": _timestamp(record.get("completed_at"))}
    entry = catalog_routes.get(_route_key(cap, route))
    bound = bool(entry and record.get("binding_id") == entry["binding_id"] and
                 record.get("character_id") == entry["character_id"])
    return {**route.public(), "resolution": "bound" if bound else "unbound",
            "at": _timestamp(record.get("completed_at"))}


def routing_snapshot(state, *, readiness=False):
    order_known, candidates = _candidates(state)
    api, authority, n8n = (_module(name) for name in ("api_pools", "lab_authority", "n8n_runtime"))
    workflow = n8n.cached_configuration() if n8n is not None else None
    candidates.append(("workflow", "workflow_n8n", None, None))
    identity = state.get("identity_integration")
    gate_on = os.environ.get("FRIDAY_IDENTITY_INTEGRATION" if readiness else "FRIDAY_IDENTITY_READER") == "1"
    shadow, matches, identity_state = {}, [None] * len(candidates), "disabled"
    if gate_on:
        identity_state = "unavailable"
        if identity is not None:
            try:
                arguments = [
                    (cap, pool, model, base if _model(model) else None)
                    for cap, pool, model, base in candidates
                ]
                observed, linked = (identity.read_routing(arguments, enrolled_only=True)
                                    if readiness else identity.read_routing(arguments))
                if observed.get("enabled") is True:
                    shadow, matches, identity_state = observed, linked, "ready"
            except Exception:
                identity_state = "error"
    revision = shadow.get("catalog_revision")
    catalog_routes = {}
    roster = shadow.get("roster", {})
    bindings = [(row, char["id"]) for char in roster.get("characters", ())
                for row in char.get("bindings", ())]
    bindings.extend((row, None) for row in roster.get("services", ()))
    service_caps = {"stt": "listen", "tts": "voice_out", "n8n": "workflow"}
    counts = Counter()
    aliases = {_public_route(row["route"]) for row in roster.get("unresolved", ())
               if readiness and row.get("kind") == "alias"}
    for binding, character_id in bindings:
        cap = service_caps.get(binding["capability"], binding["capability"])
        route = _public_route(binding)
        status = binding.get("status")
        if route is None or cap not in CAPABILITIES or status not in ("configured", "disabled", "retired"):
            continue
        key = _route_key(cap, route)
        counts[key] += 1
        catalog_routes[key] = {
            "binding_id": _uuid(binding["id"]), "character_id": _uuid(character_id),
            "catalog_status": status, "capability": cap, **route.public(),
        }
        if binding.get("saved_serving") is False:
            catalog_routes[key]["catalog_serving_blocked"] = True
        if readiness:
            catalog_routes[key]["saved_serving"] = binding.get("saved_serving")
    router = state.get("brain_router")
    get_outcomes = getattr(router, "route_observations", None)
    outcomes = {(row["pool"], row["model"]): row for row in get_outcomes()} if callable(get_outcomes) else {}
    duty = shadow.get("duty", {})
    capabilities = {}
    for cap in CAPABILITIES:
        current = _observation(duty.get("current", {}).get(cap), cap, catalog_routes)
        success = _observation(duty.get("last_success", {}).get(cap), cap, catalog_routes)
        if cap == "brain" and success is None:
            latest = max(outcomes.values(), key=lambda row: row.get("success_at") or 0, default={})
            if at := _timestamp(latest.get("success_at")):
                success = {"provider": POOLS.get(latest["pool"]), "model": _model(latest["model"]),
                           "at": at, "resolution": "session"}
        capabilities[cap] = {"current": current, "last_success": success,
                             "duty_observable": identity_state == "ready" and cap in ("brain", "vision")}
    cloud = {
        "brain": getattr(router, "_enabled", None),
        "vision": api.cloud_vision_enabled() if api else None,
        "listen": api.cloud_stt_enabled() if api else None,
    }
    order = 0
    rows, seen = [], set()
    for (cap, pool, model, _), match in zip(candidates, matches):
        provider = POOLS.get(pool)
        if provider is None:
            continue
        if cap == "brain":
            order += 1
        public_model = _model(model)
        binding = catalog_routes.get(_route_key(cap, match), {}) if match else {}
        if binding:
            seen.add(_route_key(cap, match))
        quota, cooldown = (api.cached_route_state(pool, model)
                           if api and pool not in _LOCAL and cap != "workflow" else ("unknown", None))
        configured = ("configured" if pool in _LOCAL or quota == "loaded"
                      else "unconfigured" if quota == "unconfigured" else "unknown")
        if cap == "workflow" and workflow is not None:
            configured = "configured" if workflow else "unconfigured"
        eligible = None
        read_policy = getattr(authority, "cached_route_eligibility", None)
        if callable(read_policy) and (cap == "brain" or pool in _POOL_POLICY):
            eligible = read_policy(pool, (model or "") if cap == "brain" else "")
        if pool not in _LOCAL and cloud.get(cap) is False:
            eligible = False
        if cap == "workflow" and workflow is False:
            eligible = False
        if binding.get("catalog_serving_blocked"):
            eligible = False
        timestamps = outcomes.get((pool, model), {}) if cap == "brain" else {}
        route_id = json.dumps([cap, pool, public_model], separators=(",", ":"))
        if any(row["id"] == route_id for row in rows):
            route_id = f"{route_id}:{len(rows)}"
        rows.append({
            "id": binding.get("binding_id") or route_id,
            "capability": cap, "provider": provider, "model": public_model, "kind": "runtime",
            "position": order if cap == "brain" else None,
            "configuration": configured, "eligible": eligible, "cooldown": cooldown,
            "success_at": _timestamp(timestamps.get("success_at")),
            "failure_at": _timestamp(timestamps.get("failure_at")),
            **binding,
        })
        if readiness and cap in ("brain", "vision"):
            if identity_state != "ready":
                enrollment = "unknown"
            elif match and (match in aliases or counts[_route_key(cap, match)] > 1):
                enrollment = "ambiguous"
            elif not binding or not binding.get("binding_id") or not binding.get("character_id"):
                enrollment = "unmatched"
            else:
                enrollment = "retired" if binding["catalog_status"] == "retired" else "verified"
            rows[-1]["enrollment"] = enrollment
    for key, binding in catalog_routes.items():
        if key not in seen:
            rows.append({**binding, "id": binding["binding_id"], "kind": "catalog",
                         "configuration": "unknown", "eligible": None})
            if readiness and binding["capability"] in ("brain", "vision"):
                rows[-1]["enrollment"] = ("retired" if binding["catalog_status"] == "retired" else
                                          "ambiguous" if counts[key] > 1 or _public_route(binding) in aliases
                                          else "unmatched")
    for cap, summary in capabilities.items():
        for field, flag in (("current", "serving"), ("last_success", "last_success")):
            item = summary[field]
            if not item or item["resolution"] == "session":
                continue
            def matches_exact(row):
                return row["capability"] == cap and all(
                    item.get(key) == row.get(key) for key in ("provider", "deployment", "model"))
            row = next((row for row in rows if matches_exact(row) and row.get("deployment") and
                        (item["resolution"] == "bound" or row["kind"] == "observation")), None)
            if row is None and item.get("deployment"):
                row = {**item, "id": f"observed:{cap}:{item['provider']}:{item['deployment']}:{item['model']}",
                       "capability": cap, "kind": "observation", "configuration": "unknown", "eligible": None}
                rows.append(row)
            if row is not None:
                row[flag] = True
                if field == "last_success":
                    row["success_at"] = item["at"]
    result = {"available": True, "capabilities": capabilities, "routes": rows,
              "brain_order_known": order_known, "identity": identity_state, "catalog_revision": revision}
    if readiness:
        result["readiness_roster"] = roster
    return result
