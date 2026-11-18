"""ROUND ZB-L3 L3-2: a turn that hits the hard tool cap must still SPEAK.

THE LIVE FAILURE BEING LOCKED DOWN
----------------------------------
On 2026-10-07 a turn ran 25 tools against a healthy Modal provider. The
viewport genuinely moved (the model's own digest went 6571c867 -> 9ed7f552),
then `HARD_TOOL_CAP` killed the turn. The journal recorded
`shape=reasoning_only actions=25 chunks=0 len=0` and the `/chat` caller got a
504 after 240 s. The work was real and the owner was told nothing. Round Z had
seen the same family at actions=12.

What must now hold:
  * the cap is still 24 - it is not raised
  * ONE final completion is attempted, with NO tools in the payload
  * that final narration is what gets delivered
  * if it cannot be obtained, a TRUTHFUL fallback is delivered instead
  * the fallback never claims success that was not verified
  * NO further tool executes after the cap trips
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
    """Pull a module-level helper out of the deployed server.py and exec it."""
    import textwrap

    tree = ast.parse(server_source())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            lines = server_source().splitlines()
            return textwrap.dedent(
                "\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError("%s not found as a module-level function" % name)


def _exec_helper(name, extra=None):
    """exec the deployed helper into a namespace that can actually run it.

    Two things are needed and both were found the hard way:
      * `__package__`, because `_verified_work_summary` does `from . import
        ui_scroll` and a bare namespace has no package to resolve it against;
      * the helper's SIBLINGS, because `_final_narration_completion` calls
        `_final_narration_prompt`, which lives in the same module in the
        shipped server.
    """
    import types as _types

    module = _types.ModuleType("extracted_%s" % name)
    module.__package__ = "source.server"
    module.__dict__.update({"os": os, "asyncio": srv.asyncio, "time": srv.time})
    # Module-level CONSTANTS the helpers read. Bound from the live module so the
    # tests exercise the shipped values rather than copies of them.
    for constant in ("FINAL_NARRATION_TIMEOUT", "FINAL_NARRATION_MAX_TOKENS"):
        if hasattr(srv, constant):
            module.__dict__[constant] = getattr(srv, constant)
    module.__dict__.update(extra or {})
    for sibling in ("_final_narration_prompt", "_tool_cap_fallback_text",
                    "_verified_work_summary", "_final_narration_completion"):
        if sibling == name:
            continue
        try:
            source = _extracted(sibling)
        except AssertionError:
            continue
        exec(compile(source, "<extracted:%s>" % sibling, "exec"),
             module.__dict__)
    exec(compile(_extracted(name), "<extracted>", "exec"), module.__dict__)
    return module


class _FakeLlm:
    def __init__(self, api_base="https://example.invalid/v1",
                 model="openai/some-model", api_key="k"):
        self.api_base = api_base
        self.model = model
        self.api_key = api_key


class _FakeInterpreter:
    def __init__(self, llm=None):
        self.llm = llm if llm is not None else _FakeLlm()


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _completion_payload(text):
    return {"choices": [{"message": {"content": text}}]}


class CapConstantTests(unittest.TestCase):
    def test_the_cap_is_still_twenty_four(self):
        """L3-2 must NOT be 'raise the cap'."""
        source = server_source()
        self.assertIn('HARD_TOOL_CAP = int(float(os.getenv("FRIDAY_HARD_TOOL_CAP", "24")))',
                      source)

    def test_the_cap_site_is_untouched_by_a_raise(self):
        self.assertNotIn("FRIDAY_HARD_TOOL_CAP\", \"32\"", server_source())


class FallbackTruthfulnessTests(unittest.TestCase):
    """The deterministic branch must never invent a success."""

    def setUp(self):
        self.mod = _exec_helper("_tool_cap_fallback_text")

    def test_with_nothing_verified_it_claims_nothing(self):
        text = self.mod._tool_cap_fallback_text(25, {"verified": [], "unverified": []})
        lowered = text.lower()
        self.assertIn("did not verify anything", lowered)
        self.assertIn("not claiming any of it worked", lowered)
        # A claim of success, not the substring: "not claiming any of it
        # worked" contains "worked", so a naive word check would be wrong.
        for forbidden in ("successfully", "i completed", "i finished",
                          "i did it", "all done"):
            self.assertNotIn(forbidden, lowered,
                             "fallback must not claim success: %r" % forbidden)

    def test_verified_work_is_reported_and_unfinished_work_is_named(self):
        summary = {
            "verified": ["scroll up OpenCode (wheel)"],
            "unverified": ["scroll down OpenCode (wheel)"],
        }
        text = self.mod._tool_cap_fallback_text(25, summary)
        self.assertIn("scroll up OpenCode", text)
        self.assertIn("Still unfinished", text)
        self.assertIn("scroll down OpenCode", text)

    def test_it_names_the_real_tool_count(self):
        text = self.mod._tool_cap_fallback_text(25, {"verified": [], "unverified": []})
        self.assertIn("25 tools", text)

    def test_a_missing_summary_does_not_crash_it(self):
        text = self.mod._tool_cap_fallback_text(24, None)
        self.assertIn("24 tools", text)


class FinalNarrationPayloadTests(unittest.TestCase):
    def setUp(self):
        self.mod = _exec_helper("_final_narration_prompt")

    def test_the_prompt_forbids_tools_and_forbids_false_claims(self):
        prompt = self.mod._final_narration_prompt(
            "scroll opencode up", 25,
            {"verified": ["scroll up OpenCode (wheel)"], "unverified": []})
        self.assertIn("may not call any more tools", prompt)
        self.assertIn("NEVER claim the task is done", prompt)
        self.assertIn("Report ONLY what is listed as verified", prompt)
        self.assertIn("scroll opencode up", prompt)

    def test_the_prompt_carries_the_verified_evidence(self):
        prompt = self.mod._final_narration_prompt(
            "x", 25,
            {"verified": ["scroll up OpenCode (wheel)"], "unverified": ["b"]})
        self.assertIn("VERIFIED: scroll up OpenCode (wheel)", prompt)
        self.assertIn("NOT VERIFIED: b", prompt)

    def test_with_no_evidence_it_says_so_explicitly(self):
        prompt = self.mod._final_narration_prompt("x", 24, {"verified": [],
                                                            "unverified": []})
        self.assertIn("VERIFIED: nothing.", prompt)


class FinalNarrationCompletionTests(unittest.TestCase):
    """One tools-free completion, or None - never an exception."""

    def _run(self, response, llm=None):
        mod = _exec_helper("_final_narration_completion")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
            if isinstance(response, Exception):
                raise response
            return response

        import requests as _requests
        with mock.patch.object(_requests, "post", fake_post):
            import asyncio as _aio
            result = _aio.new_event_loop().run_until_complete(
                mod._final_narration_completion(
                    _FakeInterpreter(llm), "scroll up", 25,
                    {"verified": [], "unverified": []}))
        return result, captured

    def test_a_good_completion_returns_its_text(self):
        text, captured = self._run(
            _Response(200, _completion_payload("The viewport moved, Sir.")))
        self.assertEqual(text, "The viewport moved, Sir.")

    def test_the_payload_contains_no_tools(self):
        """The requirement: the final completion must have NO tools available."""
        _text, captured = self._run(
            _Response(200, _completion_payload("done")))
        self.assertNotIn("tools", captured["json"],
                         "the final narration must not offer tools")
        self.assertNotIn("tool_choice", captured["json"])

    def test_a_provider_error_returns_none_so_the_fallback_can_run(self):
        text, _ = self._run(_Response(500, {}))
        self.assertIsNone(text)

    def test_an_empty_completion_returns_none(self):
        text, _ = self._run(_Response(200, _completion_payload("")))
        self.assertIsNone(text)

    def test_a_transport_exception_returns_none_rather_than_raising(self):
        text, _ = self._run(RuntimeError("connection reset"))
        self.assertIsNone(text)

    def test_no_configured_endpoint_returns_none(self):
        text, _ = self._run(_Response(200, _completion_payload("x")),
                            llm=_FakeLlm(api_base=None, model=None, api_key=None))
        self.assertIsNone(text)

    def test_it_refuses_the_local_candidate(self):
        """A second local inference would compete with the fallback brain."""
        text, _ = self._run(
            _Response(200, _completion_payload("x")),
            llm=_FakeLlm(api_base="http://localhost:11434",
                         model="ollama_chat/qwen3:8b"))
        self.assertIsNone(text)

    def test_the_url_is_normalised_to_v1(self):
        _text, captured = self._run(
            _Response(200, _completion_payload("x")),
            llm=_FakeLlm(api_base="https://host.example"))
        self.assertTrue(captured["url"].endswith("/v1/chat/completions"))

    def test_an_already_v1_base_is_not_doubled(self):
        _text, captured = self._run(
            _Response(200, _completion_payload("x")),
            llm=_FakeLlm(api_base="https://host.example/v1"))
        self.assertEqual(captured["url"].count("/v1/"), 1)


class VerifiedSummaryTests(unittest.TestCase):
    def setUp(self):
        self.mod = _exec_helper("_verified_work_summary")

    def test_it_reads_the_real_action_ledger(self):
        from source.server import ui_scroll

        ui_scroll.reset_action_history()
        try:
            summary = self.mod._verified_work_summary()
            self.assertIn("verified", summary)
            self.assertIn("unverified", summary)
            self.assertIsInstance(summary["verified"], list)
        finally:
            ui_scroll.reset_action_history()

    def test_only_verified_records_land_in_the_verified_list(self):
        from source.server import ui_scroll

        ui_scroll.reset_action_history()
        try:
            ui_scroll._record({"window": "OpenCode", "action": "scroll up 3",
                               "mechanism": "wheel", "verified": True})
            ui_scroll._record({"window": "OpenCode", "action": "scroll down 3",
                               "mechanism": "wheel", "verified": False})
            summary = self.mod._verified_work_summary()
            self.assertEqual(len(summary["verified"]), 1)
            self.assertIn("scroll up 3", summary["verified"][0])
            self.assertEqual(len(summary["unverified"]), 1)
        finally:
            ui_scroll.reset_action_history()


class CapSiteWiringTests(unittest.TestCase):
    """Source-level guards: the cap site must do what L3-2 promises."""

    def setUp(self):
        self.source = server_source()

    def test_the_cap_site_attempts_a_final_narration(self):
        start = self.source.index("HARD tool stop at #%s")
        window = self.source[start:start + 5200]
        self.assertIn("_final_narration_completion", window)

    def test_the_cap_site_falls_back_to_a_truthful_sentence(self):
        start = self.source.index("HARD tool stop at #%s")
        window = self.source[start:start + 5200]
        self.assertIn("_tool_cap_fallback_text", window)

    def test_the_cap_site_blocks_the_tool_instead_of_running_it(self):
        """stop_event is cooperative; the live turn ran tool #25 anyway."""
        start = self.source.index("HARD tool stop at #%s")
        window = self.source[start:start + 9000]
        self.assertIn("# Do NOT fall through: this tool must never execute.",
                      window)
        tail = window[window.index("# Do NOT fall through"):]
        tail = tail[:tail.index("trace(\"tool_start\")")]
        self.assertTrue(tail.strip().endswith("return"),
                        "the cap site must return, not fall through; got %r"
                        % tail.strip()[-80:])

    def test_the_cap_site_still_sets_stop_event(self):
        start = self.source.index("HARD tool stop at #%s")
        window = self.source[start:start + 5200]
        self.assertIn("interpreter.stop_event.set()", window)

    def test_no_orphaned_else_block_was_left_behind(self):
        """A stale duplicate telegram branch shipped once during this build."""
        start = self.source.index("HARD tool stop at #%s")
        end = self.source.index('trace("tool_start")', start)
        cap = self.source[start:end]
        self.assertEqual(cap.count("await _terminal_notice("), 1)
        self.assertNotIn("_clear_ownership", cap)

    def test_final_narration_is_not_given_tools(self):
        mod = _exec_helper("_final_narration_completion")
        body = _extracted("_final_narration_completion")
        self.assertNotIn('"tools"', body)
        self.assertNotIn("tools=", body)
        self.assertIsNotNone(mod)


if __name__ == "__main__":
    unittest.main()
