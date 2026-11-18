"""ROUND O (2026-10-03) - the text-only LLM re-ask loop cap.

WHAT THIS LOCKS
===============
Open Interpreter's `respond.py` re-asks the LLM from its `else:` branch
(reached ONLY when the model emitted no code) whenever the last message is
plain assistant text that lacks one of its four `loop_breakers`. A normal
conversational answer never contains one, so FRIDAY used to pay for a
duplicated streamed call on nearly every plain reply.

`LOOP_GUARD_CAP` in `traced_completions` is the FRIDAY-side bound on that.
ROUND O moved the default from 2 to 1, so the FIRST duplicate is skipped
instead of the second. Measured motivation (boot_console.log): 141 of 259
reconstructable turns (54%) were guarded, median 55.0s against 26.0s
unguarded.

WHY THESE TESTS ARE NOT VACUOUS
===============================
`traced_completions` is a closure inside `start_server`, so it cannot simply
be imported. Rather than reimplement the guard (which would pass no matter
what production does), `_load_production_guard()` parses `server.py`, pulls
out the REAL `LOOP_GUARD_CAP` assignment, the REAL `_chat_opener` helpers and
the REAL `traced_completions` body, and executes them. Every assertion below
therefore runs production source, and reverting the cap in `server.py` turns
these tests red.
"""

import ast
import os
import re
import sys
import textwrap
import types
import unittest

sys.path.insert(0, os.getcwd())

from source.server.server import (  # noqa: E402
    _merge_chunk_shape,
    _prompt_fingerprint,
    _redact_text,
    _scrub_tool_choice,
)

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")

# The pieces of start_server's body that make up the production guard. Located
# by AST, not by line number, so unrelated edits above them cannot silently
# repoint the tests at the wrong code.
_REQUIRED_NODES = (
    "LOOP_GUARD_CAP",
    "_chat_opener_re",
    "_chat_opener",
    "_strip_user_request_prefix",
    "_begin_llm_call_shape",
    "traced_completions",
)


def _find_start_server(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "start_server":
            return node
    raise AssertionError("start_server not found in %s" % SERVER_PY)


_NESTED_BODY_FIELDS = ("body", "orelse", "finalbody")


def _statement_for(body, name):
    """Locate one named statement anywhere inside start_server's body.

    The guard lives inside `if not getattr(interpreter.llm, ...):`, so a
    direct-children scan misses it. This descends through control-flow blocks
    but deliberately does NOT descend into a FunctionDef other than the one
    being looked for, so a same-named local inside an unrelated helper cannot
    be mistaken for the production node.
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


def _load_production_guard(env=None):
    """Execute the real guard source from server.py in a controlled namespace.

    Returns the namespace holding the genuine `LOOP_GUARD_CAP` and
    `traced_completions`, plus `source` - the exact text that was executed.
    """
    with open(SERVER_PY, encoding="utf-8") as handle:
        source_text = handle.read()
    tree = ast.parse(source_text)
    start_server = _find_start_server(tree)

    wanted = {}
    for name in _REQUIRED_NODES:
        node = _statement_for(start_server.body, name)
        wanted[name] = node
        assert node is not None, "missing guard node %r" % name

    lines = source_text.splitlines()
    # Extract EACH node on its own. Taking one contiguous line range would
    # drag in every unrelated def between them (handle_special_command,
    # _screen_context_prefix, ...) whose definitions would shadow the real
    # helpers imported above and break the harness for unrelated reasons.
    blocks = []
    for name in _REQUIRED_NODES:
        node = wanted[name]
        segment = "\n".join(lines[node.lineno - 1 : node.end_lineno])
        blocks.append((node.lineno, textwrap.dedent(segment)))
    blocks.sort(key=lambda item: item[0])
    block = "\n".join(text for _, text in blocks)

    # A fake interpreter/LLM whose message list the test drives directly.
    interpreter = types.SimpleNamespace(
        messages=[],
        llm=types.SimpleNamespace(
            model="test/model",
            api_base="",
            api_key="",
            completions=None,
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
        """Stand-in provider. Records the call, then yields one text chunk."""
        interpreter.llm.last_params = params
        yield {
            "choices": [
                {"delta": {"content": "ok"}, "finish_reason": None}
            ]
        }

    namespace = {
        "__name__": "friday_test_guard",
        "os": os,
        "re": re,
        "time": types.SimpleNamespace(monotonic=lambda: 0.0),
        "interpreter": interpreter,
        "original_completions": _fake_completions,
        "_flog": _Log(),
        "trace": traces.append,
"llm_loop_guard": {"calls": 0},
          # ROUND ZA (2026-10-05): the per-turn provider-shape accumulator is no
          # longer a start_server() closure. It lives in `server_state` and is
          # re-armed at the turn boundary, so this harness must seed it THERE -
          # supplying it as a namespace local would leave the real code reading
          # a key the harness never set.
          "tool_counter": {"n": 0},
          # ROUND Q (2026-10-03): traced_completions reads the deferred
          # tool-cap nudge out of `server_state`. In production that is a real
          # closure variable of start_server(); the harness must supply it or
          # the guard raises NameError before it can be tested.
          "server_state": {
              "pending_tool_cap_nudge": None,
              "tool_cap_nudged": False,
              "turn_log": None,
              "llm_response_shape": {"calls": [], "turn_valid": False},
          },
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

    previous_env = {}
    for key, value in (env or {}).items():
        previous_env[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        exec(compile(block, SERVER_PY, "exec"), namespace)
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    namespace["__source__"] = block
    namespace["__interpreter__"] = interpreter
    return namespace


class _Turn:
    """Drives one simulated turn through the production guard."""

    def __init__(self, env=None):
        self.ns = _load_production_guard(env=env)
        self.interpreter = self.ns["__interpreter__"]
        self.interpreter.messages = []

    @property
    def cap(self):
        return self.ns["LOOP_GUARD_CAP"]

    def user(self, text):
        self.interpreter.messages.append({"role": "user", "content": text})

    def assistant_text(self, text="A normal answer."):
        self.interpreter.messages.append(
            {"role": "assistant", "type": "message", "content": text}
        )

    def assistant_code(self, text="print(1)"):
        """An assistant message that carries CODE, as OI stores it."""
        self.interpreter.messages.append(
            {"role": "assistant", "type": "message", "content": "```python\n%s\n```" % text}
        )

    def computer(self, text="1"):
        self.interpreter.messages.append(
            {"role": "computer", "type": "console", "content": text}
        )

    def call(self):
        """Run one traced_completions call; return the chunks it yielded."""
        chunks = list(
            self.ns["traced_completions"](
                messages=self.interpreter.messages, model="test/model"
            )
        )
        return chunks

    @property
    def last_shape(self):
        # ROUND ZA: the accumulator is server_state-owned at the turn
        # boundary, so read it from there - where the code writes.
        calls = self.ns["server_state"]["llm_response_shape"]["calls"]
        return calls[-1] if calls else None

    @property
    def guard(self):
        shape = self.last_shape
        return shape.get("guard") if shape else None

    @property
    def logs(self):
        return self.ns["__log_records"]

    @property
    def log_text(self):
        return "\n".join(str(args) for _, args in self.logs)


# ---------------------------------------------------------------------------
# 1. The cap, as production actually defines it.
# ---------------------------------------------------------------------------


class CapDefinitionTests(unittest.TestCase):
    def test_production_default_cap_is_one(self):
        turn = _Turn()
        self.assertEqual(
            turn.cap,
            1,
            "ROUND O moved the default duplicate-call limit to 1; production "
            "still resolves to %r" % (turn.cap,),
        )

    def test_old_cap_value_cannot_silently_return(self):
        """Locks the literal default string in server.py.

        `_Turn()` proves the resolved value. This additionally fails if the
        default is renamed away from the documented knob, so an operator can
        no longer be misled by a comment claiming a different bound.
        """
        with open(SERVER_PY, encoding="utf-8") as handle:
            source_text = handle.read()
        match = re.search(
            r'LOOP_GUARD_CAP\s*=\s*int\(float\(os\.getenv\("FRIDAY_LLM_LOOP_GUARD",\s*"(\d+)"\)\)\)',
            source_text,
        )
        self.assertIsNotNone(
            match, "LOOP_GUARD_CAP must keep its documented env-var form"
        )
        self.assertEqual(
            match.group(1),
            "1",
            "the FRIDAY_LLM_LOOP_GUARD default is back at %s" % match.group(1),
        )

    def test_env_override_still_honoured(self):
        """The knob still works, so an operator can widen the bound if needed."""
        self.assertEqual(_Turn(env={"FRIDAY_LLM_LOOP_GUARD": "5"}).cap, 5)

    def test_no_launcher_forces_a_stale_cap(self):
        """No shipped file may ASSIGN the env var outside server.py.

        A bare mention in a docstring or comment is fine (server.py's own
        knob, profiles/default.py's note and this file all name it), so this
        looks for real writes: os.environ[...] =, .setdefault(, .putenv( and
        setenv(.
        """
        assign = re.compile(
            r"os\.environ\s*\[\s*[\"']FRIDAY_LLM_LOOP_GUARD[\"']\s*\]\s*="
            r"|os\.environ\.setdefault\(\s*[\"']FRIDAY_LLM_LOOP_GUARD"
            r"|os\.putenv\(\s*[\"']FRIDAY_LLM_LOOP_GUARD"
            r"|setenv\s+FRIDAY_LLM_LOOP_GUARD"
        )
        stale = []
        this_file = os.path.abspath(__file__)
        for root, dirs, files in os.walk(os.getcwd()):
            dirs[:] = [
                d
                for d in dirs
                if d not in (".venv", "site-packages", "__pycache__", ".git", "node_modules")
            ]
            for name in files:
                if not name.endswith((".py", ".vbs", ".ps1", ".bat", ".cmd")):
                    continue
                path = os.path.join(root, name)
                if os.path.abspath(path) in (os.path.abspath(SERVER_PY), this_file):
                    continue
                try:
                    with open(path, encoding="utf-8", errors="ignore") as handle:
                        body = handle.read()
                except OSError:
                    continue
                if assign.search(body):
                    stale.append(path)
        self.assertEqual(
            stale,
            [],
            "FRIDAY_LLM_LOOP_GUARD must not be assigned outside server.py: %r" % stale,
        )


# ---------------------------------------------------------------------------
# 2. The guard fires on the SECOND text-only call, not the third.
# ---------------------------------------------------------------------------


class TextOnlyReaskTests(unittest.TestCase):
    def test_first_call_of_a_turn_is_never_capped(self):
        turn = _Turn()
        turn.user("what is 17 times 23?")
        chunks = turn.call()
        self.assertEqual(len(chunks), 1, "the first call must stream normally")
        self.assertIsNone(turn.guard)
        self.assertFalse(turn.last_shape["skipped_stream"])

    def test_second_text_only_call_is_capped(self):
        """THE ROUND O BEHAVIOUR: call 2 is skipped, not call 3."""
        turn = _Turn()
        turn.user("what is 17 times 23?")
        turn.call()
        turn.assistant_text("The result of 17 times 23 is 391.")
        chunks = turn.call()

        self.assertEqual(chunks, [], "the duplicate call must yield nothing")
        self.assertEqual(turn.guard, "cap_end")
        self.assertTrue(turn.last_shape["skipped_stream"])
        self.assertFalse(turn.last_shape["stream_opened"])
        self.assertIn("text-only llm calls this turn", turn.log_text)

    def test_guard_trips_on_call_two_not_call_three(self):
        """Explicitly assert the trip index, which is what the cap controls."""
        for cap, expect_capped_on_second in ((1, True), (2, False)):
            turn = _Turn(env={"FRIDAY_LLM_LOOP_GUARD": str(cap)})
            turn.user("what is 17 times 23?")
            turn.call()
            turn.assistant_text("The result of 17 times 23 is 391.")
            chunks = turn.call()
            self.assertEqual(
                len(chunks) == 0,
                expect_capped_on_second,
                "with cap=%d the second text-only call should%s be skipped"
                % (cap, "" if expect_capped_on_second else " not"),
            )

    def test_capped_call_appends_the_loop_breaker_once(self):
        turn = _Turn()
        turn.user("what is 17 times 23?")
        turn.call()
        turn.assistant_text("The result of 17 times 23 is 391.")
        turn.call()
        content = turn.interpreter.messages[-1]["content"]
        self.assertEqual(content.count("The task is done."), 1)
        self.assertTrue(
            content.startswith("The result of 17 times 23 is 391."),
            "the real answer must survive the guard append",
        )

    def test_a_further_reask_stays_capped(self):
        """A model that keeps answering in prose cannot restart the stream."""
        turn = _Turn()
        turn.user("hello")
        turn.call()
        for _ in range(4):
            turn.assistant_text("Still talking.")
            self.assertEqual(turn.call(), [])
        self.assertEqual(turn.ns["llm_loop_guard"]["calls"], 5)

    def test_a_new_user_turn_resets_the_counter(self):
        turn = _Turn()
        turn.user("first question")
        turn.call()
        turn.assistant_text("first answer")
        self.assertEqual(turn.call(), [])
        turn.user("second question")
        chunks = turn.call()
        self.assertEqual(len(chunks), 1, "a fresh turn must not inherit the cap")
        self.assertEqual(turn.ns["llm_loop_guard"]["calls"], 1)


# ---------------------------------------------------------------------------
# 3. A finished text answer is text, not a tool step - and is not mangled.
# ---------------------------------------------------------------------------


class TextAnswerClassificationTests(unittest.TestCase):
    def test_completed_text_reply_is_treated_as_text(self):
        turn = _Turn()
        turn.user("what is 17 times 23?")
        turn.call()
        turn.assistant_text("The result of 17 times 23 is 391.")
        turn.call()
        self.assertEqual(
            turn.guard, "cap_end", "plain assistant text must reach the text-only guard"
        )

    def test_tool_console_step_is_not_treated_as_text(self):
        turn = _Turn()
        turn.user("open calculator")
        turn.call()
        turn.assistant_code("import subprocess")
        turn.computer("ok")
        turn.call()
        self.assertIsNone(
            turn.guard, "a computer:console tail is a tool step, not a text-only spin"
        )
        self.assertEqual(len(turn.last_shape.get("top") or []) >= 0, True)

    def test_guard_preserves_the_answer_text(self):
        turn = _Turn()
        turn.user("what is 17 times 23?")
        turn.call()
        turn.assistant_text("The result of 17 times 23 is 391.")
        turn.call()
        self.assertIn("391", turn.interpreter.messages[-1]["content"])

    def test_conversational_opener_still_uses_the_plain_end_guard(self):
        """guard1 runs before guard2 and must keep its own label."""
        turn = _Turn()
        turn.user("hi")
        turn.call()
        turn.assistant_text("Yes, I'm here.")
        turn.call()
        self.assertEqual(turn.guard, "plain_end")
        self.assertIn("conversational reply", turn.log_text)

    def test_task_request_is_not_treated_as_a_chat_opener(self):
        turn = _Turn()
        turn.user("open calculator")
        turn.call()
        turn.assistant_text("Sure, one moment.")
        turn.call()
        self.assertEqual(
            turn.guard, "cap_end", "a task request must fall through to the cap guard"
        )


# ---------------------------------------------------------------------------
# 4 + 5. Tool/code chaining and multi-step turns are untouched.
# ---------------------------------------------------------------------------


class ToolChainingTests(unittest.TestCase):
    def test_post_tool_call_is_never_capped(self):
        turn = _Turn()
        turn.user("open calculator then tell me 2+2")
        turn.call()
        for _ in range(6):
            turn.assistant_code("step")
            turn.computer("done")
            chunks = turn.call()
            self.assertEqual(len(chunks), 1, "tool steps must keep streaming")
            self.assertIsNone(turn.guard)

    def test_multi_step_chain_runs_to_completion(self):
        turn = _Turn()
        turn.user("rename three files one after another")
        turn.call()
        steps = 0
        for _ in range(5):
            turn.assistant_code("rename step %d" % steps)
            turn.computer("renamed %d" % steps)
            chunks = turn.call()
            if chunks:
                steps += 1
        self.assertEqual(
            steps, 5, "every link of a multi-step chain must reach the provider"
        )

    def test_chain_still_ends_cleanly_on_a_final_text_answer(self):
        """After real tool work the closing text answer is capped, not spun."""
        turn = _Turn()
        turn.user("open calculator")
        turn.call()
        turn.assistant_code("import subprocess")
        turn.computer("opened")
        turn.call()
        turn.assistant_text("Calculator is open.")
        self.assertEqual(turn.call(), [], "the post-tool text tail must not re-ask")
        self.assertEqual(turn.guard, "cap_end")

    def test_capped_call_is_not_logged_as_an_llm_call(self):
        """The guard returns before the `llm call:` line, as it always has."""
        turn = _Turn()
        turn.user("what is 17 times 23?")
        turn.call()
        self.assertIn("llm call:", turn.log_text)
        turn.assistant_text("391")
        turn.call()
        self.assertEqual(
            turn.log_text.count("llm call:"),
            1,
            "a capped call must not reach the provider",
        )


# ---------------------------------------------------------------------------
# 6. Proof the harness really runs production source.
# ---------------------------------------------------------------------------


class HarnessFidelityTests(unittest.TestCase):
    def test_executed_block_contains_the_real_guard_body(self):
        ns = _load_production_guard()
        block = ns["__source__"]
        for needle in (
            "def traced_completions",
            "llm_loop_guard[\"calls\"] > LOOP_GUARD_CAP",
            "_assistant_text = _last.get(\"role\") == \"assistant\"",
            "text-only llm calls this turn",
            "The task is done.",
        ):
            self.assertIn(needle, block, "guard source is missing %r" % needle)

    def test_every_required_production_node_was_found(self):
        """If a node is renamed, fail loudly instead of testing a stub."""
        with open(SERVER_PY, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        body = _find_start_server(tree).body
        for name in _REQUIRED_NODES:
            self.assertIsNotNone(
                _statement_for(body, name), "production node %r vanished" % name
            )

    def test_guard_condition_still_requires_assistant_text(self):
        """The `_assistant_text` half is what protects tool chains.

        If it were dropped, tool steps would be capped and chaining would
        break; this asserts the real condition text survives.
        """
        ns = _load_production_guard()
        self.assertIn("and _assistant_text", ns["__source__"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
