"""Render the system prompt ONCE per turn, not once per LLM call.

WHY THIS EXISTS
---------------
Open Interpreter's `respond()` loop calls `render_message(interpreter,
system_message)` at the TOP OF EVERY ITERATION - i.e. before every LLM call.
`render_message` executes each `{{...}}` block in the system prompt as real
Python through the Jupyter kernel, and FRIDAY's profile has 9 of them
(skills, desktop overview, UI probe, screen health, self-knowledge, scratchpad,
outcomes journal, lessons, scroll help).

MEASURED on this machine (2026-11-01): those 9 blocks cost **5.55s** to render,
and a tool-calling turn makes 3 LLM calls - so ~16.5s of the observed 47s turn
was re-rendering a prompt whose template had not changed. Live evidence:
`22:04:01 brain select` -> `22:04:10 llm_request_start` (9s), and
`22:04:23 tool console` -> `22:04:31 llm_request_start` (8s).

WHY IT IS SAFE
--------------
The template is constant for the duration of a turn, and tool results reach the
model as messages (`role=console` / `role=tool`), not through the system prompt
- so caching the rendered text does not hide them. What CAN go stale inside a
single turn is content the scratchpad blocks read from disk; that is an
acceptable trade for removing ~16s of silence, and it is reversible:

    FRIDAY_PROMPT_RENDER_CACHE=0

The cache is keyed on (turn epoch, template). A changed template always
re-renders, and `begin_turn()` starts a fresh epoch, so nothing is ever reused
across turns.
"""

from __future__ import annotations

import os
import threading
import time

_state = {
    "installed": False,
    "epoch": 0,
    "key": None,
    "value": None,
    "owner": None,
    "hits": 0,
    "misses": 0,
    "seconds_saved_estimate": 0.0,
    "last_render_seconds": 0.0,
    # ROUND ZB-LAT: bounded history of REAL renders, exported on /voice/trace
    # as `prompt_render`. This stage has no trace event of its own - it happens
    # inside Open Interpreter's respond loop, between `brain select` and
    # `llm_request_start` - so without this the ~7s it costs every turn was
    # invisible: it showed up only as a silent gap in friday.log. `at` is wall
    # clock so it lines up with the log's own stamps.
    "recent": [],
}
_RECENT_MAX = 12
_lock = threading.Lock()
_original = None


def enabled():
    """Opt out with FRIDAY_PROMPT_RENDER_CACHE=0."""
    return os.environ.get("FRIDAY_PROMPT_RENDER_CACHE", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def begin_turn():
    """Start a new cache epoch. Called at every turn boundary."""
    with _lock:
        _state["epoch"] += 1
        _state["key"] = None
        _state["value"] = None
        _state["owner"] = None


def stats():
    with _lock:
        return {key: value for key, value in _state.items() if key not in ("key", "value", "owner")}


def _user_turn_marker(interpreter):
    """How many user messages OI is holding.

    Used as a self-invalidating half of the cache key: a new turn always adds
    a user message, while tool/console messages inside a turn do not. That way
    the cache is correct even on an entry point that forgets to call
    `begin_turn()` - it can never serve a previous turn's prompt.
    """
    try:
        messages = getattr(interpreter, "messages", None) or []
        return sum(1 for item in messages
                   if isinstance(item, dict) and item.get("role") == "user")
    except Exception:
        return 0


def install():
    """Wrap `render_message` in Open Interpreter's respond module.

    `respond.py` binds `render_message` as a module global and calls it inside
    its loop, so replacing the module attribute is what intercepts it (patching
    `interpreter.core.render_message` would NOT - that name is already bound in
    `respond`'s namespace). Idempotent; returns True when installed or already
    installed, False when skipped/unavailable.
    """
    global _original
    if not enabled():
        if _state['installed'] or _original is not None:
            uninstall()
        return False
    try:
        from interpreter.core import respond as _oi_respond
    except Exception:
        return False
    with _lock:
        if _state["installed"]:
            return True
        original = getattr(_oi_respond, "render_message", None)
        if original is None:
            return False
        if getattr(original, "_friday_render_cache", False):
            # Someone already wrapped it (a re-install after a failed reset).
            _state["installed"] = True
            return True
        _original = original

        def _cached(interpreter, message):
            with _lock:
                key = (_state["epoch"], _user_turn_marker(interpreter), message)
                hit = _state["owner"] is interpreter and _state["key"] == key and _state["value"] is not None
                if hit:
                    _state["hits"] += 1
                    if _state["last_render_seconds"]:
                        _state["seconds_saved_estimate"] += _state["last_render_seconds"]
                    return _state["value"]
            # Never hold the state lock across a kernel round trip: Stop/new
            # admission must be able to invalidate the old producer immediately.
            started = time.monotonic()
            template = getattr(interpreter, "system_message", None)
            # Normal OI appends language/custom/computer instructions before
            # rendering. Literal suffixes do not add executable profile blocks.
            # Extra dynamic blocks keep the original ordered renderer; batching
            # remains scoped to the explicitly opted-in trusted base profile.
            batchable = (isinstance(template, str) and bool(template)
                         and isinstance(message, str)
                         and (message == template or message.startswith(template + "\n\n"))
                         and "{{" not in message[len(template):]
                         and "}}" not in message[len(template):])
            if getattr(interpreter, "friday_batch_prompt", False) and batchable:
                from .prompt_batch import render
                value = render(interpreter, message)
            else:
                value = original(interpreter, message)
            elapsed = time.monotonic() - started
            with _lock:
                if (_state['installed'] and _oi_respond.render_message is _cached
                        and _state['epoch']==key[0]
                        and _user_turn_marker(interpreter)==key[1]):
                    _state["last_render_seconds"] = elapsed
                    _state["misses"] += 1
                    _state["key"] = key
                    _state["value"] = value
                    _state["owner"] = interpreter
                    _state["recent"].append({"epoch":key[0],"seconds":round(elapsed,3),"at":round(time.time(),3)})
                    del _state["recent"][:-_RECENT_MAX]
            return value

        _cached.__name__ = "render_message"
        _cached._friday_render_cache = True
        _oi_respond.render_message = _cached
        _state["installed"] = True
        return True


def uninstall():
    """Restore the original render_message. Used by tests and by opt-out."""
    global _original
    with _lock:
        try:
            from interpreter.core import respond as _oi_respond
        except Exception:
            _state["installed"] = False
            return False
        current = getattr(_oi_respond, "render_message", None)
        if getattr(current, "_friday_render_cache", False) and _original is not None:
            _oi_respond.render_message = _original
        _original = None
        _state["installed"] = False
        _state["key"] = None
        _state["value"] = None
        _state["owner"] = None
        return True
