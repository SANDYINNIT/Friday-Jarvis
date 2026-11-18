"""ROUND ZB-L4-2: bound the reasoning on the nudge-triggered WRAP-UP call only.

WHY THIS EXISTS
---------------
After 12 tools the loop-guard nudge is delivered, and the completion it lands on
came back `finish_reason="length"`, `content_len=0` - the model spent its whole
`max_tokens=4096` budget on reasoning and was cut off before saying anything.
The nudge asks it to wrap up; wrapping up makes it reason hard; reasoning hard
is what runs it out of budget.

THE TRANSPORT IS THE WHOLE POINT. Measured on this stack (LiteLLM 1.100.0):

    params["reasoning_effort"] = "low"   SILENTLY DROPPED - same 15.5k
                                       reasoning chars, same finish=length,
                                       same zero content. A control that is
                                       accepted and then ignored.
    drop_params = False                  hard UnsupportedParamsError
    optional_params = {...}              also dropped
    extra_body = {"reasoning_effort": "low"}   FORWARDED

So these tests assert on `extra_body`, and one of them goes to the LIVE
provider to prove the value actually reaches Modal rather than merely sitting in
a Python dict.
"""

import ast
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(_HERE, "source", "server", "server.py")
NUDGE = "The OpenCode window is in front of you and you are scrolled to the bottom."


def server_source():
    with open(SERVER_PY, encoding="utf-8") as handle:
        return handle.read()


def _nudge_block():
    """The WHOLE `if _pending_nudge:` statement, comments stripped.

    The whole statement is required, not just its `else` half: the guard that
    makes ordinary calls safe IS the `if`, so extracting only the delivery body
    would make it impossible to prove that an ordinary call is untouched.

    Comments are removed via ast.unparse (they are not in the AST) so the
    explanatory note that records WHY the forbidden parameter forms do not work
    cannot be mistaken for the code using them.
    """
    import textwrap

    tree = ast.parse(server_source())
    outer = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                and node.test.id == "_pending_nudge":
            outer = node
            break
    assert outer is not None, "the `if _pending_nudge:` block was not found"
    lines = server_source().splitlines()
    raw = textwrap.dedent("\n".join(lines[outer.lineno - 1:outer.end_lineno]))
    return ast.unparse(ast.parse(raw))


def _branch():
    """Just the delivery half (the `else`), for transport assertions."""
    import textwrap

    tree = ast.parse(server_source())
    outer = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                and node.test.id == "_pending_nudge":
            outer = node
            break
    inner = outer.body[0]
    delivery = inner.orelse
    lines = server_source().splitlines()
    raw = "\n".join(lines[delivery[0].lineno - 1:delivery[-1].end_lineno])
    return ast.unparse(ast.parse(textwrap.dedent(raw)))


def _code_only():
    """The module with every comment removed.

    The forbidden forms (params["reasoning_effort"], optional_params,
    drop_params) are named in the explanatory comment that records WHY they do
    not work. A test grepping raw text would fail on the very documentation
    that prevents the bug, so it greps `ast.unparse` output - comments are not
    part of the AST, so this is exactly the executable code.
    """
    tree = ast.parse(server_source())
    return ast.unparse(tree)


def _deliver(last_role="computer", model="openai/nvidia/GLM-5.3-Flash-NVFP4",
             extra_body=None, pending=NUDGE):
    """Run the REAL `if _pending_nudge:` block and return the mutated params."""
    interp = mock.Mock()
    interp.messages = []
    ns = {
        "server_state": {"pending_tool_cap_nudge": pending},
        # The line immediately above the block binds this from server_state.
        # Supplying it exactly as that line would keeps the `if` guard real.
        "_pending_nudge": pending,
        "_last": {"role": last_role},
        "params": {"model": model, "messages": []},
        "interpreter": interp,
        "_flog": mock.Mock(),
        "trace": lambda *_a, **_k: None,
    }
    if extra_body is not None:
        ns["params"]["extra_body"] = extra_body
    exec(compile(_nudge_block(), "<nudge-block>", "exec"), ns)   # noqa: S102
    return ns["params"], ns["server_state"]


class WrapUpGetsLowReasoningTests(unittest.TestCase):
    """Requirement 1: the real wrap-up call receives it."""

    def test_the_delivered_wrap_up_call_gets_extra_body_reasoning_effort_low(self):
        params, _state = _deliver(last_role="computer")
        self.assertEqual(params["extra_body"]["reasoning_effort"], "low")

    def test_the_nudge_row_is_still_delivered(self):
        """The optimisation must not have replaced the nudge."""
        params, _state = _deliver()
        self.assertEqual(params["messages"][-1]["content"], NUDGE)

    def test_the_pending_nudge_is_still_consumed(self):
        _params, state = _deliver()
        self.assertIsNone(state["pending_tool_cap_nudge"])


class OrdinaryCallsUnaffectedTests(unittest.TestCase):
    """Requirements 2, 3, 4, 6: nothing else may receive it."""

    def test_an_ordinary_call_is_not_touched(self):
        """No pending nudge -> the branch does not run at all."""
        params, state = _deliver(pending=None)
        self.assertIsNone(state["pending_tool_cap_nudge"])
        self.assertNotIn("extra_body", params)

    def test_the_discarded_nudge_path_does_not_receive_it(self):
        """A new user turn means the nudge belongs to the PREVIOUS turn."""
        params, state = _deliver(last_role="user")
        self.assertIsNone(state["pending_tool_cap_nudge"])
        self.assertNotIn("extra_body", params,
                         "a discarded nudge must not change the next turn")
        self.assertEqual(params["messages"], [])

    def test_a_non_openai_model_does_not_receive_it(self):
        for model in ("ollama_chat/qwen3:8b", "gemini/gemini-3.8-flash",
                      "groq/openai/gpt-oss-20b", "nvidia/GLM-5.3-Flash-NVFP4"):
            params, _state = _deliver(model=model)
            self.assertNotIn(
                "extra_body", params,
                "%s must not receive an OpenAI-shaped extra_body" % model)

    def test_the_bare_barely_model_name_is_not_treated_as_openai(self):
        params, _state = _deliver(model="openai")     # no slash
        self.assertNotIn("extra_body", params)

    def test_it_is_applied_at_most_once_per_turn(self):
        """The guard is latched by tool_cap_nudged, so a second delivery
        cannot happen in the same turn."""
        source = server_source()
        self.assertIn('server_state["tool_cap_nudged"] = True', source)
        self.assertIn('and not server_state.get("tool_cap_nudged")', source)
        params, state = _deliver()
        # First delivery consumes the pending nudge, so a re-entry is inert.
        self.assertIsNone(state["pending_tool_cap_nudge"])
        self.assertEqual(params["extra_body"]["reasoning_effort"], "low")


class ExtraBodyMergingTests(unittest.TestCase):
    """Requirement 5: merge, never clobber."""

    def test_existing_extra_body_fields_are_preserved(self):
        params, _state = _deliver(
            extra_body={"some_setting": "keep-me", "other": 7})
        self.assertEqual(params["extra_body"]["some_setting"], "keep-me")
        self.assertEqual(params["extra_body"]["other"], 7)
        self.assertEqual(params["extra_body"]["reasoning_effort"], "low")

    def test_the_existing_dictionary_object_is_reused_not_replaced(self):
        """Replacing the dict would silently drop a live setting elsewhere."""
        original = {"keep": True}
        params, _state = _deliver(extra_body=original)
        self.assertIs(params["extra_body"], original)
        self.assertEqual(original["reasoning_effort"], "low")

    def test_a_non_dict_extra_body_is_replaced_rather_than_crashing(self):
        params, _state = _deliver(extra_body="not-a-dict")
        self.assertEqual(params["extra_body"], {"reasoning_effort": "low"})

    def test_no_extra_body_at_all_creates_a_clean_one(self):
        params, _state = _deliver(extra_body=None)
        self.assertEqual(params["extra_body"], {"reasoning_effort": "low"})


class TransportTests(unittest.TestCase):
    """The parameter must go via extra_body, and only via extra_body."""

    def test_it_never_uses_the_top_level_param(self):
        branch = _branch()
        self.assertNotIn('params["reasoning_effort"]', branch)
        self.assertNotIn('params["reasoning_effort"]', _code_only())

    def test_it_never_uses_optional_params(self):
        self.assertNotIn("optional_params", _branch())
        self.assertNotIn("optional_params", _code_only())

    def test_it_never_touches_drop_params(self):
        self.assertNotIn("drop_params", _branch())
        self.assertNotIn("drop_params", _code_only())

    def test_it_does_not_change_max_tokens(self):
        """max_tokens=4096 is the budget we are working AROUND, not raising.

        Checked on the BRANCH only: `max_tokens` legitimately appears elsewhere
        in the module (the narration budget, the per-call shape record), so a
        module-wide assertion would fail on unrelated correct code.
        """
        self.assertNotIn("max_tokens", _branch())

    def test_the_branch_cannot_reach_the_litellm_transport_settings(self):
        self.assertNotIn("drop_params", _code_only())
        self.assertNotIn("optional_params", _code_only())
        self.assertNotIn('params["reasoning_effort"]', _code_only())


class UnchangedBehaviourTests(unittest.TestCase):
    """Requirements 7-11."""

    def setUp(self):
        self.source = server_source()

    def test_qwen3_think_false_is_intact(self):
        self.assertIn('params["think"] = False', self.source)
        self.assertIn('startswith("qwen3")', self.source)

    def test_the_qwen3_rule_still_fires_before_the_nudge_branch(self):
        """It must not have been reordered behind the nudge flush."""
        self.assertLess(self.source.index('params["think"] = False'),
                        self.source.index('_pending_nudge = server_state.get('))

    def test_l4_1_predicate_is_unchanged(self):
        self.assertIn("def _budget_truncated_turn(", self.source)
        self.assertIn('!= "length"', self.source)
        self.assertIn("content_len", self.source)

    def test_l3_2_narration_still_uses_a_raw_request(self):
        """It must NOT be rerouted through LiteLLM by this change."""
        start = self.source.index("async def _final_narration_completion")
        body = self.source[start:start + 3000]
        self.assertIn("requests.post", body)
        self.assertNotIn("litellm", body)

    def test_hard_cap_is_still_24(self):
        self.assertIn(
            'HARD_TOOL_CAP = int(float(os.getenv("FRIDAY_HARD_TOOL_CAP", "24")))',
            self.source)

    def test_chat_timeout_is_still_240(self):
        self.assertIn('os.environ.get("FRIDAY_TEXT_TIMEOUT", "240")', self.source)

    def test_the_provider_chain_is_untouched(self):
        """The chain lives in brain_router.py, not server.py."""
        router = os.path.join(_HERE, "source", "server", "brain_router.py")
        with open(router, encoding="utf-8") as handle:
            src = handle.read()
        for token in ("modal_candidates", "groq_candidates",
                      "openrouter_candidates", "gemini_candidates"):
            self.assertIn(token, src)

    def test_cooldowns_are_untouched(self):
        """Cooldown constants live in api_pools.py."""
        pools = os.path.join(_HERE, "source", "server", "api_pools.py")
        with open(pools, encoding="utf-8") as handle:
            src = handle.read()
        for token in ('FRIDAY_API_COOLDOWN", "21600"',
                      "RATE_LIMIT_COOLDOWN_SECONDS",
                      "TRANSIENT_COOLDOWN_SECONDS"):
            self.assertIn(token, src)

    def test_retry_params_still_preserves_extra_body(self):
        """The plain-text retry strips tools, not extra_body - so the wrap-up
        setting survives a mid-stream tool-hallucination retry."""
        self.assertIn(
            'retry_params = {k: v for k, v in params.items() '
            'if k not in ("tools", "tool_choice")}', self.source)

    def test_nothing_else_writes_extra_body(self):
        """A second writer would be a second, unscoped path."""
        self.assertEqual(self.source.count('params["extra_body"] = _extra'), 1)


class LiveForwardingTest(unittest.TestCase):
    """Requirement: prove the value REACHES Modal, not just a Python dict.

    Skipped when Modal is unreachable; the unit tests above are the baseline.
    """

    def test_extra_body_actually_reaches_the_provider(self):
        """Prove the parameter REACHES Modal, not just a Python dict.

        Driven through LiteLLM, because that is the path FRIDAY actually uses:
        `extra_body` is LiteLLM's passthrough wrapper, NOT an API field. Posting
        it raw to the endpoint sends a literal `extra_body` key that the server
        ignores - which is precisely the failure this test exists to prevent,
        and which a raw-HTTP version of this test had silently reproduced.

        The prompt is the one that reproduced the ZB-L4 truncation (measured
        15.6k-16.2k reasoning chars, content 0, finish=length at 4096). A
        lighter prompt does not truncate, so it would prove nothing.
        """
        try:
            import litellm
            from source.server import brain_router as br
            from source.server import api_pools as ap
        except Exception as error:                    # pragma: no cover
            self.skipTest("import failed: %s" % error)
        ap._ensure_pools()
        keys = getattr(ap.pool("brain_modal"), "_keys", None) or []
        if not keys:
            self.skipTest("no brain_modal key configured")
        model = "openai/" + br.MODAL_MODEL
        hard = (
            "Analyse this carefully, reasoning about every failure mode:\n\n"
            "def process(items):\n    seen={}\n    out=[]\n"
            "    for it in items:\n"
            "        if it['id'] in seen: seen[it['id']]+=1\n"
            "        else: seen[it['id']]=1\n        out.append(it['id'])\n"
            "    return out, seen\n\n"
            "List every subtle bug and the correct rewrite.")
        saved_drop, saved_retries = litellm.drop_params, litellm.num_retries

        def ask(extra):
            litellm.drop_params = True      # FRIDAY's setting, unchanged
            litellm.num_retries = 0
            try:
                r = litellm.completion(
                    model=model,
                    messages=[{"role": "user", "content": hard}],
                    api_base=br.MODAL_BASE, api_key=keys[0],
                    max_tokens=4096, num_retries=0, **extra)
            finally:
                litellm.drop_params, litellm.num_retries = saved_drop, saved_retries
            ch = r.choices[0]
            msg = ch.message
            return (ch.finish_reason,
                    len(getattr(msg, "reasoning_content", None) or ""),
                    len(getattr(msg, "content", None) or ""))

        d_finish, d_reason, d_content = ask({})
        l_finish, l_reason, l_content = ask(
            {"extra_body": {"reasoning_effort": "low"}})

        # The causal signal: the parameter reached the provider, so the model
        # reasoned materially less.
        self.assertLess(
            l_reason, d_reason,
            "extra_body did not reduce reasoning (%d vs %d) - the parameter "
            "did not reach Modal" % (l_reason, d_reason))
        self.assertEqual(d_finish, "length",
                         "the baseline prompt must truncate, else this proves "
                         "nothing (reasoning=%d content=%d)" % (d_reason, d_content))
        self.assertEqual(d_content, 0)
        self.assertEqual(l_finish, "stop",
                         "low reasoning must convert truncation into a usable "
                         "answer (got %s, reasoning=%d)" % (l_finish, l_reason))
        self.assertGreater(l_content, 0,
                           "low reasoning must produce real content")


if __name__ == "__main__":
    unittest.main()
