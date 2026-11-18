"""Regression tests for per-turn loop handling in _begin/_end_single_turn.

UPDATED 2026-09-30. This test previously asserted that _begin_single_turn turns
`interpreter.loop` OFF and _end_single_turn restores it. That is no longer the
contract: the implementation deliberately PRESERVES the configured loop value
(Open Interpreter's loop engine stays ON) and relies on server.py's per-turn
llm-call guard (`traced_completions`, FRIDAY_LLM_LOOP_GUARD) to force-break any
text-only repeat loop instead. Disabling the loop engine outright would also
break legitimate multi-step tool chaining within a turn.

These are real assertions against the real function, using an instance
attribute (not a class attribute, which could never be shadowed).
"""

import os
import sys

sys.path.insert(0, os.getcwd())

from source.server.server import _begin_single_turn, _end_single_turn


class FakeInterpreter:
    def __init__(self, loop=True):
        self.loop = loop


def test_begin_preserves_the_configured_loop_value():
    """The loop engine is deliberately left as configured - not forced off."""
    interpreter = FakeInterpreter(loop=True)
    state = {"single_turn_loop": None}

    _begin_single_turn(interpreter, state)

    assert interpreter.loop is True, "loop must be preserved, not disabled"
    assert state["single_turn_loop"] is True, "the original value must be saved"


def test_begin_preserves_a_disabled_loop_too():
    """If the profile disabled the loop, begin/end must not silently re-enable it."""
    interpreter = FakeInterpreter(loop=False)
    state = {"single_turn_loop": None}

    _begin_single_turn(interpreter, state)
    assert interpreter.loop is False
    _end_single_turn(interpreter, state)
    assert interpreter.loop is False, "a deliberately disabled loop stays disabled"


def test_end_restores_state_to_unset():
    interpreter = FakeInterpreter(loop=True)
    state = {"single_turn_loop": None}

    _begin_single_turn(interpreter, state)
    assert state["single_turn_loop"] is not None
    _end_single_turn(interpreter, state)
    assert state["single_turn_loop"] is None, "state must be released after the turn"


def test_begin_arms_the_turn_log():
    """begin also starts a fresh turn log, which is what the failover hop relies on."""
    interpreter = FakeInterpreter()
    state = {"single_turn_loop": None}

    _begin_single_turn(interpreter, state, user="hello", source="text")

    turn = state.get("turn_log")
    assert turn is not None, "turn log must be armed for every turn"
    assert turn["assistant_snippet"] == ""
    assert turn["user"] == "hello"


def test_begin_is_idempotent_within_a_turn():
    """A second begin inside the same turn must not clobber the saved value."""
    interpreter = FakeInterpreter(loop=True)
    state = {"single_turn_loop": None}

    _begin_single_turn(interpreter, state, user="first", source="text")
    _begin_single_turn(interpreter, state, user="second", source="text")

    assert state["single_turn_loop"] is True
    assert interpreter.loop is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS: {name}")
    print("PASS: per-turn loop handling (preserves the configured loop value)")
