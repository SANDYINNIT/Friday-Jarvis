"""ROUND Q (2026-10-03) - the tool-cap nudge must never orphan a tool result.

THE DEFECT THIS LOCKS
=====================
FRIDAY's tool-cap guard (SERVER_TOOL_CAP, default 12) used to append its nudge
straight into `interpreter.messages` from the tool-complete handler. That
handler runs BEFORE Open Interpreter appends the tool's console/result row, so
the nudge landed WEDGED between a tool call and its result:

    [..., assistant_code, user_nudge, computer_console]

`run_tool_calling_llm.py` pairs calls to results POSITIONALLY (line 52, it only
ever looks at `messages[i + 1]`). An interposed row therefore orphans the
result, and OI's orphan branch rebuilds a tool call whose `arguments` is the
literal non-JSON string

    "# Automated tool call to fetch more output, triggered by the user."
    (run_tool_calling_llm.py:80)

That row is regenerated from `interpreter.messages` on EVERY later request, so
every provider answered HTTP 400 "Assistant tool call function.arguments must be
valid JSON" - forever, identically, until the process restarted. Observed live:
one conversation died across Modal + Groq x2 + OpenRouter x2, FRIDAY reported
"every thinking model I have is unavailable right now" (false), and the worst
observed loss was ~9.5 minutes.

THE FIX
=======
Defer the append. The cap handler stashes the nudge in
`server_state["pending_tool_cap_nudge"]`; `traced_completions` flushes it at the
last moment before the next request, giving the safe shape

    [..., assistant_code, computer_console, user_nudge]

`params["messages"]` is updated too, because `respond()` copies
`interpreter.messages` at respond.py:63 BEFORE `llm.run()` at respond.py:88 -
appending to history alone would deliver the nudge a call late and change the
guard's meaning.

WHY THESE TESTS ARE STRUCTURAL, NOT VACUOUS
===========================================
`traced_completions` is a closure inside `start_server`, so it cannot be
imported. `_load_production_guard()` parses `server.py` with `ast`, extracts the
REAL source of the guard nodes, and executes it - every assertion runs
production code. On top of that, the SAFETY invariant itself is asserted
against the REAL `interpreter.core.llm.run_tool_calling_llm.process_messages`,
so reverting the fix in `server.py` makes `process_messages` produce malformed
arguments and turns these tests red.
"""

import ast
import json
import os
import sys
import textwrap
import types
import unittest

sys.path.insert(0, os.getcwd())

from interpreter.core.llm.run_tool_calling_llm import process_messages  # noqa: E402
from source.server.server import (  # noqa: E402
    _merge_chunk_shape,
    _prompt_fingerprint,
    _redact_text,
    _scrub_tool_choice,
)

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")

_REQUIRED_NODES = (
    "LOOP_GUARD_CAP",
    "_chat_opener_re",
    "_chat_opener",
    "_strip_user_request_prefix",
    "_begin_llm_call_shape",
    "traced_completions",
)

_NUDGE_MARKER = "[System loop-guard]"


def _locate_tool_cap_block(tree):
    """Find `tool_cap = SERVER_TOOL_CAP[0]` plus the `if` that follows it.

    Returns the two statements' source span so the REAL handler can be
    executed, rather than a hand-written replica of it.
    """
    start_server = _find_start_server(tree)
    for node in ast.walk(start_server):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for index, stmt in enumerate(body[:-1]):
            if not isinstance(stmt, ast.Assign):
                continue
            names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if "tool_cap" not in names:
                continue
            following = body[index + 1]
            if not isinstance(following, ast.If):
                continue
            rendered = ast.unparse(following.test)
            if "tool_cap" in rendered and "tool_cap_nudged" in rendered:
                return stmt, following
    raise AssertionError("tool-cap handler block not found")


# ---------------------------------------------------------------------------
# Real-history helpers. These build the shapes OI actually stores.
# ---------------------------------------------------------------------------


def _code_row(code="print(1)"):
    """An assistant tool call, exactly as OI stores it (function_call form)."""
    return {
        "role": "assistant",
        "type": "message",
        "format": "python",
        "content": code,
        "function_call": {
            "name": "execute",
            "arguments": json.dumps({"language": "python", "code": code}),
        },
    }


def _result_row(text="1"):
    """A tool result. OI converts a computer:console row to role 'function'."""
    return {"role": "function", "content": text}


def _user_row(text="hello"):
    return {"role": "user", "type": "message", "content": text}


def _nudge_row(text="[System loop-guard] This turn has now run 12 tool executions ..."):
    return {"role": "user", "type": "message", "content": text}


def _strict_json_ok(raw):
    """True only if `raw` is genuinely valid JSON.

    Deliberately does NOT call `json.loads`. `profiles/default.py:24` does
    `factory1.json.loads = safe_json_loads`, and `factory1.json` IS the stdlib
    json module - so importing FRIDAY's profile replaces json.loads PROCESS-WIDE
    with a wrapper that never raises and returns {"code": <raw>} instead. Using
    json.loads here would therefore make this assertion depend on whether
    another test imported the profile first. `JSONDecoder().decode` is
    untouched by that patch and is the honest check.
    """
    try:
        json.JSONDecoder().decode(raw)
    except Exception:
        return False
    return True


def _malformed_arguments(messages):
    """Run the REAL OI reconstruction and return every non-JSON tool-call arg."""
    bad = []
    for message in process_messages([dict(m) for m in messages]):
        for call in message.get("tool_calls") or []:
            raw = (call.get("function") or {}).get("arguments")
            if raw is None:
                continue
            if not _strict_json_ok(raw):
                bad.append(raw)
    return bad


# ---------------------------------------------------------------------------
# Production guard harness (AST extraction + exec), same technique as Round O.
# ---------------------------------------------------------------------------


def _find_start_server(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "start_server":
            return node
    raise AssertionError("start_server not found in %s" % SERVER_PY)


_NESTED_BODY_FIELDS = ("body", "orelse", "finalbody")


def _statement_for(body, name):
    """Locate a named statement anywhere inside start_server.

    Descends control-flow blocks but never into a FunctionDef other than the
    one being sought, so a same-named local cannot be mistaken for production.
    """
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt
        if isinstance(stmt, ast.FunctionDef):
            if stmt.name == name:
                return stmt
            continue
        for field in _NESTED_BODY_FIELDS:
            nested = getattr(stmt, field, None)
            if isinstance(nested, list) and nested:
                try:
                    return _statement_for(nested, name)
                except AssertionError:
                    continue
    raise AssertionError("guard node %r not found in start_server" % name)


def _load_production_guard(env=None, tool_cap=None):
    with open(SERVER_PY, encoding="utf-8") as handle:
        source_text = handle.read()
    tree = ast.parse(source_text)
    start_server = _find_start_server(tree)

    lines = source_text.splitlines()
    blocks = []
    for name in _REQUIRED_NODES:
        node = _statement_for(start_server.body, name)
        segment = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        blocks.append((node.lineno, textwrap.dedent(segment)))
    blocks.sort(key=lambda item: item[0])
    block = "\n".join(text for _, text in blocks)

    interpreter = types.SimpleNamespace(
        messages=[],
        llm=types.SimpleNamespace(
            model="test/model", api_base="", api_key="", completions=None
        ),
    )

    log_records = []

    class _Log:
        def info(self, *args):
            log_records.append(("info", args))

        def warning(self, *args):
            log_records.append(("warning", args))

    traces = []

    def _fake_completions(**params):
        interpreter.llm.last_params = params
        yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}

    server_state = {
        "pending_tool_cap_nudge": None,
        "tool_cap_nudged": False,
        "turn_log": None,
        "last_screen_shot": {},
        "is_paused": False,
        # ROUND ZA (2026-10-05): the per-turn provider-shape accumulator now
        # lives in server_state and is re-armed at the turn boundary, so the
        # harness must seed it here rather than as a namespace local.
        "llm_response_shape": {"calls": [], "turn_valid": False},
    }

    namespace = {
        "__name__": "friday_test_roundq",
        "os": os,
        "re": __import__("re"),
        "time": types.SimpleNamespace(monotonic=lambda: 0.0),
        "interpreter": interpreter,
        "server_state": server_state,
        "original_completions": _fake_completions,
        "_flog": _Log(),
        "trace": traces.append,
        "llm_loop_guard": {"calls": 0},
        "tool_counter": {"n": 0},
        "turn_guard": {
            "started": 0.0,
            "console_events": 0,
            "flood_stopped": False,
            "same_run": 0,
            "last_console": None,
        },
        "_merge_chunk_shape": _merge_chunk_shape,
        "_prompt_fingerprint": _prompt_fingerprint,
        "_scrub_tool_choice": _scrub_tool_choice,
        "_redact_text": _redact_text,
        "__log_records": log_records,
        "__traces": traces,
    }
    if tool_cap is None:
        # Mirror production's own default so the trigger replica uses the real cap.
        tool_cap = float(os.getenv("FRIDAY_TOOL_CAP", "12"))
    namespace["SERVER_TOOL_CAP"] = [float(tool_cap)]

    previous = {}
    for key, value in (env or {}).items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        exec(compile(block, SERVER_PY, "exec"), namespace)

        # Execute the REAL tool-cap handler (not a replica), so a regression in
        # its history placement is observable by these tests.
        cap_assign, cap_if = _locate_tool_cap_block(tree)
        handler_src = textwrap.dedent(
            "\n".join(
                lines[cap_assign.lineno - 1 : cap_if.end_lineno]
            )
        )
        namespace["chat_append"] = lambda *a, **k: None
        exec(
            compile(
                "def _run_real_tool_cap_guard():\n"
                + "\n".join("    " + ln for ln in handler_src.splitlines()),
                SERVER_PY,
                "exec",
            ),
            namespace,
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    namespace["__source__"] = block
    namespace["__interpreter__"] = interpreter
    namespace["__state__"] = server_state
    return namespace


class _Turn:
    """Drives a simulated turn through the REAL production guard."""

    def __init__(self, env=None, tool_cap=None):
        self.ns = _load_production_guard(env=env, tool_cap=tool_cap)
        self.interpreter = self.ns["__interpreter__"]
        self.state = self.ns["__state__"]
        self.interpreter.messages = []

    @property
    def messages(self):
        return self.interpreter.messages

    def user(self, text="do the thing"):
        self.interpreter.messages.append(_user_row(text))

    def assistant_code(self, code="print(1)"):
        self.interpreter.messages.append(_code_row(code))

    def console(self, text="1"):
        self.interpreter.messages.append(_result_row(text))

    def call(self):
        """One traced_completions call. Returns the chunks it yielded."""
        params = {"messages": list(self.interpreter.messages), "model": "test/model"}
        return list(self.ns["traced_completions"](**params)), params

    # -- the REAL production nudge trigger, executed from source --
    def fire_tool_cap_nudge(self):
        """Run the production tool-cap handler verbatim.

        Returns the nudge text, or None when the guard did not fire.
        """
        self.ns["_run_real_tool_cap_guard"]()
        return self.state.get("pending_tool_cap_nudge")

    @property
    def log_text(self):
        return "\n".join(str(args) for _, args in self.ns["__log_records"])

    @property
    def traces(self):
        return list(self.ns["__traces"])

    @property
    def shapes(self):
        # ROUND ZA: server_state owns the accumulator; see the
        # note where it is seeded in this harness.
        return self.ns["server_state"]["llm_response_shape"]["calls"]


def _run_tool_turn(turn, tools, cap=12, deliver=True):
    """Run `tools` tool steps through the production nudge trigger.

    Models OI's real ordering: the code row lands, the tool runs, then the
    console/result row lands. Returns the nudge text if the guard fired.
    """
    nudge = None
    for index in range(tools):
        turn.ns["tool_counter"]["n"] += 1
        turn.assistant_code("print(%d)" % (index + 1))
        fired = turn.fire_tool_cap_nudge()
        if fired:
            nudge = fired
        # OI appends the result row AFTER the tool completes - this ordering is
        # the whole reason the old direct append was unsafe.
        turn.console(str(index + 1))
        if deliver:
            turn.call()
    return nudge


# ---------------------------------------------------------------------------
# 1. REAL HISTORY REPRODUCTION - the defect exists (baseline).
# ---------------------------------------------------------------------------


class DangerousHistoryReproductionTests(unittest.TestCase):
    """Proves the defect against the REAL OI reconstruction, not a mock."""

    def test_interposed_nudge_produces_malformed_arguments(self):
        messages = [
            _user_row("list the folders"),
            _code_row("import os"),
            _nudge_row(),
            _result_row("Desktop"),
        ]
        bad = _malformed_arguments(messages)
        self.assertTrue(
            bad,
            "BASELINE FAILED: the interposed shape no longer breaks OI. If OI "
            "was fixed upstream, this whole round may be moot - re-check.",
        )
        self.assertEqual(len(bad), 1)

    def test_the_synthesized_argument_is_the_known_non_json_string(self):
        messages = [_code_row("import os"), _nudge_row(), _result_row("Desktop")]
        bad = _malformed_arguments(messages)
        self.assertEqual(len(bad), 1)
        self.assertIn("#", bad[0])
        self.assertFalse(_strict_json_ok(bad[0]))

    def test_the_strict_json_check_survives_the_global_json_patch(self):
        """Guard for the checker itself.

        `profiles/default.py` replaces json.loads process-wide, so a checker
        built on json.loads would silently stop detecting malformed JSON once
        that module is imported. Assert the checker still works either way.
        """
        self.assertFalse(_strict_json_ok("# not json at all"))
        self.assertTrue(_strict_json_ok('{"language": "python", "code": "x"}'))
        # And explicitly record the hazard this test file had to route around.
        import json as _json
        patched = getattr(_json.loads, "__module__", "") == "json"
        self.assertFalse(
            _strict_json_ok("{"),
            "the strict checker must reject truncated JSON",
        )
        sys.stderr.write(
            "[info] json.loads is %s in this session\n"
            % ("PATCHED by profiles/default.py" if not patched else "stdlib")
        )

    def test_a_clean_chain_has_no_malformed_arguments(self):
        messages = [
            _user_row("list the folders"),
            _code_row("import os"),
            _result_row("Desktop"),
            _code_row("print(1)"),
            _result_row("1"),
        ]
        self.assertEqual(_malformed_arguments(messages), [])

    def test_the_defect_also_hits_a_mid_chain_interposition(self):
        """Not specific to the last tool - any interposed row does it."""
        messages = [
            _user_row("go"),
            _code_row("a"),
            _result_row("1"),
            _code_row("b"),
            _nudge_row(),
            _result_row("2"),
        ]
        self.assertEqual(len(_malformed_arguments(messages)), 1)

    def test_a_plain_user_row_interposed_is_equally_fatal(self):
        """The nudge is not special; POSITION is the invariant."""
        messages = [_code_row("a"), _user_row("are you there?"), _result_row("1")]
        self.assertEqual(len(_malformed_arguments(messages)), 1)


# ---------------------------------------------------------------------------
# 2 + 3. The production fix produces a SAFE history.
# ---------------------------------------------------------------------------


class ProductionNudgePlacementTests(unittest.TestCase):
    def test_nudge_lands_after_the_tool_result_not_before_it(self):
        turn = _Turn()
        turn.user("list the folders then name three of them")
        turn.call()
        nudge = _run_tool_turn(turn, 13, cap=12, deliver=False)
        self.assertIsNotNone(nudge, "the tool-cap guard must fire on tool #12")
        # Now the next real LLM call happens - the flush point.
        turn.call()
        roles = [m.get("role") for m in turn.messages]
        self.assertEqual(roles[-3:], ["assistant", "function", "user"])
        self.assertEqual(roles[-1], "user", "the nudge must be LAST")
        self.assertEqual(turn.messages[-1]["content"], nudge)
        self.assertEqual(
            _malformed_arguments(turn.messages),
            [],
            "the production history must reconstruct with ZERO malformed args",
        )

    def test_no_tool_call_is_ever_orphaned_by_the_production_flow(self):
        turn = _Turn()
        turn.user("do a long chain")
        turn.call()
        _run_tool_turn(turn, 13, cap=12, deliver=False)
        turn.call()
        processed = process_messages([dict(m) for m in turn.messages])
        # Every assistant tool call must be followed by its own tool result.
        for index, message in enumerate(processed[:-1]):
            if message.get("tool_calls"):
                self.assertEqual(
                    processed[index + 1].get("role"),
                    "tool",
                    "tool call at %d is not followed by a tool result" % index,
                )

    def test_the_delivered_request_carries_the_nudge(self):
        turn = _Turn()
        turn.user("long chain")
        turn.call()
        nudge = _run_tool_turn(turn, 13, cap=12, deliver=False)
        _, params = turn.call()
        delivered = [
            m for m in params["messages"]
            if m.get("content") == nudge
        ]
        self.assertTrue(
            delivered,
            "the nudge must be on the request that is actually sent, not just "
            "in history a call later (respond.py copies messages at :63)",
        )

    def test_pending_is_cleared_after_delivery(self):
        turn = _Turn()
        turn.user("long chain")
        turn.call()
        _run_tool_turn(turn, 13, cap=12, deliver=False)
        turn.call()
        self.assertIsNone(turn.state["pending_tool_cap_nudge"])

    def test_the_nudge_is_delivered_only_once(self):
        turn = _Turn()
        turn.user("long chain")
        turn.call()
        nudge = _run_tool_turn(turn, 13, cap=12, deliver=False)
        turn.call()
        turn.console("after")
        turn.call()
        occurrences = [m for m in turn.messages if m.get("content") == nudge]
        self.assertEqual(len(occurrences), 1, "the nudge must not be duplicated")


# ---------------------------------------------------------------------------
# 4 + 5. The guard still fires and the nudge still reaches the model.
# ---------------------------------------------------------------------------


class NudgeStillFiresTests(unittest.TestCase):
    def test_the_cap_still_fires_at_twelve_tools(self):
        turn = _Turn(tool_cap=12)
        turn.user("long chain")
        turn.call()
        self.assertIsNone(turn.fire_tool_cap_nudge())
        for index in range(11):
            turn.ns["tool_counter"]["n"] += 1
            self.assertIsNone(
                turn.fire_tool_cap_nudge(), "fired early at tool #%d" % (index + 1)
            )
        turn.ns["tool_counter"]["n"] = 12
        self.assertIsNotNone(turn.fire_tool_cap_nudge(), "must fire at tool #12")

    def test_a_fix_that_disables_the_guard_would_fail(self):
        """If the nudge were never created, nothing above would hold."""
        turn = _Turn(tool_cap=12)
        turn.user("long chain")
        turn.call()
        self.assertIsNone(_run_tool_turn(turn, 11, cap=12))
        self.assertIsNotNone(_run_tool_turn(turn, 1, cap=12, deliver=False))

    def test_the_nudge_text_is_delivered_verbatim(self):
        turn = _Turn()
        turn.user("long chain")
        turn.call()
        nudge = _run_tool_turn(turn, 13, cap=12, deliver=False)
        turn.call()
        self.assertIn(_NUDGE_MARKER, turn.messages[-1]["content"])
        self.assertEqual(turn.messages[-1]["content"], nudge)

    def test_delivery_is_logged_and_traced(self):
        turn = _Turn()
        turn.user("long chain")
        turn.call()
        _run_tool_turn(turn, 13, cap=12, deliver=False)
        turn.call()
        self.assertIn("nudge delivered", turn.log_text)
        self.assertIn("tool_cap_nudge_delivered", turn.traces)


# ---------------------------------------------------------------------------
# 6. No cross-turn leak.
# ---------------------------------------------------------------------------


class NoCrossTurnLeakTests(unittest.TestCase):
    def test_a_pending_nudge_is_discarded_when_the_next_turn_starts(self):
        turn = _Turn()
        turn.user("long chain that dies")
        turn.call()
        nudge = _run_tool_turn(turn, 13, cap=12, deliver=False)
        self.assertIsNotNone(nudge)
        self.assertIsNotNone(turn.state["pending_tool_cap_nudge"])
        # Turn ends with NO further LLM call. A new user turn begins.
        turn.user("unrelated question")
        _, params = turn.call()
        leaked = [m for m in params["messages"] if m.get("content") == nudge]
        self.assertEqual(
            leaked, [], "the stale nudge leaked into an unrelated new turn"
        )
        self.assertIsNone(turn.state["pending_tool_cap_nudge"])

    def test_the_stale_nudge_never_enters_history_either(self):
        turn = _Turn()
        turn.user("long chain that dies")
        turn.call()
        nudge = _run_tool_turn(turn, 13, cap=12, deliver=False)
        turn.user("unrelated question")
        turn.call()
        self.assertNotIn(nudge, [m.get("content") for m in turn.messages])

    def test_discard_is_logged_and_traced(self):
        turn = _Turn()
        turn.user("dies")
        turn.call()
        _run_tool_turn(turn, 13, cap=12, deliver=False)
        turn.user("next turn")
        turn.call()
        self.assertIn("discarded", turn.log_text)
        self.assertIn("tool_cap_nudge_discarded", turn.traces)

    def test_consecutive_long_turns_do_not_accumulate_nudges(self):
        turn = _Turn()
        seen = []
        for index in range(3):
            turn.user("turn %d" % index)
            turn.call()
            seen.append(_run_tool_turn(turn, 13, cap=12, deliver=False))
            turn.user("follow-up %d" % index)
            turn.call()
        live = [m.get("content") for m in turn.messages]
        for nudge in seen:
            self.assertLessEqual(live.count(nudge), 1)


# ---------------------------------------------------------------------------
# 7. 13+ tool regression, end to end.
# ---------------------------------------------------------------------------


class LongToolTurnTests(unittest.TestCase):
    def test_thirteen_tool_turn_delivers_the_nudge_and_completes(self):
        turn = _Turn()
        turn.user("do a very long chain")
        turn.call()
        nudge = _run_tool_turn(turn, 13, cap=12, deliver=False)
        self.assertIsNotNone(nudge, "guard must fire")
        chunks, _ = turn.call()
        self.assertTrue(chunks, "the post-nudge call must still reach the provider")
        self.assertEqual(turn.shapes[-1]["chunks"], 1)
        self.assertEqual(
            _malformed_arguments(turn.messages),
            [],
            "a 13-tool turn must leave reconstructable history",
        )

    def test_the_next_turn_still_works_after_a_long_turn(self):
        turn = _Turn()
        turn.user("long chain")
        turn.call()
        _run_tool_turn(turn, 13, cap=12, deliver=False)
        turn.call()  # flush: nudge is delivered, last message is the nudge
        # The nudge permits ONE further attempt, so the model emits a normal
        # tool call (which brings its own result) and then wraps up in text.
        # A bare console row directly after the nudge is NOT a shape OI can
        # produce - a result always follows a call.
        turn.assistant_code("print('final attempt')")
        turn.console("final output")
        chunks, _ = turn.call()
        self.assertTrue(
            chunks,
            "the ONE further attempt the nudge permits must reach the provider",
        )
        self.assertEqual(
            _malformed_arguments(turn.messages),
            [],
            "history must be reconstructable mid-recovery",
        )
        turn.user("and now, what is 2+2?")
        chunks, params = turn.call()
        self.assertTrue(chunks, "the follow-up turn must reach the provider")
        self.assertEqual(
            _malformed_arguments(turn.messages),
            [],
            "history must stay reconstructable for the NEXT turn too",
        )

    def test_the_guard_rearms_for_a_second_turn_in_the_same_process(self):
        """ZB-L4-3: a later independent turn gets its own advisory nudge."""
        turn = _Turn()
        turn.user("long chain")
        turn.call()
        self.assertIsNotNone(_run_tool_turn(turn, 13, cap=12, deliver=False))
        turn.call()
        turn.user("another long chain")
        turn.call()
        for index in range(13):
            turn.ns["tool_counter"]["n"] += 1
            second = turn.fire_tool_cap_nudge()
        self.assertIsNotNone(second, "the next turn must re-arm the nudge")


# ---------------------------------------------------------------------------
# 8 + 9. Regressions: tool chaining, and Round O's guard.
# ---------------------------------------------------------------------------


class ToolChainingRegressionTests(unittest.TestCase):
    def test_post_tool_calls_are_never_capped(self):
        turn = _Turn()
        turn.user("open calculator then tell me 2+2")
        turn.call()
        for _ in range(6):
            turn.assistant_code("step")
            turn.console("done")
            chunks, _ = turn.call()
            self.assertEqual(len(chunks), 1, "tool steps must keep streaming")
            self.assertIsNone(turn.shapes[-1].get("guard"))

    def test_multi_step_chain_runs_to_completion(self):
        turn = _Turn()
        turn.user("rename three files one after another")
        turn.call()
        steps = 0
        for index in range(5):
            turn.assistant_code("rename %d" % index)
            turn.console("renamed %d" % index)
            chunks, _ = turn.call()
            if chunks:
                steps += 1
        self.assertEqual(steps, 5)
        self.assertEqual(_malformed_arguments(turn.messages), [])


class RoundORegressionTests(unittest.TestCase):
    def test_loop_guard_cap_is_still_one(self):
        turn = _Turn()
        self.assertEqual(turn.ns["LOOP_GUARD_CAP"], 1)

    def test_second_text_only_call_is_still_capped(self):
        turn = _Turn()
        turn.user("what is 17 times 23?")
        self.assertEqual(len(turn.call()[0]), 1)
        turn.interpreter.messages.append(
            {"role": "assistant", "type": "message", "content": "391"}
        )
        chunks, _ = turn.call()
        self.assertEqual(chunks, [], "Round O behaviour must be intact")
        self.assertEqual(turn.shapes[-1].get("guard"), "cap_end")

    def test_a_deferred_nudge_does_not_disturb_the_text_loop_guard(self):
        turn = _Turn()
        turn.user("hi")
        turn.call()
        turn.interpreter.messages.append(
            {"role": "assistant", "type": "message", "content": "Yes, I'm here."}
        )
        chunks, _ = turn.call()
        self.assertEqual(chunks, [])
        self.assertEqual(turn.shapes[-1].get("guard"), "plain_end")


# ---------------------------------------------------------------------------
# Contract on the production source itself.
# ---------------------------------------------------------------------------


class NudgeHandlerContractTests(unittest.TestCase):
    def _tool_cap_block(self):
        """The real `if tool_counter >= cap:` block, located structurally."""
        with open(SERVER_PY, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        start_server = _find_start_server(tree)
        target = None
        for node in ast.walk(start_server):
            if not isinstance(node, ast.If):
                continue
            rendered = ast.unparse(node.test)
            if "tool_cap" in rendered and "tool_cap_nudged" in rendered:
                target = node
                break
        self.assertIsNotNone(target, "the tool-cap guard `if` was not found")
        return source, target

    def test_the_tool_cap_handler_never_writes_to_message_history(self):
        """STRUCTURAL: no executable write to interpreter.messages in the block.

        Done on the AST rather than by text search, so the explanatory comment
        (which legitimately names interpreter.messages) cannot mask a real
        regression, and a re-introduced append cannot hide behind a string.
        """
        _, block = self._tool_cap_block()
        offenders = []
        for node in ast.walk(block):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "append":
                continue
            target = func.value
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "messages"
                and isinstance(target.value, ast.Name)
                and target.value.id == "interpreter"
            ):
                offenders.append(ast.unparse(node))
        self.assertEqual(
            offenders,
            [],
            "the tool-cap handler must not append to interpreter.messages; "
            "that position orphans the pending tool result: %r" % offenders,
        )

    def test_the_tool_cap_handler_stores_the_nudge_instead(self):
        source, block = self._tool_cap_block()
        stashed = [
            node
            for node in ast.walk(block)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Subscript)
                and isinstance(t.slice, ast.Constant)
                and t.slice.value == "pending_tool_cap_nudge"
                for t in node.targets
            )
        ]
        self.assertTrue(stashed, "the handler must stash the nudge")

    def test_the_flush_lives_in_traced_completions(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            source = handle.read()
        node = _statement_for(_find_start_server(ast.parse(source)).body, "traced_completions")
        lines = source.splitlines()
        body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        self.assertIn("pending_tool_cap_nudge", body)
        self.assertIn("tool_cap_nudge_delivered", body)
        self.assertIn("tool_cap_nudge_discarded", body)

    def test_pending_state_is_declared_in_server_state(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            source = handle.read()
        node = _statement_for(
            _find_start_server(ast.parse(source)).body, "server_state"
        )
        self.assertIsNotNone(node)
        lines = source.splitlines()
        block = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        self.assertIn('"pending_tool_cap_nudge": None', block)

    def test_every_required_production_node_is_found(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        body = _find_start_server(tree).body
        for name in _REQUIRED_NODES:
            self.assertIsNotNone(_statement_for(body, name), name)

    def test_the_harness_executes_real_production_source(self):
        ns = _load_production_guard()
        block = ns["__source__"]
        for needle in (
            "def traced_completions",
            "_pending_nudge = server_state.get(\"pending_tool_cap_nudge\")",
            "tool_cap_nudge_delivered",
            "tool_cap_nudge_discarded",
        ):
            self.assertIn(needle, block, "guard source missing %r" % needle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
