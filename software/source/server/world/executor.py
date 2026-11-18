"""Bounded agent executor: turns an ASSIGNED goal into a real provider call.

This is the missing piece between the roster's state machine and actual work.
Until now `assign_task()` moved a task to `running` and nothing executed it.

DESIGN CONTRACT
---------------
* **Uses the configured chain, not a parallel one.** Candidates come from
  `brain_router.resolve()` (read-only: it returns `(pool, model, api_base)`
  tuples and never touches `interpreter.llm`, which is shared with voice turns)
  and keys come from `api_pools.pool(...).next_key()`, so cooldowns, pacing and
  the three-strikes ban apply exactly as they do to a normal turn.
* **Bounded on four axes**: model calls, steps, wall-clock seconds, and an
  explicit cancel event. Nothing runs unbounded.
* **Permission-gated**: an agent must hold the `llm` permission to spend a
  model call. Refusal is a visible task state, never a silent no-op.
* **No duplicate external effects**: a task that is interrupted (cancel or
  restart) is parked as `paused` with a reason and must be resumed
  deliberately. Artifacts are written only on completion, so a re-run never
  double-writes one.
* **Honest events**: every transition is published through `lab_state.note()`
  so the roster/world rails show what actually happened.
* **Secrets**: prompts and results pass through `delegation.redact_secrets`,
  and keys are never stored, logged or returned.

Simulated character life (`sim.py`) stays completely separate: this module
never reads or writes needs, positions or world state.
"""

from __future__ import annotations

import os
import threading
import time

# Permission an agent must hold to spend a model call.
LLM_PERMISSION = "llm"

DEFAULT_MAX_MODEL_CALLS = 4
DEFAULT_MAX_STEPS = 6
DEFAULT_MAX_SECONDS = 90.0
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TOKENS = 700

SYSTEM_DEFAULT = (
    "You are a bounded worker inside FRIDAY. Answer the task directly and "
    "completely in plain text. Do not invent facts you were not given, do not "
    "claim to have run tools you did not run, and keep the reply under 120 "
    "words unless the task asks for a list."
)


class AgentExecutor:
    """Executes one assigned task per call, with budgets and cancellation."""

    def __init__(self, roster, runner=None, note=None, identity=None):
        self.roster = roster
        self._runner = runner            # injected; defaults to provider_runner
        self._note = note or (lambda kind, data=None: None)
        self.identity = identity
        self._cancel = threading.Event()      # stops the run IN FLIGHT
        self._active = None
        self._execution = threading.Lock()
        self._lock = threading.Lock()
        self.stats = {"executions": 0, "completed": 0, "failed": 0,
                      "cancelled": 0, "refused": 0, "model_calls": 0,
                      "last_error": "", "last_latency": None,
                      "last_provider": None, "last_model": None}

    # -- control ------------------------------------------------------------
    def cancel(self, reason="user", *, task_id=None, attempt=None) -> dict:
        """Park only the specified attempt, or the current one on global Stop."""
        with self._lock:
            target = self._active
            if task_id is not None:
                if attempt:
                    target = (str(task_id), attempt)
                elif target is not None and target[0] != str(task_id):
                    target = None
        if target is None:
            return {"ok": True, "cancelled": False, "reason": "no active attempt"}
        task_id, attempt = target
        paused = self.roster.pause_for_rest(task_id, str(reason), attempt=attempt)
        if not paused.get("ok"):
            return {**paused, "cancelled": False}
        with self._lock:
            if self._active == target:
                self._cancel.set()
            self.stats["cancelled"] += 1
        self._note("task_cancel", {"task": task_id, "attempt": attempt, "reason": str(reason)})
        return {"ok": True, "cancelled": True, "state": "paused", "attempt": attempt}

    def clear_cancel(self):
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.stats, running=self._active is not None)

    # -- budgets ------------------------------------------------------------
    @staticmethod
    def budgets(agent, task) -> dict:
        """Effective budgets: the tighter of the agent's and the task's."""
        agent_budget = (agent or {}).get("budget") or {}
        merged = {"max_model_calls": DEFAULT_MAX_MODEL_CALLS,
                  "max_steps": DEFAULT_MAX_STEPS,
                  "max_seconds": DEFAULT_MAX_SECONDS}
        for key in list(merged):
            try:
                if key in agent_budget and int(agent_budget[key]) > 0:
                    merged[key] = min(merged[key], int(agent_budget[key]))
            except Exception:
                pass
            try:
                if key in (task or {}).get("budget", {}) and \
                        int((task or {}).get("budget")[key]) > 0:
                    merged[key] = min(merged[key],
                                      int((task or {}).get("budget")[key]))
            except Exception:
                pass
        return merged

    # -- execution ----------------------------------------------------------
    def execute(self, task_id, *, attempt=None, system=SYSTEM_DEFAULT,
                timeout=DEFAULT_TIMEOUT_SECONDS) -> dict:
        """Execute one admitted attempt; direct callers use the same roster claim."""
        if not self._execution.acquire(blocking=False):
            return {"ok": False, "reason": "worker is busy"}
        try:
            task_id = str(task_id)
            if attempt is None:
                admitted = self.roster.admit(task_id)
                if not admitted.get("ok"):
                    return admitted
                attempt = admitted["attempt"]
            with self._lock:
                self._active = (task_id, attempt)
                self._cancel.clear()
            claimed = self.roster.mark_running(task_id, attempt=attempt)
            if not claimed.get("ok"):
                return claimed
            self._note("task_started", {"task": task_id, "attempt": attempt,
                                        "agent": claimed["task"].get("owner"),
                                        "bucket": claimed["task"].get("category", "user_work")})
            return self._run_attempt(claimed["task"], attempt, system, timeout)
        finally:
            with self._lock:
                self._active = None
                self._cancel.clear()
            self._execution.release()

    def _run_attempt(self, task, attempt, system, timeout):
        task_id = task["id"]
        agent = self.roster.agent(task.get("owner") or "")
        context = {"task": task_id, "attempt": attempt, "agent": agent.get("id"),
                   "bucket": task.get("category", "user_work")}

        def event(kind, **extra):
            self._note(kind, {**context, **extra})

        def lost():
            current = self.roster.task(task_id)
            return {"ok": False, "reason": "attempt no longer owns execution",
                    "state": current.get("state"), "attempt": attempt}

        def fail(reason, **extra):
            outcome = self.roster.fail(task_id, reason, attempt=attempt)
            if not outcome.get("ok"):
                result = lost()
                return {**result, "reason": outcome.get("reason") or result["reason"]}
            self._bump("failed")
            with self._lock:
                self.stats["last_error"] = reason[:200]
            event("task_failed", reason=reason[:160], **extra)
            return {"ok": False, "state": "failed", "error": reason,
                    "reason": reason, "attempt": attempt}

        if not agent:
            return fail("task has no owner agent")
        if LLM_PERMISSION not in set(agent.get("permissions") or ()):
            self._bump("refused")
            reason = "agent %r lacks permission %r" % (agent.get("id"), LLM_PERMISSION)
            result = fail(reason)
            if result.get("state") == "failed":
                event("task_refused", reason=reason)
            return result
        if not self.roster.owns_attempt(task_id, attempt, "running"):
            return lost()
        event("task_running")
        self._bump("executions")
        budgets = self.budgets(agent, task)
        started = time.monotonic()
        runner = self._runner or provider_runner
        calls = 0
        while self.roster.owns_attempt(task_id, attempt, "running"):
            elapsed = time.monotonic() - started
            if elapsed > budgets["max_seconds"]:
                return fail("wall-clock budget exceeded after %.0fs" % elapsed)
            if calls >= budgets["max_model_calls"]:
                return fail("model-call budget exceeded (%d)" % budgets["max_model_calls"])
            call_started = time.monotonic()
            candidates = [] if self._runner is None else None
            options = {}
            if candidates is not None:
                options = {"identity": self.identity, "observations": candidates,
                           "may_dispatch": lambda: self.roster.owns_attempt(task_id, attempt, "running")}
            try:
                result = runner(task.get("goal") or "", system=system,
                                timeout=timeout, agent=agent, **options)
            except Exception as error:
                result = {"ok": False, "error": type(error).__name__}
            if not isinstance(result, dict):
                result = {"ok": False, "error": "invalid provider response"}
            calls += 1
            self._bump("model_calls")
            latency = round(time.monotonic() - call_started, 3)
            provider = str(result.get("provider") or "")
            model = str(result.get("model") or "")
            if not self.roster.owns_attempt(task_id, attempt, "running"):
                return lost()
            if candidates is not None:
                recorded = self.roster.record_execution(task_id, attempt, calls, candidates)
                if not recorded.get("ok"):
                    return fail(recorded.get("reason") or "could not persist execution evidence")
            if not result.get("ok"):
                reason = str(result.get("error") or "provider error")
                if calls < budgets["max_steps"] and calls < budgets["max_model_calls"]:
                    event("task_retry", attempt="%s:call:%d" % (attempt, calls),
                          task_attempt=attempt, error=reason[:160], provider=provider, model=model)
                    continue
                if calls >= budgets["max_model_calls"] and calls < budgets["max_steps"]:
                    return fail("model-call budget exceeded (%d): %s" % (calls, reason),
                                provider=provider, model=model)
                return fail(reason, provider=provider, model=model)
            text = str(result.get("text") or "").strip()
            if not text:
                return fail("provider returned an empty result", provider=provider, model=model)
            try:
                from .. import lab_artifacts
                writer = lab_artifacts.for_store(self.roster.store) if self.roster.store is not None else None
            except Exception as error:
                return fail("artifact write failed: " + type(error).__name__, provider=provider, model=model)
            completed = self.roster.complete(task_id, artifact=text, attempt=attempt, writer=writer,
                                             provider=provider, model=model, steps=calls,
                                             execution_call=calls if candidates else None)
            if not completed.get("ok"):
                return fail(completed.get("reason") or "could not persist result",
                            provider=provider, model=model)
            if not completed.get("dedup"):
                self._bump("completed")
                with self._lock:
                    self.stats.update(last_provider=provider, last_model=model, last_latency=latency)
                event("task_completed", provider=provider, model=model, latency=latency,
                      model_calls=calls, steps=calls, tokens_in=result.get("tokens_in"),
                      tokens_out=result.get("tokens_out"), cached=bool(result.get("cached")),
                      artifact_chars=len(text), artifact_id=completed.get("artifact_id"))
            return {"ok": True, "state": completed["state"], "attempt": attempt,
                    "artifact_id": completed.get("artifact_id"), "provider": provider,
                    "model": model, "latency": latency, "model_calls": calls,
                    "steps": calls, "text": text}
        return lost()

    def _bump(self, key):
        with self._lock:
            if key in self.stats:
                self.stats[key] += 1


# ---------------------------------------------------------------------------
# The default runner: one bounded completion over FRIDAY's configured chain.
# ---------------------------------------------------------------------------
def provider_runner(goal, *, system=SYSTEM_DEFAULT, timeout=DEFAULT_TIMEOUT_SECONDS,
                    agent=None, identity=None, observations=None, may_dispatch=None, **_ignored):
    """One completion through the configured chain. Returns a dict, never raises.

    Candidates come from `brain_router.resolve()` (read-only) and keys from
    `api_pools`, so cooldowns, pacing and the model-ban ledger are shared with
    normal turns. Keys are used in-process only - never returned or logged.
    """
    from .. import api_pools
    from .. import brain_router
    from ..delegation import redact_secrets, MAX_RESULT_CHARS
    from ..identity_integration import provider_for_pool

    started = time.monotonic()
    candidates = list(brain_router.resolve(str(goal or "")))
    last_error = "no configured provider candidate"
    for pool_name, model, api_base in candidates:
        observed = None
        try:
            # User-disable (ROUND LAB-2B): an explicitly disabled cast
            # character means its brain pool is SKIPPED for worker lanes.
            # This is an enforcement point the projection wrote, not a
            # second router: the chain and its ledger are untouched, and the
            # skip is visible in the task's honest failure reason.
            try:
                from .. import lab_authority as _lab_auth
                if _lab_auth.pool_user_disabled(pool_name, str(model or "")):
                    last_error = ("%s is user-disabled by the user; skipped"
                                  % pool_name)
                    continue
            except Exception:
                pass
            if pool_name == "local":
                model = model or "qwen3:8b"
                api_base = api_base or "http://127.0.0.1:11434/v1"
            if not api_base:
                continue
            pool = api_pools.pool(pool_name)
            if pool is None:
                continue
            key = pool.next_key()               # honours cooldown + pacing
            if key is None:
                last_error = "quota exhausted on %s" % pool_name
                continue
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": str(goal or "")}],
                "stream": False,
                "max_tokens": MAX_TOKENS,
                "temperature": 0.2,
            }
            if observations is not None:
                observed = {"pool": pool_name, "provider": provider_for_pool(pool_name),
                            "model": model, "capability": "brain",
                            "resolution": "unresolved", "outcome": "failed"}
                if identity is not None:
                    try:
                        observed.update(identity.execution_binding("brain", pool_name, model, api_base))
                    except Exception:
                        pass  # Catalog availability never changes worker routing.
            if may_dispatch is not None and not may_dispatch():
                break
            if observed is not None:
                observations.append(observed)
            response = _post_chat(api_base, key, payload, timeout)
            status, body, *transport = response
            if observed is not None:
                if type(status) is int:
                    observed["http_status"] = status
                if transport and transport[0]:
                    # The configured route was selected, but its redirected
                    # response cannot verify who produced the output.
                    for field in ("binding_id", "character_id", "catalog_revision"):
                        observed.pop(field, None)
                    observed.update(resolution="unresolved", redirected=True)
            if status == 200:
                text = _extract_text(body)
                if text.strip():
                    api_pools.model_succeed(pool_name, model)
                    tokens = _extract_usage(body)
                    text = redact_secrets(text)[:MAX_RESULT_CHARS]
                    if observed is not None:
                        observed["outcome"] = "returned_result"
                    return {"ok": True, "text": text,
                            "provider": pool_name, "model": model,
                            "tokens_in": tokens.get("tokens_in"),
                            "tokens_out": tokens.get("tokens_out"),
                            "cached": tokens.get("cached"),
                            "error": "",
                            "latency": round(time.monotonic() - started, 3)}
                last_error = "%s returned no content" % pool_name
            elif status in (401, 402, 403, 429):
                # Share the quota ledger with normal turns instead of burning
                # the same key again on the next attempt.
                api_pools.fail_model(pool_name, model)
                last_error = "%s HTTP %s" % (pool_name, status)
            else:
                last_error = "%s HTTP %s" % (pool_name, status)
        except Exception as error:
            last_error = type(error).__name__
    return {"ok": False, "text": "", "provider": "", "model": "",
            "error": str(last_error)[:300],
            "latency": round(time.monotonic() - started, 3)}


def _post_chat(api_base, key, payload, timeout):
    """POST a chat/completions body and note whether the response redirected."""
    import requests as _requests
    url = api_base.rstrip("/") + "/chat/completions"
    response = _requests.post(
        url,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % key},
        json=payload,
        timeout=max(5.0, float(timeout)),
    )
    try:
        return int(response.status_code), response.json(), bool(response.history)
    except Exception:
        return int(response.status_code), None, bool(response.history)


def _extract_text(body):
    try:
        content = body["choices"][0]["message"]["content"]
        return content if isinstance(content, str) else ""
    except Exception:
        return ""


def _extract_usage(body):
    """REAL usage counters from an OpenAI-compatible completion body.

    `tokens_in/out` are the provider's own accounting when provided; they are
    None (never 0) when absent, so the budget can know the difference between
    'no counter available' and 'zero tokens actually used'. `cached` reports
    the provider's cached-tokens detail only when the schema exposes it.
    """
    try:
        usage = (body or {}).get("usage") or {}
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens")
        cached = False
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            cached = bool(int(details.get("cached_tokens") or 0))
        return {"tokens_in": int(tokens_in) if tokens_in is not None else None,
                "tokens_out": int(tokens_out) if tokens_out is not None
                else None,
                "cached": cached}
    except Exception:
        return {"tokens_in": None, "tokens_out": None, "cached": False}
