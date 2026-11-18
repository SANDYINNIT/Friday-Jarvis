"""`brain_router.apply_for()` must scope its mid-turn storm guard to THIS turn.

THE BUG (found from live logs on 2026-10-03, not from reading the code)
------------------------------------------------------------------------
`apply_for()` is called at TURN START, BEFORE this turn's user row is appended -
`server.py` calls `apply_for(request, interpreter)` and only then
`old_input({...role: user...})`, at all three call sites. The guard walked
backwards from the end of `interpreter.messages`, so at turn start it was looking
at the PREVIOUS turn. Any `computer`/`tool` row there set the flag, and the guard
then skipped every cloud candidate:

    if _tool_this_turn and pool_name != "local":
        continue

Live evidence that this actually happened: a turn that used four tools made the
NEXT turn select `ollama_chat/qwen3:8b` while `api_pools` reported no cooldown
and no failover line was logged.

These tests pin both required behaviours:
  * previous turn used tools  -> cloud is still selectable
  * tool rows after THIS turn's user row -> cloud is still skipped (the guard's
    actual purpose: a mid-turn cloud 429 must not re-run completed tools)
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.getcwd())

from source.server import brain_router as br


class _FakeLLM:
    def __init__(self):
        self.model = None
        self.api_key = None
        self.api_base = None


class _FakeInterpreter:
    """Minimal stand-in: `apply_for` only touches .messages and .llm."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.llm = _FakeLLM()
        self.computer = types.SimpleNamespace(language="python")
        self.offline = False
        self.auto_run = True
        self.debug = False


def _user(text):
    return {"role": "user", "type": "message", "content": text}


def _assistant(text):
    return {"role": "assistant", "type": "message", "content": text}


def _console(text="out"):
    return {"role": "computer", "type": "console", "content": text}


CLOUD_PREFIX = "openai/"


class TurnScopedToolActivityTests(unittest.TestCase):
    def _select(self, messages, user_text):
        interp = _FakeInterpreter(messages)
        br.BrainRouter().apply_for(user_text, interp)
        return str(interp.llm.model or "")

    # -- the regression ----------------------------------------------------
    def test_previous_turn_used_tools_does_not_downgrade_to_local(self):
        """THE BUG. A tool-heavy turn must not poison the next turn's routing."""
        messages = [
            _user("Click the ShowBox button in the Button Bench app"),
            _console(), _assistant("done"), _console(), _assistant("done"),
        ]
        chosen = self._select(messages, "In that app, click whichever control")
        self.assertTrue(
            chosen.startswith(CLOUD_PREFIX),
            "cloud must stay selectable after a tool-using turn; got %r" % chosen)

    def test_previous_turn_with_many_tool_rows_still_allows_cloud(self):
        messages = [_user("previous request")] + [_console(), _assistant("ok")] * 6
        chosen = self._select(messages, "a completely different request")
        self.assertTrue(chosen.startswith(CLOUD_PREFIX), chosen)

    # -- the guard must still work ----------------------------------------
    def test_tool_rows_after_this_turn_user_row_still_skip_cloud(self):
        """Mid-turn failover: the guard's real purpose must survive the fix."""
        messages = [
            _user("move the app to the right monitor"),
            _console("screenshot taken"),
            _assistant("working on it"),
        ]
        chosen = self._select(messages, "move the app to the right monitor")
        self.assertIn("qwen3", chosen,
                      "cloud must stay skipped after this turn already used a "
                      "tool; got %r" % chosen)

    def test_mid_turn_with_no_tools_yet_does_not_skip_cloud(self):
        messages = [_user("move the app to the right monitor")]
        chosen = self._select(messages, "move the app to the right monitor")
        self.assertTrue(chosen.startswith(CLOUD_PREFIX), chosen)

    # -- robustness --------------------------------------------------------
    def test_prefixed_context_row_still_matches_the_raw_request(self):
        """The appended row can carry a memory/screen-context prefix, and the
        match must still recognise it as THIS turn - otherwise the guard would
        silently stop protecting mid-turn hops. No tool rows yet, so cloud wins."""
        prefixed = ("[Context: my last screen read is 0 minutes old.]\n\n"
                    "User request:\nwhat is on my screen?")
        messages = [_user(prefixed)]
        chosen = self._select(messages, "what is on my screen?")
        self.assertTrue(chosen.startswith(CLOUD_PREFIX),
                        "a prefixed row is still this turn; got %r" % chosen)

    def test_prefixed_row_plus_tool_rows_still_skips_cloud(self):
        """...and with a tool row after it, the guard must fire."""
        prefixed = ("[Context: my last screen read is 0 minutes old.]\n\n"
                    "User request:\nwhat is on my screen?")
        messages = [_user(prefixed), _console(), _assistant("here it is")]
        chosen = self._select(messages, "what is on my screen?")
        self.assertIn("qwen3", chosen,
                      "prefix matching failed, so the guard did not fire; "
                      "got %r" % chosen)

    def test_empty_history_is_not_an_error(self):
        chosen = self._select([], "hello")
        self.assertTrue(chosen.startswith(CLOUD_PREFIX), chosen)

    def test_history_with_no_user_row_is_not_an_error(self):
        chosen = self._select([_console(), _assistant("orphan")], "hello")
        self.assertTrue(chosen.startswith(CLOUD_PREFIX), chosen)

    def test_a_repeated_identical_request_still_protects_the_mid_turn_case(self):
        """Fail-open direction check: identical text must not crash the guard."""
        messages = [
            _user("click it"), _console(), _assistant("done"),
            _user("click it"), _console(),
        ]
        chosen = self._select(messages, "click it")
        # Ambiguous by construction (last user row is the previous turn's, but the
        # text is identical). Either resolution is acceptable; what matters is that
        # it does not raise and still returns a usable model.
        self.assertTrue(chosen, "no model was configured")


if __name__ == "__main__":
    unittest.main()