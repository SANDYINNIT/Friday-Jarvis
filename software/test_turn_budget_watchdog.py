"""ROUND W (2026-10-04) - the turn/time budget architecture, corrected.

WHAT WAS WRONG (all measured, Round V)
======================================
1. THERE WAS NO 120-SECOND MODEL LIMIT. Modal and local Ollama both reach
   litellm through the same path, which carries a 6000 s client timeout and
   `LITELLM_MAX_STREAMING_DURATION_SECONDS = None` (streams are never killed).
   FRIDAY itself answers "about 120 seconds" in her notes; the only
   `MAX_TIMEOUT_SECONDS = 120.0` constants live in delegation.py, mcp_runtime.py
   and n8n_runtime.py and govern none of this.

2. `TOOL_WALL_CLOCK_LIMIT` (240 s) was DOCUMENTED AND USED as a runaway-script
   defence but was really a whole-turn budget. It fired live on 2026-10-04
   13:53:26 against a 247 s turn of 12 tools that each completed in 0-77 ms,
   and FRIDAY told Sir "the code was looping instead of finishing" - false.

3. It was evaluated ONLY inside the console-output handler, so a turn that hung
   SILENTLY was never checked at all - the exact failure it was written for.

4. `watchdog_loop` called `getattr(interpreter, "stop", None)`. That attribute
   does not exist (`hasattr(AsyncInterpreter, "stop")` is False), so the
   cancellation branch never ran. The watchdog only relabelled turns.

5. `last_progress` was stamped per-channel and incompletely, so a fresh turn
   could be judged against a stamp from 21 minutes earlier ("no progress for
   1275s", one second into a healthy turn).

THE FIX
=======
* `_stamp_progress()` - one helper, used at every turn boundary and on every
  output chunk, so the watchdog clock is always re-armed by real work.
* `_stop_active_turn()` - cancels via `interpreter.stop_event.set()`, the
  mechanism that actually reaches `km.interrupt_kernel()`.
* `TURN_WALL_CLOCK_LIMIT` (default 600 s) - an honestly-named, finite TURN
  budget, enforced from the watchdog tick rather than from console output.
* `SILENT_HANG_LIMIT` (default 900 s) - catches a turn with no output at all.
* `runtime_limits()` - FRIDAY can finally report her real limits.

SAFETY IS NOT WEAKENED: `HARD_TOOL_CAP` (24) and `TOOL_FLOOD_EVENT_CAP` (300
identical repeats) are untouched, and `stop_event` still interrupts the kernel.

These tests drive the REAL functions. `_extract` pulls the production source out
of `server.py` with `ast` and executes it, so reverting any fix in `server.py`
turns them red.
"""

import ast
import os
import sys
import textwrap
import time
import types
import unittest

sys.path.insert(0, os.getcwd())

from source.server.self_awareness import limitations, runtime_limits  # noqa: E402

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")
# Derived from SERVER_PY at IMPORT time on purpose. Do NOT use os.getcwd() inside
# a test body: another test in the full suite changes the working directory to a
# temp dir, which made these two tests fail only in a full run and pass alone.
AWARENESS_PY = os.path.join(os.path.dirname(SERVER_PY), "self_awareness.py")
BRAIN_ROUTER_PY = os.path.join(os.path.dirname(SERVER_PY), "brain_router.py")


def _server_source():
    with open(SERVER_PY, encoding="utf-8") as handle:
        return handle.read()


def _find_start_server(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "start_server":
            return node
    raise AssertionError("start_server not found")


def _node_source(source_lines, node):
    return textwrap.dedent(
        "\n".join(source_lines[node.lineno - 1 : node.end_lineno])
    )


def _extract(name):
    """Return the real source of a module-level function in server.py."""
    source = _server_source()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return _node_source(source.splitlines(), node)
    raise AssertionError("function %r not found at module level" % name)


def _extract_inside(name):
    """Return the real source of a nested def inside start_server."""
    source = _server_source()
    lines = source.splitlines()
    tree = ast.parse(source)
    start_server = _find_start_server(tree)
    for node in ast.walk(start_server):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return _node_source(lines, node)
    raise AssertionError("nested def %r not found in start_server" % name)


class _StopEvent:
    """Stand-in for interpreter.stop_event - a real threading.Event would do."""

    def __init__(self):
        self.was_set = False

    def set(self):
        self.was_set = True

    def is_set(self):
        return self.was_set


class WatchdogHarness:
    """Execute the real _stop_active_turn / _turn_budget_check against a fake
    interpreter and server_state."""

    def __init__(self, *, turn_started_at=None, last_progress=None,
                 progress_observed=False, active=True, elapsed=0.0, silent=0.0):
        source = _extract("_stamp_progress")
        stop_src = _extract_inside("_stop_active_turn")
        budget_src = _extract_inside("_turn_budget_check")

        self.stop_event = _StopEvent()
        self.logs = []
        # Deliberately NO `stop` attribute - that is the real interpreter's shape
        # and the whole reason the old watchdog was inert.
        self.interpreter = types.SimpleNamespace(stop_event=self.stop_event)

        class _Log:
            def __init__(inner, sink):
                inner.sink = sink

            def info(inner, *a):
                pass

            def warning(inner, *a):
                inner.sink.append(a)

            def debug(self, *a):
                pass

            def error(self, *a):
                pass

        import time as _time
        now = _time.monotonic()
        self.server_state = {
            "last_progress": (now - silent) if last_progress is None else last_progress,
            "turn_started_at": (
                (now - elapsed) if turn_started_at is None else turn_started_at
            ),
            "turn_progress_observed": progress_observed,
            "is_paused": False,
        }
        self._now = now
        self._active = active
        self._flog = _Log(self.logs)
        self.calls = []

        class _Clock:
            def monotonic(inner):
                return self._now

        namespace = {
            "interpreter": self.interpreter,
            "server_state": self.server_state,
            "time": _Clock(),
            "_flog": self._flog,
            "response_is_active": lambda: self._active,
            "TURN_WALL_CLOCK_LIMIT": self.turn_budget,
            "SILENT_HANG_LIMIT": self.silent_hang,
        }
        # `os` is in scope in the real module; provide it so extracted code
        # that touches it behaves the same.
        namespace["os"] = os
        namespace["__name__"] = "friday_test_roundw"
        exec(compile(source, SERVER_PY, "exec"), namespace)
        exec(compile(textwrap.dedent(stop_src), SERVER_PY, "exec"), namespace)
        exec(compile(textwrap.dedent(budget_src), SERVER_PY, "exec"), namespace)
        self.ns = namespace
        self.stop_active_turn = namespace["_stop_active_turn"]
        self.turn_budget_check = namespace["_turn_budget_check"]

    turn_budget = 600.0
    silent_hang = 300.0


# ---------------------------------------------------------------------------
# 1. The watchdog really cancels.
# ---------------------------------------------------------------------------


class WatchdogCancellationTests(unittest.TestCase):
    def test_watchdog_calls_stop_event_set(self):
        harness = WatchdogHarness()
        self.assertFalse(harness.stop_event.was_set)
        stopped = harness.stop_active_turn("test")
        self.assertTrue(harness.stop_event.was_set,
                        "the watchdog must interrupt the running turn")
        self.assertTrue(stopped)

    def test_the_real_interpreter_has_no_stop_method(self):
        """Why the old watchdog was inert - proved against the real class."""
        from interpreter.core.async_core import AsyncInterpreter
        from interpreter.core.core import OpenInterpreter
        self.assertFalse(hasattr(OpenInterpreter, "stop"))
        self.assertFalse(hasattr(AsyncInterpreter, "stop"))

    def test_no_production_code_calls_interpreter_stop(self):
        """AST check, so the explanatory comment cannot mask a regression."""
        tree = ast.parse(_server_source())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "getattr":
                continue
            args = node.args
            if not args:
                continue
            first = ast.unparse(args[0])
            if first != "interpreter":
                continue
            rendered = ast.unparse(node)
            if '"stop"' in rendered or "'stop'" in rendered:
                offenders.append(rendered)
        self.assertEqual(
            offenders, [],
            "the dead interpreter.stop() dependency must not return: %r" % offenders,
        )

    def test_stop_event_is_the_documented_mechanism(self):
        source = _extract_inside("_stop_active_turn")
        self.assertIn("stop_event", source)
        self.assertIn(".set()", source)


# ---------------------------------------------------------------------------
# 2 + 3 + 4. Progress tracking.
# ---------------------------------------------------------------------------


class ProgressTrackingTests(unittest.TestCase):
    def test_last_progress_is_stamped_at_turn_start(self):
        ns = {"time": time}
        exec(compile(_extract("_stamp_progress"), SERVER_PY, "exec"), ns)
        state = {"last_progress": 0.0}
        ns["_stamp_progress"](state, turn_started=True)
        self.assertGreater(state["last_progress"], 0.0)
        self.assertIn("turn_started_at", state)
        self.assertIs(state["turn_progress_observed"], False)

    def test_a_plain_stamp_does_not_fake_a_turn_boundary(self):
        ns = {"time": time}
        exec(compile(_extract("_stamp_progress"), SERVER_PY, "exec"), ns)
        state = {"last_progress": 0.0}
        ns["_stamp_progress"](state)
        self.assertNotIn("turn_started_at", state)

    def test_every_turn_dispatch_re_arms_the_clock(self):
        """The universal chokepoint must stamp, so no channel can miss it."""
        source = _extract("_mark_turn_dispatch")
        self.assertIn("_stamp_progress", source)
        self.assertIn("turn_started=True", source)

    def test_idle_time_before_a_turn_is_not_counted_as_stall(self):
        """A turn that begins now must never look stale, however long the
        process had been idle first."""
        harness = WatchdogHarness(elapsed=0.0, silent=100000.0)
        self.assertIsNone(
            harness.turn_budget_check(),
            "a turn that just began must not be judged by pre-turn idle time",
        )


# ---------------------------------------------------------------------------
# 5 + 6. The turn budget and the silent-hang budget.
# ---------------------------------------------------------------------------


class TurnBudgetTests(unittest.TestCase):
    def test_default_turn_budget_is_600_seconds(self):
        source = _server_source()
        self.assertIn('TURN_WALL_CLOCK_LIMIT = float(os.getenv("FRIDAY_TURN_WALL_CLOCK", "600"))', source)

    def test_environment_override_is_honoured(self):
        import importlib
        os.environ["FRIDAY_TURN_WALL_CLOCK"] = "123"
        try:
            ns = {}
            src = _server_source()
            # evaluate just the constant line in isolation
            line = [ln for ln in src.splitlines() if "TURN_WALL_CLOCK_LIMIT = float(" in ln][0]
            exec(compile("import os\n" + line.strip(), "<t>", "exec"), ns)
            self.assertEqual(ns["TURN_WALL_CLOCK_LIMIT"], 123.0)
        finally:
            os.environ.pop("FRIDAY_TURN_WALL_CLOCK", None)

    def test_a_healthy_long_turn_over_the_old_240s_is_not_killed(self):
        """THE ROUND W BEHAVIOUR: 300 s of slow-but-working is fine."""
        harness = WatchdogHarness(elapsed=300.0, silent=2.0, progress_observed=True)
        self.assertIsNone(
            harness.turn_budget_check(),
            "a working turn must survive past the old 240 s wall",
        )

    def test_a_turn_past_600s_is_ended(self):
        harness = WatchdogHarness(elapsed=601.0, silent=1.0, progress_observed=True)
        reason = harness.turn_budget_check()
        self.assertIsNotNone(reason)
        self.assertIn("turn budget", reason)

    def test_an_idle_process_is_never_ended(self):
        harness = WatchdogHarness(active=False, elapsed=99999.0)
        self.assertIsNone(harness.turn_budget_check())

    def test_silent_hang_is_detected_without_any_console_output(self):
        """The gap Round W closes: no console output, yet still caught.

        The silent-hang limit is deliberately SHORTER than the turn budget and is
        checked first, so this case is actually reachable - a longer limit would
        have been dead code behind the turn budget.
        """
        harness = WatchdogHarness(
            elapsed=301.0, silent=301.0, progress_observed=False
        )
        reason = harness.turn_budget_check()
        self.assertIsNotNone(reason, "a silent hang must still be detected")
        self.assertIn("silent hang", reason)

    def test_silent_hang_fires_before_the_turn_budget(self):
        """Ordering matters: the stricter, more specific limit must win."""
        harness = WatchdogHarness(
            elapsed=10000.0, silent=10000.0, progress_observed=False
        )
        self.assertIn("silent hang", harness.turn_budget_check())

    def test_the_silent_hang_limit_is_shorter_than_the_turn_budget(self):
        source = _server_source()
        self.assertIn('SILENT_HANG_LIMIT = float(os.getenv("FRIDAY_SILENT_HANG_LIMIT", "300"))', source)
        self.assertIn('TURN_WALL_CLOCK_LIMIT = float(os.getenv("FRIDAY_TURN_WALL_CLOCK", "600"))', source)

    def test_a_working_turn_is_never_judged_silent(self):
        """Once any output arrived, a long turn is governed by the budget alone."""
        harness = WatchdogHarness(
            elapsed=500.0, silent=500.0, progress_observed=True
        )
        self.assertIsNone(harness.turn_budget_check())

    def test_a_silent_but_within_budget_turn_is_left_alone(self):
        harness = WatchdogHarness(
            elapsed=100.0, silent=100.0, progress_observed=False
        )
        self.assertIsNone(harness.turn_budget_check())

    def test_the_budget_check_needs_no_console_output_at_all(self):
        """Structural: the check must NOT live inside the console handler."""
        source = _server_source()
        budget = _extract_inside("_turn_budget_check")
        self.assertIn("TURN_WALL_CLOCK_LIMIT", budget)
        self.assertIn("SILENT_HANG_LIMIT", budget)
        # The console handler must no longer compare elapsed time at all.
        self.assertNotIn(
            "over_time = elapsed > TOOL_WALL_CLOCK_LIMIT", source,
            "the turn budget must not be enforced from the console handler",
        )


# ---------------------------------------------------------------------------
# 11 + 12. Tool safety is NOT weakened.
# ---------------------------------------------------------------------------


class ToolSafetyPreservedTests(unittest.TestCase):
    def test_hard_tool_cap_is_unchanged_at_24(self):
        self.assertIn(
            'HARD_TOOL_CAP = int(float(os.getenv("FRIDAY_HARD_TOOL_CAP", "24")))',
            _server_source(),
        )

    def test_console_flood_cap_is_unchanged_at_300(self):
        self.assertIn(
            'TOOL_FLOOD_EVENT_CAP = int(float(os.getenv("FRIDAY_TOOL_FLOOD_CAP", "300")))',
            _server_source(),
        )

    def test_the_repetition_guard_is_still_enforced(self):
        source = _server_source()
        self.assertIn(
            'over_events = turn_guard["same_run"] > TOOL_FLOOD_EVENT_CAP', source
        )

    def test_the_flood_guard_still_interrupts_the_kernel(self):
        source = _server_source()
        # both kill paths must still set stop_event
        self.assertGreaterEqual(source.count("interpreter.stop_event.set()"), 2)

    def test_kernel_interruption_still_exists_in_oi(self):
        """The mechanism the guards depend on must still be there."""
        import inspect
        from interpreter.core.computer.terminal.languages import jupyter_language
        src = inspect.getsource(jupyter_language)
        self.assertIn("interrupt_kernel", src)


# ---------------------------------------------------------------------------
# 13 + 14. Honesty about limits.
# ---------------------------------------------------------------------------


def _strip_docstrings(tree):
    """Remove every docstring node so only real CODE is left to inspect."""
    doomed = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doomed.append(body[0])
    for node in doomed:
        node.value.value = ""
    return tree


class SelfAwarenessTruthTests(unittest.TestCase):
    def test_no_false_120_second_claim(self):
        limits = runtime_limits()
        self.assertNotIn("120", limits["honest_answer"])
        joined = " ".join(limits["cannot"]) if "cannot" in limits else ""
        for text in limitations()["cannot"]:
            self.assertNotIn("120 second", text)

    def test_the_injected_prompt_states_no_false_120_second_ceiling(self):
        """The PROMPT is what she reads every turn, so truth must hold THERE.

        Round W root cause: the truthful limits were never injected, and the only
        place a false "120 second ceiling" could hide unnoticed was the prompt text
        itself. Checking runtime_limits() alone proved nothing about the prompt.

        The prompt deliberately names the wrong answer so she can refuse it, so
        this checks the ASSERTION, not mere presence of the number: every line
        mentioning 120 must also be a denial, and no line may affirm a ceiling.
        """
        import re

        from source.server.self_awareness import _limits_for_prompt, self_knowledge

        affirms_ceiling = re.compile(
            r"(?:ceiling|limit|timeout|budget)\s*(?:is|of|=|:)?\s*120",
            re.I,
        )
        for text in (_limits_for_prompt(), self_knowledge(max_chars=100000)):
            low = text.lower()
            # The truthful statement must be present, not merely absent of lies.
            self.assertIn("no short cap", low)
            self.assertNotIn("capped at two minutes", low)
            self.assertNotIn("my hard ceiling is 120", low)
            for line in low.splitlines():
                # \b120\b, so a coincidental pid like 12016 is not a "ceiling".
                if not re.search(r"\b120\b", line):
                    continue
                self.assertIsNone(
                    affirms_ceiling.search(line),
                    "prompt affirms a 120 ceiling: %r" % line,
                )
                self.assertTrue(
                    any(word in line for word in ("never", "no ", "not ")),
                    "a bare 120 in the prompt risks being read as a real "
                    "ceiling, so it must be an explicit denial: %r" % line,
                )

    def test_inference_is_reported_as_unrationed(self):
        limits = runtime_limits()
        self.assertTrue(limits["model_inference"]["streaming_is_unbounded"])
        self.assertGreaterEqual(limits["model_inference"]["ceiling_seconds"], 6000)

    def test_the_turn_budget_is_reported_truthfully(self):
        self.assertEqual(runtime_limits()["turn"]["budget_seconds"], 600.0)

    def test_generated_code_limits_are_reported(self):
        code = runtime_limits()["generated_code"]
        self.assertEqual(code["hard_tool_cap"], 24)
        self.assertEqual(code["console_repeat_cap"], 300)

    def test_runtime_never_reads_a_developer_notes_file(self):
        """FRIDAY must not depend on OpenCode/developer note files at runtime.

        `TO_DO.md`, `MEMORY.md`, `AGENTS.md` and `SEARCHES.md` are developer
        workflow ledgers, not runtime state. `self_knowledge()` is injected into
        the system prompt EVERY turn, so any such read puts dev bookkeeping in
        front of her on every request. `self_awareness` used to do exactly this
        via `pending_changes()`.

        Checked on the AST with docstrings stripped, so an explanatory COMMENT
        or a historical note may still mention these files (several legitimately
        do) without tripping the guard - only real string literals count, because
        only those can be opened at runtime.
        """
        developer_notes = ("TO_DO.md", "MEMORY.md", "AGENTS.md", "SEARCHES.md")
        offenders = []
        # Scan the WHOLE runtime tree, not a hand-picked few files: the prompt
        # profile is an equally valid place for such a read to hide, and a
        # file allow-list would silently stop covering new modules.
        skip = {".venv", "tmp-oi", "site-packages", ".git", "__pycache__",
                "node_modules", "site-packages-patches", "open_interpreter_patched"}
        root = os.path.dirname(os.path.dirname(os.path.dirname(AWARENESS_PY)))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for name in filenames:
                if not name.endswith(".py") or name.startswith("test_"):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    with open(full, encoding="utf-8") as handle:
                        tree = _strip_docstrings(ast.parse(handle.read()))
                except Exception:
                    continue
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Constant)
                            and isinstance(node.value, str)):
                        continue
                    for dev_file in developer_notes:
                        if dev_file in node.value:
                            offenders.append(
                                "%s:%s -> %r"
                                % (os.path.relpath(full, root), node.lineno,
                                   node.value.strip()[:70]))
        self.assertEqual(
            offenders, [],
            "runtime must not read developer notes files: %r" % offenders,
        )

    def test_the_change_queue_function_is_gone(self):
        """The dependency was removed, not repointed at another notes file."""
        import source.server.self_awareness as sa

        self.assertFalse(
            hasattr(sa, "pending_changes"),
            "pending_changes() must not return in any form",
        )

    def test_what_is_being_changed_is_still_reported(self):
        """The legitimate capability must survive the removal.

        "What is being changed in you" is real runtime information, derived from
        her OWN source file mtimes - it just must not come from a dev ledger.
        """
        from source.server.self_awareness import recent_changes, self_knowledge

        recent = recent_changes(hours=72 * 365 * 50)
        self.assertIn(".py", recent, "mtime-derived change list must still work")
        self.assertIn("BEING CHANGED IN YOU RIGHT NOW:", self_knowledge())

    def test_no_120_second_ceiling_anywhere_in_self_awareness(self):
        """No EXECUTABLE 120-second inference ceiling may exist.

        Checked on the AST rather than the raw text, because runtime_limits()
        deliberately NAMES ``MAX_TIMEOUT_SECONDS = 120.0`` in its docstring to
        record that those constants exist elsewhere and govern nothing. That
        correction must not be mistaken for a live ceiling.
        """
        path = AWARENESS_PY
        with open(path, encoding="utf-8") as handle:
            tree = _strip_docstrings(ast.parse(handle.read()))

        offenders = []
        for node in ast.walk(tree):
            rendered = ast.dump(node)
            if "MAX_TIMEOUT_SECONDS" in rendered:
                offenders.append(type(node).__name__)
        self.assertEqual(
            offenders, [],
            "self_awareness must not reintroduce a 120s ceiling: %r" % offenders,
        )

    def test_self_awareness_states_no_short_inference_cap(self):
        """The truth she must be able to report: inference is not rationed."""
        from source.server.self_awareness import limitations, runtime_limits

        limits = runtime_limits()
        inference = limits["model_inference"]
        self.assertIsNotNone(inference["ceiling_seconds"])
        self.assertGreaterEqual(
            float(inference["ceiling_seconds"]), 6000.0,
            "inference must not be rationed on a short client timeout",
        )
        self.assertTrue(inference["streaming_is_unbounded"])
        self.assertNotIn("120", limits["honest_answer"])
        said = " ".join(limitations()["cannot"]).lower()
        self.assertIn("no short cap", said)


# ---------------------------------------------------------------------------
# 15. Provider routing untouched.
# ---------------------------------------------------------------------------


class RoutingUnchangedTests(unittest.TestCase):
    def test_the_limits_reach_the_prompt_every_turn(self):
        """runtime_limits() existing is not enough - it must be INJECTED.

        Round W lesson: the truth was available in the module and still never
        reached her prompt, so she answered from memory instead.
        """
        from source.server.self_awareness import self_knowledge

        text = self_knowledge(max_chars=100000)
        self.assertIn("YOUR REAL TIME AND SAFETY LIMITS", text)
        self.assertIn("600", text)
        self.assertIn("24", text)

    def test_the_prompt_limits_are_not_truncated_away(self):
        """Adding limits must not push the tail of self_knowledge out."""
        from source.server.self_awareness import self_knowledge

        text = self_knowledge()
        self.assertIn("Never guess about your own system - read it.", text)
        self.assertIn("YOUR REAL TIME AND SAFETY LIMITS", text)

    def test_a_turn_is_ended_on_its_budget_only_once(self):
        """The watchdog ticks every second; it must not re-speak every second."""
        harness = WatchdogHarness(
            elapsed=1000.0, silent=0.0, progress_observed=True
        )
        self.assertIsNotNone(harness.turn_budget_check())
        # _finish_budgeted_turn sets the latch; the check must then stay quiet.
        harness.server_state["turn_budget_enforced"] = True
        self.assertIsNone(
            harness.turn_budget_check(),
            "a turn already ended on its budget must not be ended again",
        )

    def test_a_new_turn_re_arms_the_budget_latch(self):
        ns = {"time": time}
        exec(compile(_extract("_stamp_progress"), SERVER_PY, "exec"), ns)
        state = {"last_progress": 0.0, "turn_budget_enforced": True}
        ns["_stamp_progress"](state, turn_started=True)
        self.assertFalse(
            state["turn_budget_enforced"],
            "the next turn must not inherit the previous turn's exemption",
        )

    def test_the_chain_is_intact(self):
        from source.server import brain_router
        self.assertEqual(brain_router.LOCAL_MODEL, "ollama_chat/qwen3:8b")
        self.assertIn("groq", brain_router.GROQ_BASE)
        self.assertIn("openrouter", brain_router.OPENROUTER_BASE)
        self.assertIn("generativelanguage", brain_router.GEMINI_BASE)

    def test_modal_is_still_primary(self):
        from source.server import brain_router
        self.assertEqual(brain_router.MODAL_MODEL,
                         "nvidia/GLM-5.3-Flash-NVFP4")

    def test_no_timeout_work_touched_the_router(self):
        """Round W changed no provider timeout - the brief forbade it."""
        path = BRAIN_ROUTER_PY
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertNotIn("LITELLM_REQUEST_TIMEOUT", body)
        self.assertNotIn("request_timeout", body)

    def test_inference_ceiling_is_still_litellms_default(self):
        """No FRIDAY-side inference cap was introduced."""
        import litellm
        from litellm.constants import DEFAULT_REQUEST_TIMEOUT_SECONDS
        self.assertEqual(float(litellm.request_timeout),
                         float(DEFAULT_REQUEST_TIMEOUT_SECONDS))

    def test_vision_and_stt_timeouts_were_not_touched(self):
        from source.server import screen_understanding, cloud_stt
        self.assertEqual(screen_understanding.VISION_TIMEOUT_SECONDS, 25.0)
        self.assertEqual(screen_understanding.LOCAL_VISION_TIMEOUT_SECONDS, 20.0)
        self.assertEqual(cloud_stt.STT_TIMEOUT, 30.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)