"""ROUND ZA: the per-turn provider-shape accumulator must not cross turns.

`llm_response_shape` records the raw provider response shape (sizes, names, a
prompt hash - no content) so `_log_empty_turn` can classify a turn that produced
no assistant text from evidence instead of inference.

Its reset used to live INSIDE `traced_completions`, gated on
`interpreter.messages[-1]["role"] == "user"`. That is an LLM-LAYER signal: it
can only run once a turn has already decided to call the provider. So every turn
that never reached the provider kept the PREVIOUS turn's per-call records, and a
turn that made no call at all could be filed as `content_but_no_text` with
chunks belonging to someone else's request. The comment at the old site said its
purpose was to prevent exactly that inheritance.

The reset now happens at `_stamp_progress(..., turn_started=True)` - the helper
every channel already calls to mark "a new turn begins".

These tests EXECUTE THE REAL CODE, extracted from `server.py` by AST. Nothing
here reimplements the lifecycle: a hand-written model of it would agree with a
wrong implementation, which is how the original defect survived.
"""

import ast
import os
import re
import sys
import textwrap
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Anchored to __file__, never os.getcwd(): another test in the full suite
# chdirs into a temp directory without restoring (this trap has bitten three
# times in this project already).
SERVER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "source", "server", "server.py")

from source.server.server import (  # noqa: E402
    _merge_chunk_shape,
    _prompt_fingerprint,
    _scrub_tool_choice,
)

# start_server-body pieces: the provider-call side.
_GUARD_NODES = (
    "LOOP_GUARD_CAP",
    "_chat_opener_re",
    "_chat_opener",
    "_strip_user_request_prefix",
    "_begin_llm_call_shape",
    "traced_completions",
)

# module-level pieces: the turn boundary and the reset.
_MODULE_NODES = (
    "_blank_llm_response_shape",
    "_reset_llm_response_shape",
    "_stamp_progress",
)


def _find_start_server(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "start_server":
            return node
    raise AssertionError("start_server not found in %s" % SERVER_PY)


def _statement_for(body, name):
    """One named statement anywhere inside start_server's body, by AST."""
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt
        if isinstance(stmt, ast.FunctionDef):
            if stmt.name == name:
                return stmt
        for field in ("body", "orelse", "finalbody"):
            nested = getattr(stmt, field, None)
            if isinstance(nested, list):
                found = _statement_for(nested, name)
                if found is not None:
                    return found
    return None


class _Log:
    def __init__(self):
        self.records = []

    def info(self, *args):
        self.records.append(("info", args))

    def warning(self, *args, **kwargs):
        self.records.append(("warning", args))


def _load_real_lifecycle():
    """Execute the REAL turn-boundary and provider-call source from server.py.

    Returns (namespace, executed_source). Raises if any required node is
    missing, so a rename cannot silently turn these tests into no-ops.
    """
    with open(SERVER_PY, encoding="utf-8") as handle:
        source_text = handle.read()
    tree = ast.parse(source_text)
    start_server = _find_start_server(tree)
    lines = source_text.splitlines()

    blocks = []
    for name in _MODULE_NODES:
        node = None
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef) and stmt.name == name:
                node = stmt
                break
        assert node is not None, "missing module-level node %r" % name
        blocks.append((node.lineno,
                       textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))))

    for name in _GUARD_NODES:
        node = _statement_for(start_server.body, name)
        assert node is not None, "missing guard node %r" % name
        blocks.append((node.lineno,
                       textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))))

    blocks.sort(key=lambda item: item[0])
    block = "\n".join(text for _, text in blocks)

    interpreter = types.SimpleNamespace(
        messages=[],
        llm=types.SimpleNamespace(model="test/model", api_base="", api_key="",
                                  completions=None),
    )

    server_state = {
        "pending_tool_cap_nudge": None,
        "tool_cap_nudged": False,
        "turn_log": None,
    }

    def _fake_completions(**params):
        """Stand-in provider: one text chunk, then a finish reason."""
        interpreter.llm.last_params = params
        yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    namespace = {
        "__name__": "friday_test_za_shape",
        "os": os,
        "re": re,
        "time": time,
        "types": types,
        "interpreter": interpreter,
        "original_completions": _fake_completions,
        "_flog": _Log(),
        "trace": lambda *_a, **_k: None,
        "llm_loop_guard": {"calls": 0},
        "tool_counter": {"n": 0},
        "server_state": server_state,
        "turn_guard": {"started": 0.0, "console_events": 0, "flood_stopped": False,
                       "same_run": 0, "last_console": None},
        "_merge_chunk_shape": _merge_chunk_shape,
        "_prompt_fingerprint": _prompt_fingerprint,
        "_scrub_tool_choice": _scrub_tool_choice,
    }
    exec(compile(block, SERVER_PY, "exec"), namespace)  # noqa: S102
    namespace["__server_state"] = server_state
    namespace["__interpreter"] = interpreter
    return namespace, block


_NS = {}


def setUpModule():
    _NS["ns"], _NS["src"] = _load_real_lifecycle()


class TurnBoundaryShapeTests(unittest.TestCase):
    """Requirements 1-9: the accumulator belongs to exactly one turn."""

    def setUp(self):
        # A fresh lifecycle per test: rebuild so nothing leaks between tests.
        ns, src = _load_real_lifecycle()
        _NS["ns"] = ns
        _NS["src"] = src
        self.ns = ns
        self.state = ns["__server_state"]
        self.interpreter = ns["__interpreter"]

    # -- helpers driving the REAL code -------------------------------------

    def _begin_turn(self):
        """Exactly what every channel does when a turn starts."""
        # A new turn starts from the user's words.
        self.interpreter.messages = [{"role": "user", "content": "do a thing"}]
        self.ns["_stamp_progress"](self.state, turn_started=True)

    def _mid_turn_progress(self):
        """The four mid-turn progress stamps (turn_started defaults False)."""
        self.ns["_stamp_progress"](self.state)

    def _provider_call(self, content="ok", reasoning=0):
        """One provider call, with OI's message flow modelled faithfully.

        A turn's FIRST call sees the user row last; every later call sees the
        tool-result console row last. That distinction matters here because it
        is exactly the signal the old reset was gated on, and because
        `llm_loop_guard["calls"]` supplies the 1-based call_index.
        """
        def completions(**params):
            self.interpreter.llm.last_params = params
            if reasoning:
                yield {"choices": [{"delta": {"reasoning_content": "r" * reasoning},
                                    "finish_reason": None}]}
            yield {"choices": [{"delta": {"content": content},
                                "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

        self.ns["original_completions"] = completions
        chunks = list(self.ns["traced_completions"](
            messages=self.interpreter.messages, model="test/model"))
        # OI appends the assistant row and then the tool console row.
        self.interpreter.messages.append({"role": "assistant", "content": content})
        self.interpreter.messages.append({"role": "computer", "type": "console",
                                          "content": "1"})
        return chunks

    def _shape(self):
        return self.state.get("llm_response_shape")

    def _calls(self):
        return (self._shape() or {}).get("calls") or []

    # -- 2, 5: a normal provider turn starts empty ------------------------

    def test_a_fresh_turn_starts_with_an_empty_accumulator(self):
        self._begin_turn()
        self.assertIsNotNone(self._shape())
        self.assertEqual(self._calls(), [])
        self.assertFalse(self._shape()["turn_valid"])

    def test_the_accumulator_exists_before_the_first_provider_call(self):
        """Correct by construction, not patched afterwards."""
        self._begin_turn()
        self.assertIn("calls", self._shape())
        self.assertEqual(self._shape(), {"calls": [], "turn_valid": False})

    # -- 6: a provider call populates it -----------------------------------

    def test_a_provider_call_records_its_own_shape(self):
        self._begin_turn()
        self._provider_call(content="hello")
        calls = self._calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_index"], 1)
        self.assertEqual(calls[0]["content_len"], 5)
        self.assertEqual(calls[0]["finish_reason"], "stop")
        self.assertTrue(self._shape()["turn_valid"])

    # -- 7: multiple calls in the SAME turn all survive --------------------

    def test_multiple_provider_calls_in_one_turn_are_all_attributed(self):
        self._begin_turn()
        self._provider_call(content="first")
        self._mid_turn_progress()          # mid-turn stamp must NOT clear it
        self._provider_call(content="second")
        self._provider_call(content="third")
        calls = self._calls()
        self.assertEqual(len(calls), 3, "one turn's calls must not erase each other")
        self.assertEqual([c["call_index"] for c in calls], [1, 2, 3])
        self.assertEqual([c["content_len"] for c in calls], [5, 6, 5])

    def test_a_mid_turn_progress_stamp_does_not_reset_the_accumulator(self):
        """`_stamp_progress()` without turn_started is called mid-turn four
        times. If that reset the accumulator, provider calls would lose each
        other's records mid-turn."""
        self._begin_turn()
        self._provider_call(content="a")
        before = len(self._calls())
        self._mid_turn_progress()
        self.assertEqual(len(self._calls()), before)

    def test_entering_traced_completions_does_not_reset_a_running_turn(self):
        """The old reset site is gone: a second call in a turn (no user role
        last) must keep the first call's record."""
        self._begin_turn()
        self._provider_call(content="a")
        # A tool round-trip leaves a console row last, not a user row.
        self.interpreter.messages = [
            {"role": "user", "content": "do a thing"},
            {"role": "assistant", "content": "```python\nprint(1)```"},
            {"role": "computer", "type": "console", "content": "1"},
        ]
        self.ns["original_completions"] = lambda **p: iter(
            [{"choices": [{"delta": {"content": "b"}, "finish_reason": "stop"}]}])
        list(self.ns["traced_completions"](
            messages=self.interpreter.messages, model="test/model"))
        self.assertEqual(len(self._calls()), 2)

    # -- 1, 3, 4, 8: THE BUG. A no-provider turn must not inherit ----------

    def test_a_turn_that_never_calls_the_provider_inherits_nothing(self):
        # Turn 1: a real provider turn that populates everything.
        self._begin_turn()
        self._provider_call(content="previous turn content", reasoning=7)
        self.assertEqual(len(self._calls()), 1)
        previous = self._shape()
        self.assertGreater(previous["calls"][0]["content_len"], 0)
        self.assertGreater(previous["calls"][0]["reasoning_len"], 0)
        self.assertTrue(previous["turn_valid"])

        # Turn 2: begins, and NEVER reaches the provider (a deterministic
        # command returns before _mark_turn_dispatch/traced_completions).
        self._begin_turn()
        self.assertEqual(self._shape(), {"calls": [], "turn_valid": False},
                         "a no-provider turn must start empty")

    def test_the_original_false_signature_cannot_be_recreated(self):
        """`content_but_no_text` is filed when provider content exists but no
        assistant text did. With the leak, a no-provider turn after a populated
        one was filed as exactly that - the 2026-10-04 signature."""
        self._begin_turn()
        self._provider_call(content="previous turn content")
        # Turn 2: no provider call, and no assistant text either.
        self._begin_turn()
        rs = dict(self._shape() or {})
        calls = [c for c in (rs.get("calls") or []) if isinstance(c, dict)]
        provider_content = sum(int(c.get("content_len") or 0) for c in calls)
        self.assertEqual(provider_content, 0)
        shape = ("content_but_no_text" if provider_content > 0
                 else "reasoning_only" if False else "nothing_at_all")
        self.assertEqual(shape, "nothing_at_all")

    def test_repeated_no_provider_turns_stay_empty(self):
        self._begin_turn()
        self._provider_call(content="once")
        for _ in range(4):
            self._begin_turn()
            self.assertEqual(self._calls(), [])
            self._mid_turn_progress()
            self.assertEqual(self._calls(), [])

    def test_the_next_turn_after_a_provider_turn_starts_empty(self):
        self._begin_turn()
        self._provider_call(content="a")
        self._provider_call(content="b")
        self.assertEqual(len(self._calls()), 2)
        self._begin_turn()
        self.assertEqual(self._calls(), [])
        self._begin_turn()          # text/voice stamp fires twice pre-provider
        self.assertEqual(self._calls(), [])
        # ...and the first call of the new turn is attributed to the new turn.
        self._provider_call(content="c")
        self.assertEqual(len(self._calls()), 1)
        self.assertEqual(self._calls()[0]["content_len"], 1)

    # -- 9: guard / empty paths ---------------------------------------------

    def test_a_prompt_fingerprint_is_never_carried_across_turns(self):
        self._begin_turn()
        self._provider_call(content="a")
        fingerprint = self._calls()[0]["prompt_fingerprint"]
        self.assertTrue(fingerprint)
        self._begin_turn()
        self.assertEqual(self._calls(), [])
        self._provider_call(content="a")
        # Same prompt in a new turn is a NEW record, not the old one.
        self.assertEqual(len(self._calls()), 1)
        self.assertEqual(self._calls()[0]["call_index"], 1)
        self.assertIsNotNone(self._calls()[0]["prompt_fingerprint"])
        self.assertEqual(self._calls()[0]["prompt_fingerprint"], fingerprint)

    def test_every_other_per_call_field_is_also_cleared(self):
        self._begin_turn()
        self._provider_call(content="content here", reasoning=5)
        populated = self._calls()[0]
        self.assertEqual(populated["content_len"], 12)
        self.assertEqual(populated["reasoning_len"], 5)
        self._begin_turn()
        for field in ("chunks", "content_len", "reasoning_len", "finish_reason",
                      "model", "prompt_fingerprint", "usage", "chunk_type",
                      "parse", "top", "choice", "delta", "chars",
                      "stream_opened", "skipped_stream", "finished"):
            self.assertNotIn(field, self._calls() and self._calls()[0] or {},
                             "no field may survive into the next turn: %s" % field)


class ResetLocationTests(unittest.TestCase):
    """The reset must live at the turn boundary, not inside the LLM layer."""

    def setUp(self):
        self.ns, self.src = _load_real_lifecycle()

    def test_the_reset_lives_in_the_turn_boundary_helper(self):
        body = self.src
        stamp_at = body.find("def _stamp_progress")
        self.assertGreater(stamp_at, -1, "_stamp_progress was not extracted")
        stamp_block = body[stamp_at:]
        self.assertIn("_reset_llm_response_shape(server_state)", stamp_block)
        reset_at = body.find("def _reset_llm_response_shape")
        self.assertGreater(reset_at, -1)
        self.assertLess(reset_at, stamp_at,
                        "the helper must be defined before _stamp_progress uses it")

    def test_the_reset_is_gated_on_turn_started(self):
        """A mid-turn progress stamp must not re-arm a live turn."""
        stamp_at = self.src.find("def _stamp_progress")
        block = self.src[stamp_at:]
        reset_at = block.find("_reset_llm_response_shape(server_state)")
        gate_at = block.find("if turn_started:")
        self.assertGreater(reset_at, -1)
        self.assertGreater(gate_at, -1)
        self.assertLess(gate_at, reset_at,
                        "the reset must sit inside the `if turn_started:` block")

    def test_traced_completions_no_longer_resets_the_accumulator(self):
        """The old site was inside traced_completions, behind a user-role
        signal. That is the defect: an LLM-layer reset cannot protect turns that
        never reach the LLM layer."""
        guard_at = self.src.find("def traced_completions")
        self.assertGreater(guard_at, -1)
        block = self.src[guard_at:]
        self.assertNotIn('_reset_llm_response_shape', block)
        self.assertNotIn('llm_response_shape"].update', block)
        self.assertNotIn('llm_response_shape.update', block)

    def test_the_accumulator_is_read_from_server_state_not_a_closure(self):
        """A start_server() closure could not be re-armed by the module-level
        turn boundary at all - that is the NameError trap from the 2026-10-02
        audit."""
        with open(SERVER_PY, encoding="utf-8") as handle:
            full = handle.read()
        tree = ast.parse(full)
        start_server = _find_start_server(tree)
        assigned = []
        for stmt in ast.walk(start_server):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "llm_response_shape":
                        assigned.append(stmt.lineno)
        self.assertEqual(
            assigned, [],
            "llm_response_shape must not be a start_server() closure any more")

    def test_the_empty_turn_reader_takes_the_shape_from_server_state(self):
        """The `_log_empty_turn(..., response_shape=...)` argument.

        This line lives inside start_server's turn-end block, which the
        extracted harness cannot execute, so it is asserted at source level
        rather than left uncovered. The fallback matters: a state that was
        never seeded must hand back an EMPTY shape, never something stale.
        """
        with open(SERVER_PY, encoding="utf-8") as handle:
            full = handle.read()
        tree = ast.parse(full)
        start_server = _find_start_server(tree)
        readers = []
        for node in ast.walk(start_server):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_log_empty_turn"):
                for keyword in node.keywords:
                    if keyword.arg == "response_shape":
                        readers.append(ast.unparse(keyword.value))
        self.assertTrue(readers, "_log_empty_turn is no longer called")
        for expression in readers:
            # ast.unparse normalises string quotes to single.
            self.assertIn("server_state.get('llm_response_shape')", expression)
            self.assertIn("_blank_llm_response_shape()", expression,
                          "an unseeded state must fall back to an EMPTY shape")


class AnomalyClassificationStillWorksTests(unittest.TestCase):
    """Requirement 10: the existing classification must be unaffected."""

    def test_the_three_shapes_are_still_distinguishable(self):
        """Reproduce _log_empty_turn's discriminator over a real shape."""
        def classify(turn, shape):
            rs = dict(shape or {})
            calls = [c for c in (rs.get("calls") or []) if isinstance(c, dict)]
            provider_reasoning = sum(int(c.get("reasoning_len") or 0) for c in calls)
            provider_content = sum(int(c.get("content_len") or 0) for c in calls)
            reasoning_seen = bool(turn.get("thinking") or []) or provider_reasoning > 0
            if provider_content > 0:
                return "content_but_no_text"
            if reasoning_seen:
                return "reasoning_only"
            return "nothing_at_all"

        self.assertEqual(classify({}, {"calls": [{"content_len": 9,
                                                   "reasoning_len": 0}]}),
                         "content_but_no_text")
        self.assertEqual(classify({}, {"calls": [{"content_len": 0,
                                                   "reasoning_len": 9}]}),
                         "reasoning_only")
        self.assertEqual(classify({}, {"calls": [], "turn_valid": False}),
                         "nothing_at_all")
        # The leak's exact failure: a stale populated shape on an empty turn.
        self.assertEqual(classify({}, {"calls": [{"content_len": 9,
                                                   "reasoning_len": 0}]}),
                         "content_but_no_text")

    def test_an_empty_turn_after_a_populated_one_logs_nothing_at_all(self):
        ns, _ = _load_real_lifecycle()
        state = ns["__server_state"]
        ns["_stamp_progress"](state, turn_started=True)
        ns["original_completions"] = lambda **p: iter(
            [{"choices": [{"delta": {"content": "prior"}, "finish_reason": "stop"}]}])
        ns["__interpreter"].messages = [{"role": "user", "content": "hi"}]
        list(ns["traced_completions"](
            messages=ns["__interpreter"].messages, model="test/model"))
        self.assertEqual(len(state["llm_response_shape"]["calls"]), 1)

        # Next turn never reaches the provider.
        ns["_stamp_progress"](state, turn_started=True)
        records = []
        with mock.patch.object(sys.modules["source.server.server"],
                               "_append_turn_record", records.append):
            from source.server.server import _log_empty_turn as real
            real(state, {"assistant_snippet": "", "thinking": [],
                         "actions": [], "assistant_chunks": 0, "assistant_len": 0},
                 response_shape=state.get("llm_response_shape"),
                 turn_failed=False, turn_error="")
        self.assertTrue(records, "an empty turn must still be recorded")
        self.assertEqual(records[0]["shape"], "nothing_at_all")
        self.assertEqual(records[0]["provider_content_len"], 0)
        self.assertEqual(records[0]["calls_this_turn"], 0)


if __name__ == "__main__":
    unittest.main()