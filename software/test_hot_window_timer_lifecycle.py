"""Execute the actual window closures with controlled timers, including stale callbacks."""
import ast
import threading
import types
from pathlib import Path

import pytest


def window():
    source = (Path(__file__).parent / "source/server/server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    clock = types.SimpleNamespace(now=100.0)
    timers = []
    events = []

    class Timer:
        def __init__(self, seconds, fn, args=(), kwargs=None):
            self.fn, self.args, self.kwargs = fn, args, kwargs or {}
            self.canceled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.canceled = True

        def fire(self):
            # Deliberately fire even if canceled: cancel() cannot stop a callback
            # that was already running and waiting for the lock.
            self.fn(*self.args, **self.kwargs)

    state = {"hot_window_lock": threading.Lock(), "hot_window_active": False,
             "hot_window_timer": None, "hot_window_seconds": 30.0,
             "hot_window_expires_at": 0.0, "is_paused": False}
    ns = {"server_state": state, "time": types.SimpleNamespace(monotonic=lambda: clock.now),
          "threading": types.SimpleNamespace(Timer=Timer, current_thread=threading.current_thread),
          "trace": events.append, "publish_status": lambda *args: events.append(args),
          "STATUS_IDLE": "idle", "STATUS_LISTENING": "listening"}
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
             and n.name in ("open_hot_window", "close_hot_window")]
    assert len(nodes) == 2
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<production-window>", "exec"), ns)
    return ns, state, clock, timers, events


def test_refresh_cancels_old_timer_and_stale_callback_cannot_close_window():
    ns, state, clock, timers, events = window()
    assert ns["open_hot_window"]()
    clock.now = 110.0
    assert ns["open_hot_window"]()
    assert timers[0].canceled
    count = len(events)
    timers[0].fire()
    assert len(events) == count
    assert state["hot_window_active"]
    assert state["hot_window_expires_at"] == 140.0
    assert state["hot_window_timer"] is timers[1]
    timers[1].fire()
    assert not state["hot_window_active"]
    assert state["hot_window_timer"] is None


@pytest.mark.parametrize("reason", ["paused", "command", "shutdown"])
def test_explicit_close_still_works_and_stale_callback_cannot_close_reopened_window(reason):
    ns, state, clock, timers, _ = window()
    ns["open_hot_window"]()
    ns["close_hot_window"](reason)
    assert not state["hot_window_active"]
    assert timers[0].canceled
    clock.now = 115.0
    ns["open_hot_window"]()
    timers[0].fire()
    assert state["hot_window_active"]


def test_paused_cannot_open_and_duplicate_expiry_is_inert():
    ns, state, _, timers, events = window()
    state["is_paused"] = True
    assert ns["open_hot_window"]() is False
    assert not timers
    state["is_paused"] = False
    ns["open_hot_window"]()
    timers[0].fire()
    count = len(events)
    timers[0].fire()
    assert len(events) == count
