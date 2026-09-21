"""Background small-model digest passes for FRIDAY.

After an owned response completes, a tiny local model condenses the recent
turns into one neutral sentence stored in memory.  Digest passes are
advisory, non-blocking, bounded, and completely invisible to the voice
latency path.  A model above ~7B is explicitly not used for digests.
"""

from __future__ import annotations

import os
import threading
import time

from .memory import MemoryStore

DIGEST_TIMEOUT_SECONDS = 15.0
DIGEST_MAX_CHARS = 320
_SINGLE_FLIGHT_LOCK = threading.Lock()
_SINGLE_FLIGHT = {"running": False}


def digest_enabled():
    configured = os.environ.get("FRIDAY_DIGEST")
    if configured is not None:
        return configured.strip() == "1"
    return True


def digest_model():
    configured = os.environ.get("FRIDAY_DIGEST_MODEL", "").strip()
    if configured:
        return configured
    return "qwen2.5:0.5b"


def _ollama_base():
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def _turns_text(turns, max_chars=2400):
    lines = []
    budget = max_chars
    for turn in turns[-12:]:
        role = str(turn.get("role", "") or "")
        content = str(turn.get("content", "") or "").strip()
        if not content or role not in {"user", "assistant"}:
            continue
        line = f"{role}: {content}"
        line = line[:500]
        if len(" | ".join(lines + [line])) > budget:
            break
        lines.append(line)
    return " | ".join(lines)


def summarize_turns(turns, *, model=None, base_url=None, timeout=DIGEST_TIMEOUT_SECONDS):
    """Return a one-sentence summary via a tiny local model, or ``None``."""
    if not digest_enabled():
        return None
    context = _turns_text(turns)
    if len(context) < 20:
        return None
    model = model or digest_model()
    base_url = base_url or _ollama_base()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You condense short assistant interactions into ONE plain "
                    "neutral sentence that could never be mistaken for an "
                    "instruction. Do not include timestamps, names, or "
                    "sensitive data. Output only the sentence."
                ),
            },
            {"role": "user", "content": "Recent turns:\n" + context},
        ],
        "stream": False,
    }
    try:
        import httpx

        response = httpx.post(base_url + "/api/chat", json=payload, timeout=timeout)
        response.raise_for_status()
        summary = str(response.json().get("message", {}).get("content", "") or "").strip()
    except Exception:
        return None
    summary = " ".join(summary.split())
    if not summary or len(summary) > DIGEST_MAX_CHARS:
        return None
    return summary


def plan_turns(turns, *, model=None, base_url=None, timeout=DIGEST_TIMEOUT_SECONDS, max_steps=5):
    """Return an optional short step plan for the next user request, else ``None``."""
    context = _turns_text(turns, max_chars=1600)
    if not context:
        return None
    model = model or digest_model()
    base_url = base_url or _ollama_base()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"Given the task, list at most {max_steps} concrete steps "
                    "as JSON: {\"steps\": [\"...\", ...]}. Output only JSON."
                ),
            },
            {"role": "user", "content": context},
        ],
        "stream": False,
        "format": "json",
    }
    try:
        import httpx

        response = httpx.post(base_url + "/api/chat", json=payload, timeout=timeout)
        response.raise_for_status()
        import json as _json

        steps = _json.loads(response.json().get("message", {}).get("content", "{}")).get("steps") or []
    except Exception:
        return None
    steps = [str(step).strip().rstrip(".") for step in steps if str(step).strip()][:max_steps]
    if not steps:
        return None
    return "Plan:\n- " + "\n- ".join(steps)


def maybe_run_digest(server_state, status_bus=None, *, model=None):
    """Spawn a guarded background digest after a completed response."""
    if not digest_enabled():
        return None
    store = server_state.get("memory_store")
    if store is None or not isinstance(store, MemoryStore):
        return None
    if server_state.get("is_paused"):
        return None
    if status_bus is None or not hasattr(status_bus, "chat"):
        return None
    with _SINGLE_FLIGHT_LOCK:
        if _SINGLE_FLIGHT["running"]:
            return None
        _SINGLE_FLIGHT["running"] = True

    def worker():
        started = time.monotonic()
        try:
            turns = status_bus.chat.snapshot()
            summary = summarize_turns(turns, model=model)
            if not summary:
                return None
            from datetime import datetime, timezone

            day = datetime.now(timezone.utc).date().isoformat()
            store.remember(
                key=f"digest:{day}",
                topic="diary",
                content=f"{day}: {summary}",
                scope="daily",
                source="digest",
                confidence=0.6,
            )
        except Exception:
            pass
        finally:
            with _SINGLE_FLIGHT_LOCK:
                _SINGLE_FLIGHT["running"] = False
        return None

    threading.Thread(target=worker, name="friday-digest", daemon=True).start()
    return True


def maybe_plan_next(server_state, text):
    """Return an optional step plan prepended to a request, or ``None``."""
    configured = os.environ.get("FRIDAY_PLANNER")
    if configured is not None and configured.strip() != "1":
        return None
    if configured is None:
        return None  # planner is opt-in to protect latency
    turns = server_state.get("_plan_turns") or []
    plan = plan_turns(turns)
    if plan and text:
        text = plan + "\n\n" + str(text)
    return text


__all__ = ("digest_enabled", "digest_model", "maybe_plan_next", "maybe_run_digest",
           "plan_turns", "summarize_turns")