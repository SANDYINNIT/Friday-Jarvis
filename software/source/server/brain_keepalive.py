"""Keep the local brain RESIDENT so turns are not charged a model load.

WHY THIS EXISTS (measured 2026-09-30)
-------------------------------------
On this machine a two-token reply to `qwen3:8b` took:

    first call (cold)   49.3 s
    second call (warm)   2.2 s
    third  call (warm)   2.2 s
    40-token generation  2.9 s

So the local brain is FAST. What was slow was Ollama UNLOADING it: the
server default `keep_alive` is 5 minutes, FRIDAY is idle between turns, and
every question after a pause paid a ~49s model load. That single hidden cost
explains a long list of symptoms that looked like "the AI is dumb" or "every
model is unavailable":

  * the photo caption came back EMPTY (its 30s budget was consumed by a load),
    which made the caller emit a canned acknowledgement;
  * the vision read appeared unreliable;
  * turns reported "every thinking model I have is unavailable right now";
  * wall-clock guards fired on work that was merely loading.

`main.warmup_services()` deliberately does NOT warm the brain, to keep boot
fast. That is still correct - warming at boot would slow startup. This module
solves the same problem without touching boot: a background thread pings the
active brain on an interval comfortably shorter than the unload timeout, with a
long `keep_alive`, so the model is always resident when Sir actually speaks.

The ping is tiny (a handful of prompt tokens, `num_predict: 1`) and runs on a
daemon thread, so it can never block or delay a turn. Set
`FRIDAY_BRAIN_KEEPALIVE=0` to disable.

Reference: Ollama FAQ / envconfig - `OLLAMA_KEEP_ALIVE` defaults to 5 minutes;
a negative value keeps a model loaded indefinitely.
"""

from __future__ import annotations

import os
import threading
import time

# How often to touch the model. Must be comfortably below Ollama's 5m default
# so the model is never actually unloaded between pings.
DEFAULT_INTERVAL_SECONDS = float(os.getenv("FRIDAY_BRAIN_KEEPALIVE_INTERVAL", "150"))
# How long Ollama should hold the model after each ping.
KEEP_ALIVE = os.getenv("FRIDAY_BRAIN_KEEPALIVE_TTL", "30m")
# Bound the first ping's cost: a load is slow, so give it room, but never hang.
PING_TIMEOUT_SECONDS = float(os.getenv("FRIDAY_BRAIN_KEEPALIVE_TIMEOUT", "120"))

PING_PROMPT = "ok"

_state = {"thread": None, "stop": threading.Event(), "model": None, "pings": 0,
          "last_ok": None, "last_error": None}


def enabled() -> bool:
    return os.getenv("FRIDAY_BRAIN_KEEPALIVE", "1").strip().lower() not in (
        "0", "false", "no", "off", ""
    )


def _base_url() -> str:
    return os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434").rstrip("/")


def _api_chat() -> str:
    return os.environ.get("OLLAMA_API_CHAT", f"{_base_url()}/api/chat")


def _model_name(model) -> str:
    """Accept a bare tag or a litellm-style `ollama_chat/<tag>` string."""
    text = str(model or "").strip()
    if not text:
        return ""
    for prefix in ("ollama_chat/", "ollama/"):
        if text.startswith(prefix):
            return text[len(prefix):]
    if "/" in text and not text.startswith(("qwen", "llama", "gemma", "mistral",
                                            "phi", "deepseek", "moondream",
                                            "granite", "codellama", "nomic")):
        return text.split("/", 1)[1]
    return text


def ping_once(model=None, timeout=None) -> bool:
    """One keep-alive touch. Never raises; returns True when it succeeded."""
    tag = _model_name(model or _state.get("model") or
                      os.environ.get("FRIDAY_LOCAL_BRAIN_MODEL", "qwen3:8b"))
    if not tag:
        return False
    try:
        import requests

        response = requests.post(
            _api_chat(),
            json={
                "model": tag,
                "think": False,
                "stream": False,
                "keep_alive": KEEP_ALIVE,
                "messages": [{"role": "user", "content": PING_PROMPT}],
                "options": {"temperature": 0, "num_predict": 1},
            },
            timeout=timeout or PING_TIMEOUT_SECONDS,
        )
        ok = getattr(response, "status_code", 500) < 400
        _state["pings"] += 1
        _state["last_ok"] = time.time()
        _state["last_error"] = None if ok else f"HTTP {response.status_code}"
        return ok
    except Exception as error:
        # A failed ping is not an error worth surfacing - the model simply
        # unloads and the next real turn pays the load again.
        _state["last_error"] = f"{type(error).__name__}: {error}"[:200]
        return False


def start(model=None, interval=None):
    """Start the background keep-alive thread. Safe to call repeatedly."""
    if not enabled():
        return None
    tag = _model_name(model or os.environ.get("FRIDAY_LOCAL_BRAIN_MODEL", "qwen3:8b"))
    _state["model"] = tag
    existing = _state.get("thread")
    if existing is not None and existing.is_alive():
        return existing
    _state["stop"] = threading.Event()
    seconds = interval or DEFAULT_INTERVAL_SECONDS
    if seconds <= 0:
        return None

    def _loop():
        # Ping promptly on boot so the FIRST Sir request after a restart is warm,
        # then settle into the interval.
        while not _state["stop"].is_set():
            ping_once(tag)
            if _state["stop"].wait(seconds):
                break

    thread = threading.Thread(target=_loop, daemon=True, name="friday-brain-keepalive")
    _state["thread"] = thread
    thread.start()
    return thread


def stop():
    _state["stop"].set()
    thread = _state.get("thread")
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    _state["thread"] = None


def status() -> dict:
    """Small self-report; safe to call from anywhere, never raises."""
    thread = _state.get("thread")
    return {
        "enabled": enabled(),
        "running": bool(thread is not None and thread.is_alive()),
        "model": _state.get("model"),
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "keep_alive": KEEP_ALIVE,
        "pings": _state.get("pings", 0),
        "last_ok": _state.get("last_ok"),
        "last_error": _state.get("last_error"),
        "why": (
            "Ollama unloads models after 5m by default; a cold qwen3:8b reply "
            "measured 49.3s here versus 2.2s warm."
        ),
    }
