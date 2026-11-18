"""Default-off adapter for catalog inventory and runtime duty observations."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
import time
from threading import RLock
from uuid import uuid4

from .world.identity_catalog import (
    IdentityCatalog, Route, discover_configuration, read_shadow_projection, route_fields,
    serving_editable,
)
from .world.identity_shadow import DutyShadow, TOWN_FAILURE_SECONDS

_SCOPE = ContextVar("friday_identity_observation", default=None)
_REQUEST_SCOPE = ContextVar("friday_admitted_request_guard", default=None)
_PROVIDERS = {
    "brain_modal": "modal", "vision_modal": "modal", "brain_groq": "groq",
    "brain_gemini": "gemini", "vision_gemini": "gemini",
    "brain_openrouter": "openrouter", "vision_openrouter": "openrouter",
    "local": "ollama", "vision_local": "ollama",
}


def provider_for_pool(pool):
    return _PROVIDERS.get(pool)


def _base(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("configured base unavailable")
    return value.strip().rstrip("/")


def _deployment_base(provider, base):
    base = _base(base)
    if provider == "ollama":
        return base.removesuffix("/api/chat")
    return base


def discover_inventory(manifest, router, vision, *, require_all=False):
    """Return complete public rows and transient exact endpoint matchers.

    The explicit manifest declares provisioned pools, independent of quota and
    credentials. A declaration which cannot be resolved makes the entire scan
    partial. Only an explicitly complete manifest can remove a route.
    Installed startup additionally requires a declaration for every resolver pool.
    """
    if (not isinstance(manifest, dict) or manifest.get("complete") is not True
            or not isinstance(manifest.get("deployments"), dict)
            or not isinstance(manifest.get("services"), list)):
        raise ValueError("complete identity inventory declaration required")
    declarations = manifest["deployments"]
    candidates = {}
    if require_all or set(declarations) & {"brain_modal", "brain_groq", "brain_gemini",
                             "brain_openrouter", "local"}:
        for pool, model, base in router.resolve(""):
            candidates.setdefault(pool, []).append(
                ("brain", router.LOCAL_MODEL if pool == "local" else model,
                 router.LOCAL_BASE if pool == "local" else base))
    if "vision_modal" in declarations or (require_all and router.MODAL_BASE):
        candidates["vision_modal"] = [("vision", vision._modal_vision_model(), router.MODAL_BASE)]
    if require_all or "vision_gemini" in declarations:
        candidates["vision_gemini"] = [("vision", model, vision.GEMINI_BASE)
                                       for model in vision.VISION_GEMINI_CHAIN]
    if require_all or "vision_openrouter" in declarations:
        candidates["vision_openrouter"] = [("vision", vision.OPENROUTER_VISION_MODEL,
                                             vision.OPENROUTER_BASE)]
    if require_all or "vision_local" in declarations:
        models = list(vision.LOCAL_VISION_CHAIN) + [vision.select_vision_model()]
        candidates["vision_local"] = [("vision", model, vision.LOCAL_CHAT_URL) for model in models]
    if require_all and set(candidates) != set(declarations):
        raise ValueError("manifest does not cover the resolver inventory")
    rows, matchers, roots = [], {}, {}
    for pool, declaration in declarations.items():
        provider = provider_for_pool(pool)
        if not provider or not candidates.get(pool) or not isinstance(declaration, dict):
            raise ValueError("declared route unavailable")
        ref = declaration.get("deployment")
        declared = _base(declaration.get("base"))
        root = _deployment_base(provider, declared)
        if roots.setdefault((provider, ref), root) != root:
            raise ValueError("deployment reference has conflicting endpoints")
        models = frozenset(model for _, model, _ in candidates[pool])
        for cap, model, base in candidates[pool]:
            if _base(base) != declared:
                raise ValueError("declared route endpoint changed or is unavailable")
            route = Route(provider, ref, model)
            rows.append({**route.public(), "kind": "reasoning", "capabilities": [cap],
                         "enabled": declaration.get("enabled", True)})
            matchers[(cap, pool)] = (declared, provider, ref, models)
    for row in manifest["services"]:
        if row.get("kind") != "service":
            raise ValueError("service declaration requires service kind")
        rows.append(row)
    discover_configuration(rows)  # Validate the full scan before opening a store.
    return rows, matchers


class IdentityIntegration:
    def __init__(self, catalog, shadow, matchers):
        self.catalog = catalog
        self.shadow = shadow
        self._matchers = matchers
        self._lock = RLock()

    def refresh(self, manifest, router, vision):
        """Preserve the prior revision and route matchers on a failed scan."""
        try:
            rows, matchers = discover_inventory(manifest, router, vision)
            with self._lock:
                self.catalog.reconcile(rows, complete=True)
                self._matchers = matchers
            return True
        except Exception:
            return False

    def _match(self, cap, pool, model, base):
        matcher = self._matchers.get((cap, pool))
        if (matcher and base and
                _deployment_base(matcher[1], base) == _deployment_base(matcher[1], matcher[0])):
            return Route(matcher[1], matcher[2], model)
        return None

    def enrolled(self, binding):
        return any(cap == binding["capability"] and (provider, deployment) ==
                   (binding["provider"], binding["deployment"]) and binding["model"] in models
                   for (cap, _), (_, provider, deployment, models) in self._matchers.items())

    def set_serving(self, character_id, binding_id, capability, allowed, revision):
        with self._lock:
            return self.catalog.set_serving(character_id, binding_id, capability,
                                            allowed, revision, self.enrolled)

    def background_route(self, binding):
        """Exact configured Brain deployment. No fallback or duty observation."""
        if binding["capability"] != "brain" or binding["status"] != "configured":
            return None
        with self._lock:
            candidates = [(pool, base, binding["model"], provider)
                          for (cap, pool), (base, provider, ref, models) in self._matchers.items()
                          if cap == "brain" and provider == binding["provider"]
                          and ref == binding["deployment"] and binding["model"] in models]
            return candidates[0] if len(candidates) == 1 else None

    def town_failure_state(self, failure):
        """Read exact cached route cooldown after a verified failed attempt."""
        from .api_pools import cached_route_state

        route = failure["route"]
        with self._lock:
            pools = [pool for (cap, pool), (_, provider, deployment, models) in self._matchers.items()
                     if cap == failure["capability"] and (provider, deployment) ==
                     (route["provider"], route["deployment"]) and route["model"] in models]
        if len(pools) != 1:
            return None
        _, cooldown = cached_route_state(pools[0], route["model"])
        if cooldown:
            return "cooldown"
        if time.monotonic() - failure["observed_at"] < TOWN_FAILURE_SECONDS:
            return "recent_failure"
        return None

    def set_background_life(self, character_id, binding_id, mode, revision, installed):
        with self._lock:
            return self.catalog.set_life(character_id, binding_id, mode, revision,
                                         installed, self.background_route)

    def route_disabled(self, cap, pool, model, base):
        """Exclude enrolled exact routes only. Keep unbound fallback behavior."""
        with self._lock:
            try:
                route = self._match(cap, pool, model, base)
                if route is None:
                    return False
                view = read_shadow_projection(self.catalog.path)
                return any(row["capability"] == cap and route_fields(row) == route.public()
                           and row.get("can_serve") is False
                           for row in view["bindings"].values())
            except Exception:
                # A declared pool cannot bypass its saved exclusions if the
                # authoritative catalog cannot be read.
                return (cap, pool) in self._matchers

    def read_routing(self, candidates, *, enrolled_only=False):
        """Capture exact public matches against the same catalog read."""
        with self._lock:
            matches = []
            for cap, pool, model, base in candidates:
                matcher = self._matchers.get((cap, pool))
                if not base or (enrolled_only and (not matcher or model not in matcher[3])):
                    matches.append(None)
                else:
                    matches.append(self._match(cap, pool, model, base))
            return self.shadow.snapshot(), matches

    def execution_binding(self, capability, pool, model, base):
        """Snapshot a worker's exact route without recording assistant duty."""
        with self._lock:
            matcher = self._matchers.get((capability, pool))
            if not matcher or model not in matcher[3] or _base(base) != matcher[0]:
                return {}
            route = Route(matcher[1], matcher[2], model).public()
            view = read_shadow_projection(self.catalog.path)
            matches = [row for row in view["bindings"].values()
                       if row["capability"] == capability and route_fields(row) == route]
            if len(matches) == 1 and serving_editable(view, matches[0]):
                binding = matches[0]
                return {**route, "resolution": "verified", "catalog_revision": view["revision"],
                        "binding_id": binding["id"], "character_id": binding["character_id"]}
            return {**route, "resolution": "unresolved"}

    def select(self, request, cap, generation, pool, model, base):
        with self._lock:
            route = self._match(cap, pool, model, base)
            if route is not None:
                return self.shadow.observe_selected(request, cap, generation, route)


def from_environment(router, vision, env=None):
    """Bootstrap only when explicitly enabled and the inventory is complete."""
    env = os.environ if env is None else env
    if env.get("FRIDAY_IDENTITY_INTEGRATION") != "1":
        return None
    try:
        manifest = json.loads(env["FRIDAY_IDENTITY_DEPLOYMENTS"])
        rows, matchers = discover_inventory(manifest, router, vision)
        history_path = env["FRIDAY_IDENTITY_HISTORY_DB"]
        catalog = IdentityCatalog(env["FRIDAY_IDENTITY_CATALOG_DB"])
        shadow = DutyShadow(catalog.path, history_path, enabled=True)
        catalog.reconcile(rows, complete=True)
        return IdentityIntegration(catalog, shadow, matchers)
    except Exception:
        return None


def _safe(call, *args):
    try:
        return call(*args)
    except Exception:
        return None


@dataclass(frozen=True)
class _Scope:
    turn: object
    generation: object


class _Turn:
    def __init__(self, state, integration, owner, generation):
        self.state, self.integration, self.owner = state, integration, owner
        self.generation = generation
        self.request = integration.shadow.begin_request(uuid4().hex)
        self.lock = RLock()
        self.sequence = {"brain": 0, "vision": 0}
        self.current = {}
        self.brain_output = None
        self.closed = False

    def live(self, generation):
        return (not self.closed and self.request is not None
                and self.state.get("identity_turn") is self
                and self.state.get("turn_ownership") is self.owner
                and generation is self.generation
                and self.state.get("turn_attempt") is generation
                and self.state.get("cancelled_owner") is not self.owner
                and not self.state.get("tool_cap_terminated")
                and not self.state.get("turn_budget_enforced"))

    def selected(self, generation, cap, pool, model, base):
        with self.lock:
            if not self.live(generation):
                return None
            prior = self.current.pop(cap, None)
            if prior:
                _safe(self.integration.shadow.finish, prior, "cancelled")
            self.sequence[cap] += 1
            attempt = self.integration.select(
                self.request, cap, self.sequence[cap], pool, model, base)
            if attempt:
                self.current[cap] = attempt
            return attempt

    def finish(self, generation, cap, attempt, outcome):
        with self.lock:
            if (attempt is None or not self.live(generation)
                    or self.current.get(cap) is not attempt):
                return False
            result = self.integration.shadow.finish(attempt, outcome)
            if result:
                self.current.pop(cap, None)
            return result

    def observe_brain_output(self, generation):
        with self.lock:
            if self.live(generation):
                self.brain_output = self.current.get("brain")

    def finish_brain(self, generation, outcome):
        with self.lock:
            attempt = self.current.get("brain")
            if outcome == "success" and (attempt is None or self.brain_output is not attempt):
                outcome = "failure"  # A bare completion status proves no model output.
            return self.finish(generation, "brain", attempt, outcome)

    def advance(self, owner, generation):
        with self.lock:
            if self.closed or owner is not self.owner:
                return
            for attempt in self.current.values():
                _safe(self.integration.shadow.finish, attempt, "cancelled")
            self.current.clear()
            self.generation = generation

    def close(self):
        with self.lock:
            self.closed = True
            self.current.clear()
            _safe(self.integration.shadow.end_request, self.request)


def admit(state, owner):
    """Called only by the real admission boundary after its generation exists."""
    if state.get("identity_integration") is None or state.get("turn_attempt") is None:
        return
    prior = state.pop("identity_turn", None)
    if prior:
        _safe(prior.close)
    turn = _safe(_Turn, state, state["identity_integration"], owner, state["turn_attempt"])
    if turn:
        state["identity_turn"] = turn


def advance(state, owner):
    turn = state.get("identity_turn")
    if turn and state.get("turn_attempt") is not None:
        _safe(turn.advance, owner, state["turn_attempt"])


def close(state, owner):
    turn = state.get("identity_turn")
    if turn and turn.owner is owner:
        state.pop("identity_turn", None)
        _safe(turn.close)


def request_current():
    """Read existing owner/generation revocation state; never grant authority."""
    scope = _REQUEST_SCOPE.get()
    if scope is None:
        return True  # Standalone caller retains its existing authorization boundary.
    state, generation, owner = scope
    return (owner is not None and state.get("turn_ownership") is owner
            and state.get("turn_attempt") is generation
            and state.get("cancelled_owner") is not owner
            and not state.get("tool_cap_terminated")
            and not state.get("turn_budget_enforced"))


@contextmanager
def scoped(state, generation):
    # Carry the existing admission fence even when identity observation is off.
    guard = _REQUEST_SCOPE.set((state, generation, state.get("turn_ownership")))
    try:
        turn = state.get("identity_turn")
        if turn is None:
            yield
            return
        token = _SCOPE.set(_Scope(turn, generation))
        try:
            yield
        finally:
            _SCOPE.reset(token)
    finally:
        _REQUEST_SCOPE.reset(guard)


def brain_dispatched(state, interpreter, generation):
    turn = state.get("identity_turn")
    if turn is None:
        return

    def observe():
        ticket = state["brain_router"].current
        if ticket is None:
            return
        pool, model = ticket[0], ticket[2]  # never pass the credential slot
        llm = interpreter.llm
        if llm.model == (model if pool == "local" else "openai/" + model):
            turn.selected(generation, "brain", pool, model, llm.api_base)

    _safe(observe)


def brain_output(state, generation):
    turn = state.get("identity_turn")
    if turn:
        _safe(turn.observe_brain_output, generation)


def brain_result(state, generation, outcome):
    turn = state.get("identity_turn")
    if turn:
        _safe(turn.finish_brain, generation, outcome)


def serving_disabled(capability, pool, model, base):
    """Use the admitted integration when available, including worker threads."""
    scope = _SCOPE.get()
    if scope is not None:
        return scope.turn.integration.route_disabled(capability, pool, model, base)
    # Some image/composer calls are outside an admitted observation scope.
    # Reuse the actual live router's integration instead of a second policy.
    from .brain_router import _LIVE_ROUTER
    router = _LIVE_ROUTER.get("router")
    return bool(router and router.catalog_route_disabled(capability, pool, model, base))


@contextmanager
def vision_attempt(pool, model, base):
    """Observe a real vision transport attempt only within an admitted scope."""
    scope = _SCOPE.get()
    attempt = None
    successful = False
    if scope is not None:
        attempt = _safe(scope.turn.selected, scope.generation, "vision", pool, model, base)

    def success():
        nonlocal successful
        successful = True

    try:
        yield success
    finally:
        if attempt is not None:
            _safe(scope.turn.finish, scope.generation, "vision", attempt,
                  "success" if successful else "failure")

