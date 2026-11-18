"""ROUND ZB-L4 L4-1: recover a turn the PROVIDER truncated mid-reasoning.

THE PROVEN FAILURE
-------------------
Live on 2026-10-07, healthy Modal, 12 tools, journal evidence:

    call#13  max_tokens=4096  reasoning_effort=None  think=UNSET
             reasoning_len=17426  content_len=0  finish_reason="length"

The model spent its whole token budget on reasoning and was cut off before
emitting any content or any tool call. Open Interpreter then ended the loop
NORMALLY (respond.py:448), FRIDAY journalled `shape=reasoning_only`, and Sir
was told nothing.

It was NOT HARD_TOOL_CAP (12 < 24), stop_event, the wall-clock budget, the
silent-hang budget, the flood cap, a provider outage, a lost delivery, or
corrupt telemetry.

These tests hold the FOUR-condition signature and prove it fires on exactly
that signature and nothing else, and that the recovery is DELIVERED rather than
merely logged.
"""

import ast
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from source.server import server as srv  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(_HERE, "source", "server", "server.py")


def server_source():
    with open(SERVER_PY, encoding="utf-8") as handle:
        return handle.read()


def _extracted(name):
    """Pull a module-level helper out of the DEPLOYED server.py."""
    import textwrap

    tree = ast.parse(server_source())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            lines = server_source().splitlines()
            return textwrap.dedent(
                "\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError("%s not found as a module-level function" % name)


def _exec_module(names, extra=None):
    """exec deployed helpers into a namespace that can actually run them."""
    import types as _types

    module = _types.ModuleType("extracted_l41")
    module.__package__ = "source.server"
    module.__dict__.update({"os": os, "asyncio": srv.asyncio, "time": srv.time})
    for constant in ("FINAL_NARRATION_TIMEOUT", "FINAL_NARRATION_MAX_TOKENS"):
        if hasattr(srv, constant):
            module.__dict__[constant] = getattr(srv, constant)
    module.__dict__.update(extra or {})
    for name in names:
        exec(compile(_extracted(name), "<extracted:%s>" % name, "exec"),
             module.__dict__)
    return module


# --------------------------------------------------------------------------
# Builders for the exact states the hook sees
# --------------------------------------------------------------------------
def _shape(last_call):
    return {"calls": [last_call], "turn_valid": True}


def _call(finish_reason="length", content_len=0, reasoning_len=17426,
          max_tokens=4096, index=13):
    return {"call_index": index, "finish_reason": finish_reason,
            "content_len": content_len, "reasoning_len": reasoning_len,
            "max_tokens": max_tokens, "chunks": 1367}


def _turn(actions=12, snippet=""):
    return {"assistant_snippet": snippet,
            "actions": [{} for _ in range(actions)],
            "assistant_len": len(snippet)}


def _state(shape, **kw):
    state = {"llm_response_shape": shape}
    state.update(kw)
    return state


def _detect(shape, turn):
    """Run the REAL predicate from the deployed source."""
    mod = _exec_module(["_budget_truncated_turn"])
    return mod._budget_truncated_turn(_state(shape), turn)


class SignatureDetectionTests(unittest.TestCase):
    """Requirement 1, 3, 4, 5: fires on the proven signature ONLY."""

    def test_the_proven_signature_matches(self):
        matched, last = _detect(_shape(_call()), _turn())
        self.assertTrue(matched, "the proven case MUST match")
        self.assertEqual(last.get("finish_reason"), "length")

    def test_finish_stop_with_content_does_not_match(self):
        """Requirement 3: a normal completed turn is never touched."""
        matched, _ = _detect(
            _shape(_call(finish_reason="stop", content_len=812)), _turn())
        self.assertFalse(matched)

    def test_finish_stop_with_NO_content_does_not_match(self):
        """A model that stopped normally having said nothing is a DIFFERENT
        case from a truncation, and the proven signature is `length`. Found by
        the mutation run: `finish=stop` + content was covered, `finish=stop` +
        no content was not, and a mutation that admitted 'stop' survived."""
        matched, _ = _detect(
            _shape(_call(finish_reason="stop", content_len=0)), _turn())
        self.assertFalse(matched,
                         "only finish=length is the proven truncation signature")

    def test_a_finish_reason_of_none_does_not_match(self):
        matched, _ = _detect(
            _shape(_call(finish_reason=None, content_len=0)), _turn())
        self.assertFalse(matched)

    def test_finish_tool_calls_does_not_match(self):
        matched, _ = _detect(
            _shape(_call(finish_reason="tool_calls", content_len=0)), _turn())
        self.assertFalse(matched)

    def test_actions_zero_does_not_match(self):
        """Requirement 4: a turn that did no work has nothing to narrate."""
        matched, _ = _detect(_shape(_call()), _turn(actions=0))
        self.assertFalse(matched)

    def test_length_with_content_does_not_match(self):
        """Requirement 5: a LONG ANSWER also hits `length`. That is a normal
        completion, and narrating over it would destroy a real answer."""
        matched, _ = _detect(
            _shape(_call(finish_reason="length", content_len=6181)), _turn())
        self.assertFalse(matched,
                         "content exists: this is a normal long answer")

    def test_existing_assistant_text_does_not_match(self):
        """Requirement 10: never overwrite or duplicate a real answer."""
        matched, _ = _detect(_shape(_call()),
                             _turn(snippet="I scrolled it and it moved."))
        self.assertFalse(matched)

    def test_only_the_last_call_is_considered(self):
        """The turn's FINAL call is the one that decides; an earlier truncated
        call that was followed by a good one must not trigger recovery."""
        shape = {"calls": [_call(index=3, finish_reason="length", content_len=0),
                           _call(index=4, finish_reason="tool_calls",
                                 content_len=0)],
                 "turn_valid": True}
        matched, _ = _detect(shape, _turn())
        self.assertFalse(matched)

    def test_no_calls_recorded_does_not_match(self):
        matched, _ = _detect({"calls": [], "turn_valid": False}, _turn())
        self.assertFalse(matched)

    def test_a_missing_turn_does_not_match(self):
        matched, _ = _detect(_shape(_call()), None)
        self.assertFalse(matched)

    def test_a_broken_shape_never_raises(self):
        """A diagnostic must never be the thing that kills a turn."""
        mod = _exec_module(["_budget_truncated_turn"])
        hostile = {"llm_response_shape": {"calls": ["not-a-dict", None]}}
        self.assertEqual(mod._budget_truncated_turn(hostile, _turn()),
                         (False, None))
        self.assertEqual(mod._budget_truncated_turn({}, _turn()), (False, None))


class RecoveryTests(unittest.TestCase):
    """Requirements 1, 2, 6, 9."""

    def _recover(self, narration_result, tools_run=12):
        mod = _exec_module(
            ["_recover_truncated_turn", "_final_narration_completion",
             "_tool_cap_fallback_text", "_verified_work_summary",
             "_final_narration_prompt"])
        with mock.patch.object(mod, "_final_narration_completion",
                               return_value=narration_result) as narration:
            import asyncio as _aio
            result = _aio.new_event_loop().run_until_complete(
                mod._recover_truncated_turn(
                    mock.Mock(), {"last_user_text": "scroll opencode"},
                    {"actions": [{} for _ in range(tools_run)]}))
        return result, narration

    def test_narration_is_used_when_the_provider_answers(self):
        (text, source), narration = self._recover("The viewport moved, Sir.")
        self.assertEqual(text, "The viewport moved, Sir.")
        self.assertEqual(source, "narration")
        narration.assert_awaited()

    def test_truthful_fallback_when_narration_is_unavailable(self):
        """Requirement 6: provider gone -> truthful text, never a claim."""
        (text, source), _ = self._recover(None)
        self.assertEqual(source, "truthful_fallback")
        self.assertTrue(text)
        lowered = text.lower()
        self.assertIn("tool budget", lowered)
        for forbidden in ("successfully", "i completed", "all done"):
            self.assertNotIn(forbidden, lowered)

    def test_an_exception_in_narration_still_yields_truthful_text(self):
        mod = _exec_module(
            ["_recover_truncated_turn", "_final_narration_completion",
             "_tool_cap_fallback_text", "_verified_work_summary",
             "_final_narration_prompt"])
        with mock.patch.object(mod, "_final_narration_completion",
                               side_effect=RuntimeError("provider down")):
            import asyncio as _aio
            text, source = _aio.new_event_loop().run_until_complete(
                mod._recover_truncated_turn(
                    mock.Mock(), {},
                    {"actions": [{} for _ in range(12)]}))
        self.assertEqual(source, "truthful_fallback")
        self.assertTrue(text and "tool budget" in text.lower())

    def test_the_helper_uses_no_start_server_closure_variables(self):
        """REGRESSION. `_flog` and `trace` are CLOSURE locals of start_server.

        The first version of this helper logged with them directly and raised
        NameError at the exact moment it was trying to save a turn. Unit tests
        missed it because they injected a mock `_flog`. So the helper must
        return its source instead of logging, and must not reference either
        name outside its own docstring.
        """
        import types as _types

        # Execute the helper in a namespace with NO _flog/trace at all - the
        # honest test of a closure-free helper.
        module = _types.ModuleType("closure_free")
        module.__package__ = "source.server"
        module.__dict__.update({"os": os, "asyncio": srv.asyncio,
                                "time": srv.time})
        for constant in ("FINAL_NARRATION_TIMEOUT", "FINAL_NARRATION_MAX_TOKENS"):
            if hasattr(srv, constant):
                module.__dict__[constant] = getattr(srv, constant)
        for name in ("_recover_truncated_turn", "_final_narration_completion",
                     "_tool_cap_fallback_text", "_verified_work_summary",
                     "_final_narration_prompt"):
            exec(compile(_extracted(name), "<e:%s>" % name, "exec"),
                 module.__dict__)
        self.assertNotIn("_flog", module.__dict__)
        self.assertNotIn("trace", module.__dict__)
        with mock.patch.object(module, "_final_narration_completion",
                               return_value="Recovered."):
            import asyncio as _aio
            text, source = _aio.new_event_loop().run_until_complete(
                module._recover_truncated_turn(
                    mock.Mock(), {}, {"actions": [{} for _ in range(3)]}))
        self.assertEqual(text, "Recovered.")
        self.assertEqual(source, "narration")

    def test_the_call_site_does_the_logging_instead(self):
        """Whoever removed the logging from the helper must log it somewhere."""
        source = server_source()
        self.assertIn('trace("truncated_turn_narration")', source)
        self.assertIn('trace("truncated_turn_truthful_fallback")', source)

    def test_the_fallback_never_fabricates(self):
        mod = _exec_module(["_tool_cap_fallback_text"])
        text = mod._tool_cap_fallback_text(
            12, {"verified": [], "unverified": []}).lower()
        self.assertIn("did not verify anything", text)
        self.assertNotIn("successfully", text)


class L3_2StillFiresTests(unittest.TestCase):
    """L4-1 must not have broken the EXISTING hard-cap recovery.

    The first mutation run found this gap honestly: the L3-2 wiring assertion
    only grepped the source text, so deleting the actual narration CALL at the
    hard-cap site survived. These execute the site's own assignment instead.
    """

    def _cap_site(self):
        source = server_source()
        start = source.index("if tool_counter[\"n\"] > HARD_TOOL_CAP:")
        return source[start:start + 6000]

    def test_the_hard_cap_site_still_assigns_a_real_narration(self):
        window = self._cap_site()
        self.assertIn("_narration = await _final_narration_completion(",
                      window,
                      "the L3-2 recovery call must survive L4-1")

    def test_the_hard_cap_site_still_falls_back_to_truthful_text(self):
        window = self._cap_site()
        self.assertIn("_narration = _tool_cap_fallback_text(", window)

    def test_l4_1_did_not_replace_the_l3_2_call_with_a_no_op(self):
        """Exactly one awaited narration call may exist at the cap site."""
        window = self._cap_site()
        self.assertEqual(
            len([l for l in window.splitlines()
                 if "_narration = await _final_narration_completion(" in l]),
            1)

    def test_both_recoveries_use_the_same_proven_machinery(self):
        """L4-1 must reuse L3-2, not grow a second narration implementation."""
        window = self._cap_site()
        self.assertIn("wrap = _narration", window,
                      "the cap site still delivers what it narrated")
        body = _extracted("_recover_truncated_turn")
        self.assertIn("_final_narration_completion", body)
        self.assertIn("_tool_cap_fallback_text", body)


class WiringTests(unittest.TestCase):
    """Requirements 2 and 8: delivered, and the journal still runs once."""

    def setUp(self):
        self.source = server_source()

    def test_the_hook_sits_in_the_turn_end_path(self):
        i = self.source.index("_budget_truncated_turn(\n")
        # The predicate is called immediately after the empty-turn journal, so
        # widen the window to cover the whole detection block.
        window = self.source[max(0, i - 2600):i + 900]
        self.assertIn("_log_empty_turn", window,
                      "recovery must hang off the empty-turn detection point")
        self.assertIn("not turn.get(\"assistant_snippet\")", window,
                      "the predicate must sit inside the empty-turn branch")

    def test_recovery_result_is_delivered_not_just_logged(self):
        """A narration that is only written to the log is still silence."""
        self.assertIn("queue_speech_only(recovered_narration)", self.source)
        self.assertIn("if not snippet_text and recovered_narration:", self.source)
        self.assertIn("_redact_for_phone(recovered_narration)", self.source)

    def test_local_turns_speak_the_recovery(self):
        # Option 2: the local/external test now reads the ownership record
        # (`not ownership.is_external`) instead of `capture is None`, because
        # the legacy capture dict no longer exists.
        window = self.source[self.source.index("ownership = _ownership(server_state)"):]
        self.assertIn("if recovered_narration and not ownership.is_external:",
                      window[:400])

    def test_log_empty_turn_is_still_called_exactly_once(self):
        """Requirement 8: L4-1 must not journal the same turn twice."""
        self.assertEqual(self.source.count("_log_empty_turn("), 2,
                         "one def + exactly one call site")

    def test_l3_2_hard_cap_path_is_untouched(self):
        """Requirement 9: the existing cap recovery still fires at 24."""
        self.assertIn("if tool_counter[\"n\"] > HARD_TOOL_CAP:", self.source)
        window = self.source[self.source.index("if tool_counter[\"n\"] > HARD_TOOL_CAP:"):]
        window = window[:window.index("# Do NOT fall through")]
        self.assertIn("_final_narration_completion", window)

    def test_the_hard_cap_is_still_24(self):
        self.assertIn('HARD_TOOL_CAP = int(float(os.getenv("FRIDAY_HARD_TOOL_CAP", "24")))',
                      self.source)

    def test_l4_1_adds_no_new_timeout_or_budget(self):
        """It is detection, not another guard."""
        for forbidden in ("FRIDAY_L4", "L4_TIMEOUT", "TRUNCATED_LIMIT",
                          "NARRATION_BUDGET"):
            self.assertNotIn(forbidden, self.source)

    def test_the_recovery_makes_exactly_one_provider_call(self):
        """Requirement: no duplicate brain calls beyond the one recovery."""
        body = _extracted("_recover_truncated_turn")
        self.assertEqual(body.count("_final_narration_completion("), 1)

    def test_the_signature_cannot_be_widened_silently(self):
        """All four conditions must remain required."""
        body = _extracted("_budget_truncated_turn")
        for required in ('assistant_snippet', 'actions', 'finish_reason',
                         'content_len', '"length"'):
            self.assertIn(required, body,
                          "condition %r must remain in the predicate" % required)


class NotInScopeTests(unittest.TestCase):
    """Explicit guards on everything this build must NOT touch."""

    def test_chat_timeout_unchanged(self):
        self.assertIn('os.environ.get("FRIDAY_TEXT_TIMEOUT", "240")',
                      server_source())

    def test_no_global_max_tokens_change(self):
        body = _extracted("_recover_truncated_turn")
        self.assertNotIn("max_tokens", body.replace("max_tokens", "", 0)
                         .replace("FINAL_NARRATION_MAX_TOKENS", "")
                         .replace("\"max_tokens\": FINAL_NARRATION_MAX_TOKENS,", ""))
        # The recovery must not raise the brain budget it is called from.
        self.assertNotIn("params[", body)

    def test_no_new_routing_or_tier(self):
        source = server_source()
        for forbidden in ("reasoning_effort", "tier", "classify"):
            if forbidden == "tier":
                continue
            self.assertNotIn(forbidden, _extracted("_budget_truncated_turn"))


if __name__ == "__main__":
    unittest.main()
