"""PRIORITY-1 runtime defects (2026-11-06) - regression tests.

Five organic defects from friday.log 2026-11-06, each locked against the
PRODUCTION source (AST-extracted from server.py / imported from the real
modules), never against a reimplementation:

 1. `trace("client_playback", event=...)` collided with `def trace(event,
    **detail)` -> "got multiple values for argument 'event'", 498x, each one
    republishing state=error and failing the brain router.
 2. The input-error path had no dedup: one broken telemetry call became an
    error storm that poisoned turn state.
 3. The HARD tool cap did not terminate the turn: the websocket output loop
    re-enters new_output after every `return`, so queued residue fired the cap
    a SECOND time (#26 after #25) and produced two competing final narrations.
    Also: loop_guard's budget (25) disagreed with HARD_TOOL_CAP (24).
 4. Tool honesty: the script assembler CLEARED its buffer on every streaming
    delta (the completed row/log kept only the last fragment), and
    `success = output.get("success", True)` - a key OI never sets - reported
    success=True next to a FileNotFoundError traceback. Subprocess readers in
    FRIDAY's helpers decoded with the locale codec and could die on charmap
    bytes.
 5. Self-awareness: the model GUESSED absolute paths for her own source and
    hit FileNotFoundError (WinError 3) four times; the prompt also advertised
    a stale screenshot directory.
"""

import ast
import os
import re
import sys
import textwrap
import threading
import types
import unittest

sys.path.insert(0, os.getcwd())

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")
RESPOND_PY = os.path.join(
    sys.prefix, "Lib", "site-packages", "interpreter", "core", "respond.py",
)


def _server_source():
    with open(SERVER_PY, encoding="utf-8") as handle:
        return handle.read()


def _start_server_node(tree):
    return next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "start_server"
    )


def _extract(name, container=None):
    """Pull ONE named function out of server.py (module level or in start_server)."""
    source = _server_source()
    tree = ast.parse(source)
    scope = tree.body if container is None else [container]
    if container is None:
        node = next(
            (n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == name),
            None,
        )
        if node is None:
            start = _start_server_node(tree)
            node = next(
                (n for n in ast.walk(start)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == name),
                None,
            )
    else:
        node = next(
            (n for n in ast.walk(container)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == name),
            None,
        )
    assert node is not None, "%s not found" % name
    lines = source.splitlines()
    return textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))


class _NullLog:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Recorder:
    def __init__(self, name="rec", log=None, result=None):
        self.name = name
        self.log = log if log is not None else []
        self.result = result
        self.calls = []

    def __call__(self, *args, **kwargs):
        record = (self.name, args, kwargs)
        self.log.append(record)
        self.calls.append(record)
        return self.result


# ===========================================================================
# 1. The trace() contract, both sides.
# ===========================================================================


def _load_real_trace():
    """Execute the REAL `trace` closure body from start_server."""
    source = _server_source()
    tree = ast.parse(source)
    start = _start_server_node(tree)
    node = next(
        n for n in ast.walk(start)
        if isinstance(n, ast.FunctionDef) and n.name == "trace"
    )
    lines = source.splitlines()
    body = textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))
    records = []
    namespace = {
        "_flog": _NullLog(),
        "_voice_trace_get": lambda: types.SimpleNamespace(
            record=lambda event, detail=None: records.append((event, detail))
        ),
        "debug": False,
        "trace_start": 0.0,
        "time": types.SimpleNamespace(monotonic=lambda: 0.0),
    }
    exec(compile(body, SERVER_PY, "exec"), namespace)
    return namespace["trace"], records


class TraceContractTests(unittest.TestCase):
    """The collision that produced 498 'input error' lines."""

    def setUp(self):
        self.trace, self.records = _load_real_trace()

    def test_positional_plus_event_keyword_does_not_raise(self):
        """BEFORE the fix this raised TypeError at the call site."""
        self.trace("client_playback", event="first_audio", bytes=4096)

    def test_the_playback_kwarg_is_recorded(self):
        self.trace("client_playback", playback_event="progress", bytes=1)
        self.assertEqual(self.records[0][0], "client_playback")
        self.assertEqual(self.records[0][1], {"playback_event": "progress",
                                              "bytes": 1})

    def test_a_dict_payload_with_an_event_key_is_accepted(self):
        self.trace({"event": "dict_shape", "x": 1})
        self.assertEqual(self.records[0][0], "dict_shape")
        self.assertEqual(self.records[0][1], {"x": 1})

    def test_a_bare_event_keyword_without_positional_is_accepted(self):
        self.trace(event="kwarg_only")
        self.assertEqual(self.records[0][0], "kwarg_only")

    def test_the_plain_call_shape_still_works(self):
        self.trace("stt_start")
        self.assertEqual(self.records[0][0], "stt_start")
        self.assertIsNone(self.records[0][1])

    def test_a_broken_telemetry_sink_never_escapes(self):
        """Telemetry must never poison the pipeline it observes."""
        def _boom(*a, **k):
            raise RuntimeError("sink down")
        namespace = {
            "_flog": types.SimpleNamespace(debug=_boom),
            "_voice_trace_get": lambda: types.SimpleNamespace(record=_boom),
            "debug": True,               # also exercises the print branch
            "trace_start": 0.0,
            "time": types.SimpleNamespace(monotonic=_boom),
        }
        source = _server_source()
        tree = ast.parse(source)
        start = _start_server_node(tree)
        node = next(n for n in ast.walk(start)
                    if isinstance(n, ast.FunctionDef) and n.name == "trace")
        lines = source.splitlines()
        body = textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))
        exec(compile(body, SERVER_PY, "exec"), namespace)
        namespace["trace"]("anything")          # must not raise
        namespace["trace"](dict(event="x"))     # must not raise

    def test_the_production_caller_never_passes_event_as_a_keyword(self):
        """The caller side of the contract: name the field for what it is."""
        self.assertNotIn('trace("client_playback", event=', _server_source())
        self.assertIn('trace("client_playback", playback_event=', _server_source())


# ===========================================================================
# 2. The input-error path: bounded reporting, no turn-state poisoning.
# ===========================================================================


def _extract_new_input():
    source = _server_source()
    tree = ast.parse(source)
    start = _start_server_node(tree)
    node = next(n for n in ast.walk(start)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "new_input")
    lines = source.splitlines()
    return textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))


class _RaisingTraceHarness:
    """The REAL new_input with a telemetry layer that fails.

    `errors` is a list of exception instances/messages consumed one per trace
    call (cycled), so a test can distinguish "same message repeated" from
    "distinct messages".
    """

    def __init__(self, error=None, errors=None):
        self.calls = []
        log = self.calls
        source = _extract_new_input()
        self.status_errors = []
        self.mark_failures = []
        pool = list(errors) if errors else None
        one = error or RuntimeError(
            "trace() got multiple values for argument 'event'")
        state = {"i": 0}

        def _trace(*a, **k):
            if pool:
                idx = min(state["i"], len(pool) - 1)
                state["i"] += 1
                item = pool[idx]
                raise (item if isinstance(item, BaseException)
                       else RuntimeError(str(item)))
            raise one

        namespace = {
            "asyncio": __import__("asyncio"),
            "time": types.SimpleNamespace(time=lambda: 1000.0,
                                          monotonic=lambda: 0.0),
            "trace": _trace,
            "record_playback": _Recorder("record_playback", log),
            "_stamp_progress": _Recorder("_stamp_progress", log),
            "_flog": _NullLog(),
            "publish_status": lambda state, detail="": self.status_errors.append(
                (state, detail)),
            "emit_input_terminal": _Recorder("emit_input_terminal", log),
            "STATUS_IDLE": "idle",
            "STATUS_THINKING": "thinking",
            "STATUS_LISTENING": "listening",
            "STATUS_ERROR": "error",
            "response_is_active": lambda: False,
            "reject_reason": lambda *a, **k: None,
            "_find_wake_word": lambda *a, **k: None,
            "_strip_wake_word": lambda t: t,
            "_is_recent_tts_echo": lambda *a, **k: False,
            "_hot_window_is_active": lambda *a, **k: False,
            "_hot_window_candidate": lambda *a, **k: None,
            "_STOP_PHRASE_RE": re.compile(r"$^"),
            "_redact_text": lambda s: s,
            "_automatic_memory_enabled": lambda *a, **k: False,
            "_begin_single_turn": _Recorder("_begin_single_turn", log),
            "_mark_turn_dispatch": _Recorder("_mark_turn_dispatch", log),
            "_stamp_progress": _Recorder("_stamp_progress", log),
            "_pending_push": _Recorder("_pending_push", log),
            "snapshot_turn_baseline": lambda *a, **k: {},
            "restore_turn_baseline": lambda *a, **k: None,
            "_play_chirp": _Recorder("_play_chirp", log),
            "_record_fresh_screen_evidence": _Recorder("screen", log),
            "_screen_context_prefix": _Recorder("prefix", log, ""),
            "_log_screen_call_ledger": _Recorder("ledger", log),
            "_pending_resolve": lambda *a, **k: None,
            "_pending_reminder_if_any": lambda *a, **k: None,
            "api_pools": types.SimpleNamespace(
                cloud_brain_enabled=lambda: False),
            "build_memory_context": _Recorder("mem", log, ""),
            "extract_and_remember_automatic": _Recorder("auto", log),
            "format_memory_response": _Recorder("fmt", log, ""),
            "parse_memory_command": _Recorder("parse", log),
            "is_screen_request": _Recorder("screen_req", log, False),
            "screen_response": _Recorder("screen_resp", log, ""),
            "route_command": _Recorder("route", log),
            "transcribe_wav": _Recorder("transcribe", log, "friday hi"),
            "pcm_to_wav": _Recorder("pcm", log, b""),
            "log_action": _Recorder("log_action", log),
            "append_outcome": _Recorder("append_outcome", log),
            "chat_append": _Recorder("chat_append", log),
            "close_hot_window": _Recorder("close_hot_window", log),
            "open_hot_window": _Recorder("open_hot_window", log, False),
            "handle_special_command": _Recorder("special", log),
            "log_direct": _Recorder("log_direct", log),
            "old_input": _Recorder("old_input", log),
            "queue_direct_response": _Recorder("queue_direct", log),
            "build_memory_context": _Recorder("mem", log, ""),
            "_ACTIVITY_QUERY_RE": None,
            "_LIVE_STATE_QUERY_RE": None,
        }
        queue = []
        stt = types.SimpleNamespace(
            start=_Recorder("stt_start", log),
            stop=_Recorder("stt_stop", log),
            text=lambda: "friday hi",
            feed_audio=_Recorder("feed_audio", log),
        )
        interp = types.SimpleNamespace(
            stt=stt,
            tts=types.SimpleNamespace(is_playing=lambda: False, stop=lambda: None),
            messages=[],
            output_queue=types.SimpleNamespace(
                sync_q=types.SimpleNamespace(put=lambda item: queue.append(item))
            ),
        )
        router = types.SimpleNamespace(
            new_turn=_Recorder("new_turn", log),
            apply_for=_Recorder("apply_for", log),
            failover_to_next=_Recorder("failover", log),
            mark_failure=lambda *a, **k: self.mark_failures.append(a),
        )
        server_state = {
            "is_paused": False,
            "pending_input": None,
            "last_wake_time": 0.0,
            "last_wake_text": "",
            "hot_window_seconds": 30,
            "hot_window_active": True,      # hot window: no wake word needed
            "hot_window_expires_at": 1e18,
            "hot_window_timer": None,
            "hot_window_lock": threading.Lock(),
            "recent_tts_texts": [],
            "tts_echo_buffer": "",
            "tts_echo_turn_active": False,
            "memory_store": None,
            "turn_channel": "",
            "turn_reply_token": None,
            "turn_log": None,
            "last_progress": 0.0,
            "brain_router": router,
            "event_loop": None,
            "barge_in": False,
        }
        self_obj = types.SimpleNamespace(
            stt=stt, tts=interp.tts, output_queue=interp.output_queue,
            messages=[], audio_chunks=[], play_audio=False,
        )
        namespace.update({
            "interpreter": interp,
            "server_state": server_state,
            "self": self_obj,
        })
        exec(compile(source, SERVER_PY, "exec"), namespace)
        self.new_input = namespace["new_input"]
        self.self_obj = self_obj
        self.server_state = server_state
        self.queue = queue

    def feed(self, chunk):
        import asyncio
        asyncio.run(self.new_input(self.self_obj, chunk))


class InputErrorResilienceTests(unittest.TestCase):
    """One failing trace call: reported ONCE, never poisons turn state."""

    def test_a_repeated_telemetry_failure_is_reported_once(self):
        harness = _RaisingTraceHarness()
        for _ in range(6):
            harness.feed({"type": "playback", "event": "first_audio",
                          "bytes": 100})
        error_reports = [d for s, d in harness.status_errors if s == "error"]
        self.assertLessEqual(
            len(error_reports), 1,
            "a telemetry failure must not republish state=error in a loop "
            "(got %d)" % len(error_reports),
        )
        self.assertEqual(len(error_reports), 1,
                         "the FIRST distinct message is reported exactly once")

    def test_the_brain_router_is_never_failed_by_an_input_error(self):
        """A diagnostic failure must not ban a provider / poison turn state."""
        harness = _RaisingTraceHarness()
        for _ in range(6):
            harness.feed({"type": "playback", "event": "progress", "bytes": 1})
        self.assertEqual(
            harness.mark_failures, [],
            "the input-error path must not call brain_router.mark_failure "
            "(provider failures are marked in the turn-error path)",
        )

    def test_processing_continues_after_a_telemetry_failure(self):
        """The exception is caught per chunk; later chunks still run."""
        harness = _RaisingTraceHarness()
        harness.feed({"type": "playback", "event": "first_audio"})
        # A subsequent AUDIO chunk has its own handler (feed_audio) and must
        # still run even though the previous chunk's trace blew up.
        harness.feed(b"synthetic-audio-bytes")
        names = [name for name, _, _ in harness.calls]
        self.assertIn("feed_audio", names,
                      "input processing must continue after a failed trace")

    def test_distinct_messages_are_each_reported_once_not_per_event(self):
        pool = [RuntimeError("distinct-%d" % i) for i in range(12)]
        harness = _RaisingTraceHarness(errors=pool)
        for i in range(12):
            harness.feed({"type": "playback", "event": "e%d" % i})
        details = [d for s, d in harness.status_errors if s == "error"]
        self.assertEqual(len(details), 12, "one report per distinct message")
        for i in range(12):
            message = "distinct-%d" % i
            hits = [d for d in details
                    if d == "Input error: %s" % message]
            self.assertEqual(len(hits), 1,
                             "%r reported %d times" % (message, len(hits)))

    def test_the_seen_buffer_is_bounded(self):
        pool = [RuntimeError("distinct-%d" % i) for i in range(40)]
        harness = _RaisingTraceHarness(errors=pool)
        for i in range(40):
            harness.feed({"type": "playback", "event": "e%d" % i})
        seen = harness.server_state.get("_input_error_seen")
        self.assertIsInstance(seen, list)
        self.assertLessEqual(len(seen), 8,
                             "the dedup buffer must stay bounded")


# ===========================================================================
# 3. The HARD tool cap terminates the turn exactly once.
# ===========================================================================


def _extract_hard_stop_block():
    """The real `if tool_counter["n"] > HARD_TOOL_CAP:` block from new_output."""
    source = _server_source()
    tree = ast.parse(source)
    start = _start_server_node(tree)
    new_output = next(n for n in ast.walk(start)
                      if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == "new_output")
    for node in ast.walk(new_output):
        if not isinstance(node, ast.If):
            continue
        try:
            test = ast.unparse(node.test)
        except Exception:
            continue
        # unparse renders the subscript with single quotes.
        if "tool_counter" in test and "HARD_TOOL_CAP" in test:
            lines = source.splitlines()
            return textwrap.dedent(
                "\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError("hard-stop block not found")


def _extract_drain_block():
    """The real POST-HARD-CAP DRAIN guard from new_output."""
    source = _server_source()
    tree = ast.parse(source)
    start = _start_server_node(tree)
    new_output = next(n for n in ast.walk(start)
                      if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == "new_output")
    for node in ast.walk(new_output):
        if not isinstance(node, ast.If):
            continue
        try:
            test = ast.unparse(node.test)
        except Exception:
            continue
        if "tool_cap_terminated" in test and "_drain_keep" in ast.unparse(node):
            lines = source.splitlines()
            return textwrap.dedent(
                "\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError("drain block not found")


class _HardStopHarness:
    def __init__(self):
        self.narrations = []
        self.chat_rows = []
        self.status = []
        self.traces = []
        self.interpreter = types.SimpleNamespace(
            stop_event=threading.Event())
        self.server_state = {
            "tool_cap_terminated": False,
            "last_user_text": "do the thing",
        }
        self.namespace = {
            "server_state": self.server_state,
            # Production increments the counter BEFORE this block runs, so the
            # value seen here is post-increment: 25 means "the 25th tool
            # started against a cap of 24".
            "tool_counter": {"n": 25},
            "HARD_TOOL_CAP": 24,
            "tool_code_parts": [],
            "tool_timing": {"started": None},
            "interpreter": self.interpreter,
            "os": os,
            "time": types.SimpleNamespace(monotonic=lambda: 1.0,
                                          strftime=lambda fmt: "now"),
            "_flog": _NullLog(),
            "chat_append": lambda role, content, status="complete": (
                self.chat_rows.append((role, content, status))),
            "chat_upsert_tool": _Recorder("chat_upsert"),
            "publish_status": lambda state, detail="": self.status.append(
                (state, detail)),
            "STATUS_ERROR": "error",
            "STATUS_THINKING": "thinking",
            "trace": lambda *a, **k: self.traces.append(a),
            "_play_chirp": _Recorder("chirp"),
            "_verified_work_summary": lambda: {"verified": [], "unverified": []},
            "_tool_cap_fallback_text": lambda n, s: "FALLBACK-%d" % n,
            "_ownership": lambda ss: types.SimpleNamespace(is_external=False),
            "queue_speech_only": lambda text: self.narrations.append(text),
            "_redact_text": lambda s: s,
            "SERVER_TOOL_CAP": [99],
            "_redact_for_phone": lambda s: s,
            "TEXT_CHANNEL_CHAT_ID": "text",
            "_post_reply": _Recorder("post_reply"),
            "_clear_ownership": _Recorder("clear_ownership"),
            "telegram_adapter": types.SimpleNamespace(
                send_text=_Recorder("send_text")),
            "_final_narration_completion": None,  # set below (async)
        }

        async def _narration(*a, **k):
            return "NARRATION-%d" % len(self.narrations)

        async def _terminal_notice(owner, text):
            self.narrations.append(text)

        self.namespace["_terminal_notice"] = _terminal_notice
        self.namespace["owner"] = object()
        self.namespace["_final_narration_completion"] = _narration
        block = _extract_hard_stop_block()
        driver = ("async def _drive():\n"
                  + textwrap.indent(block, "    "))
        exec(compile(driver, SERVER_PY, "exec"), self.namespace)

    def run_once(self):
        import asyncio
        try:
            asyncio.run(self.namespace["_drive"]())
        except SystemExit:      # pragma: no cover - defensive
            raise
        except StopIteration:   # pragma: no cover - defensive
            pass


class HardStopTerminationTests(unittest.TestCase):
    """After the cap: no resurrection, exactly ONE terminal narration."""

    def test_the_cap_fires_once_and_latches_the_turn(self):
        h = _HardStopHarness()
        h.run_once()
        self.assertTrue(h.server_state["tool_cap_terminated"],
                        "the cap must latch termination")
        self.assertEqual(len(h.narrations), 1)
        self.assertTrue(h.interpreter.stop_event.is_set())

    def test_a_second_arrival_never_narrates_again(self):
        """The residue that used to fire HARD stop #26 and a 2nd narration."""
        h = _HardStopHarness()
        h.run_once()
        h.run_once()                     # queued residue re-enters the block
        self.assertEqual(len(h.narrations), 1,
                         "exactly ONE final narration per terminated turn")
        stop_warnings = [
            d for s, d in h.status if s == "error"
        ]
        # one terminal publish from the cap itself, nothing extra
        self.assertEqual(len(stop_warnings), 1)

    def test_the_turn_is_not_terminated_before_the_cap(self):
        h = _HardStopHarness()
        h.namespace["tool_counter"]["n"] = 24      # at cap, not over it
        h.run_once()
        self.assertFalse(h.server_state["tool_cap_terminated"])
        self.assertEqual(h.narrations, [])

    def test_the_latch_is_reset_for_a_new_turn(self):
        from source.server.server import _begin_single_turn
        state = {"tool_cap_terminated": True}
        interp = types.SimpleNamespace(loop=True)
        _begin_single_turn(interp, state, user="next", source="voice")
        self.assertFalse(state["tool_cap_terminated"],
                         "a new turn must start with the drain off")

    def test_queue_speech_only_tags_its_frames(self):
        """The drain keeps FRIDAY's own frames by tag, drops OI residue."""
        source = _server_source()
        body = _extract("queue_speech_only")
        frames = []
        interpreter = types.SimpleNamespace(
            output_queue=types.SimpleNamespace(
                sync_q=types.SimpleNamespace(put=frames.append)))
        namespace = {"_put_owned": frames.append}
        exec(compile(body, SERVER_PY, "exec"), namespace)
        namespace["queue_speech_only"]("the one answer")
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(f.get("_friday_authored") for f in frames),
                        "queued narration frames must be tagged so the "
                        "post-cap drain keeps them")


class PostCapDrainTests(unittest.TestCase):
    """Late callbacks from a killed turn are ignored, lifecycle is not."""

    def _drive(self, latch, output):
        block = _extract_drain_block()
        reached = {"after": False}
        namespace = {
            "server_state": {"tool_cap_terminated": latch},
            "output": output,
        }
        driver = ("def _drive():\n"
                  "    for _single in (1,):\n"
                  + textwrap.indent(block, "        ")
                  + "\n"
                  + "        reached['after'] = True\n")
        exec(compile(driver, SERVER_PY, "exec"), namespace)
        namespace["reached"] = reached
        namespace["_drive"]()
        return reached["after"]

    def test_tool_residue_is_drained_after_termination(self):
        for residue in (
            {"type": "code", "start": True, "content": "print(1)"},
            {"type": "code", "end": True},
            {"type": "console", "content": "Traceback (most recent call last)"},
            {"role": "assistant", "type": "message", "content": "my own text"},
            {"role": "assistant", "type": "message", "end": True},
        ):
            self.assertFalse(
                self._drive(True, residue),
                "post-cap residue must be drained: %r" % (residue,),
            )

    def test_the_narration_and_lifecycle_still_pass(self):
        self.assertTrue(self._drive(
            True, {"role": "assistant", "type": "message",
                   "content": "the answer", "_friday_authored": True}))
        self.assertTrue(self._drive(
            True, {"role": "assistant", "type": "message",
                   "end": True, "_friday_authored": True}))
        self.assertTrue(self._drive(
            True, {"role": "server", "type": "status", "content": "complete"}))
        self.assertTrue(self._drive(
            True, {"type": "error", "content": "boom"}))
        self.assertTrue(self._drive(
            True, {"speech_stop": True, "utterance": "u"}))

    def test_before_the_cap_everything_passes(self):
        self.assertTrue(self._drive(
            False, {"role": "assistant", "type": "message", "content": "hi"}))
        self.assertTrue(self._drive(
            False, {"type": "code", "start": True}))


class LoopGuardCapAgreementTests(unittest.TestCase):
    """loop_guard's budget and server.py's HARD cap must agree on 24."""

    def test_loop_guard_default_is_24(self):
        from source.server.loop_guard import LoopGuardConfig
        self.assertEqual(LoopGuardConfig().max_tool_executions, 24)

    def test_server_hard_cap_default_is_the_same_24(self):
        match = re.search(
            r'HARD_TOOL_CAP = int\(float\(os\.getenv\("FRIDAY_HARD_TOOL_CAP", '
            r'"(\d+)"\)\)\)',
            _server_source(),
        )
        self.assertIsNotNone(match)
        from source.server.loop_guard import LoopGuardConfig
        self.assertEqual(int(match.group(1)),
                         LoopGuardConfig().max_tool_executions,
                         "the two sides must agree on the intended value")

    def test_the_cap_was_reconciled_down_not_raised(self):
        """24 is the documented ceiling; it must not be lifted to hide loops."""
        self.assertIn('FRIDAY_HARD_TOOL_CAP", "24"', _server_source())
        self.assertNotIn('FRIDAY_HARD_TOOL_CAP", "32"', _server_source())


class RefusedExecutionAfterStopTests(unittest.TestCase):
    """OI must not start another LLM call or run code once stop_event is set."""

    def test_respond_breaks_immediately_when_stopped(self):
        from interpreter.core.respond import respond

        class _Stopped:
            stop_event = threading.Event()

        stopped = _Stopped()
        stopped.stop_event.set()
        # Nothing else on the fake interpreter is touched: the check sits at
        # the TOP of respond()'s loop, before the system-message render and
        # long before RUN CODE.
        self.assertEqual(list(respond(stopped)), [])

    def test_the_stop_check_sits_before_run_code(self):
        with open(RESPOND_PY, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("_stop = getattr(interpreter, \"stop_event\", None)", text)
        stop_at = text.index('_stop = getattr(interpreter, "stop_event", None)')
        run_at = text.index("### RUN CODE")
        self.assertLess(stop_at, run_at,
                        "the stop check must precede code execution")


# ===========================================================================
# 4. Tool honesty: assembly + outcome + subprocess decoding.
# ===========================================================================


def _extract_script_capture_block():
    source = _server_source()
    tree = ast.parse(source)
    start = _start_server_node(tree)
    new_output = next(n for n in ast.walk(start)
                      if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == "new_output")
    for node in ast.walk(new_output):
        if not isinstance(node, ast.If):
            continue
        try:
            test = ast.unparse(node.test)
            body = ast.unparse(node)
        except Exception:
            continue
        # unparse renders string literals with single quotes; match the OUTER
        # capture block (the one that ASSIGNS _script_payload), not the inner
        # `if _script_payload and not output.get('end')` consumer.
        if (test == "isinstance(output, dict) and output.get('type') == 'code'"
                and "_script_payload = " in body):
            lines = source.splitlines()
            return textwrap.dedent(
                "\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError("script capture block not found")


class ScriptAssemblyTests(unittest.TestCase):
    """The fragments are streaming DELTAS; the buffer must accumulate them."""

    def _drive(self, frames):
        block = _extract_script_capture_block()
        parts = []
        upserts = []
        namespace = {
            "output": {},
            "tool_code_parts": parts,
            "tool_timing": {"started": None},
            "tool_counter": {"n": 3},
            "time": types.SimpleNamespace(monotonic=lambda: 1.0),
            "_flog": _NullLog(),
            "chat_upsert_tool": lambda *a, **k: upserts.append((a, k)),
        }
        driver = ("def _drive():\n"
                  "    for output in FRAMES:\n"
                  + textwrap.indent(block, "        "))
        namespace["FRAMES"] = frames
        exec(compile(driver, SERVER_PY, "exec"), namespace)
        namespace["_drive"]()
        return parts, upserts

    def test_deltas_assemble_into_the_executed_block(self):
        intended = "import os\nprint('hello')\n"
        parts, _ = self._drive([
            {"type": "code", "format": "python", "start": True},
            {"type": "code", "content": "import os\n"},
            {"type": "code", "content": "print('hello')\n"},
            {"type": "code", "end": True},
        ])
        self.assertEqual("".join(parts), intended,
                         "the assembled script must equal the intended block; "
                         "BEFORE the fix only the LAST delta survived")

    def test_repeated_identical_slices_are_not_dropped(self):
        parts, _ = self._drive([
            {"type": "code", "content": "a"},
            {"type": "code", "content": "a"},
            {"type": "code", "content": "b"},
        ])
        self.assertEqual("".join(parts), "aab")

    def test_the_live_row_shows_the_whole_script_not_one_fragment(self):
        _, upserts = self._drive([
            {"type": "code", "format": "python", "start": True},
            {"type": "code", "content": "first slice"},
            {"type": "code", "content": "+second slice"},
        ])
        last_text = upserts[-1][0][0]
        self.assertIn("first slice+second slice", last_text)

    def test_the_capture_reads_both_payload_keys(self):
        text = _server_source()
        self.assertIn('output.get("code") or output.get("content")', text)


def _extract_code_end_block():
    source = _server_source()
    tree = ast.parse(source)
    start = _start_server_node(tree)
    new_output = next(n for n in ast.walk(start)
                      if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == "new_output")
    for node in ast.walk(new_output):
        if not isinstance(node, ast.If):
            continue
        # unparse renders the string literal with single quotes.
        if ast.unparse(node.test) == "output.get('end')":
            lines = source.splitlines()
            return textwrap.dedent(
                "\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError("code-end block not found")


def _extract_console_verdict_block():
    source = _server_source()
    tree = ast.parse(source)
    start = _start_server_node(tree)
    new_output = next(n for n in ast.walk(start)
                      if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == "new_output")
    for node in ast.walk(new_output):
        if not isinstance(node, ast.If):
            continue
        try:
            test = ast.unparse(node.test)
        except Exception:
            continue
        if test.startswith("_console_shows_failure("):
            lines = source.splitlines()
            return textwrap.dedent(
                "\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError("console verdict block not found")


class ToolOutcomeHonestyTests(unittest.TestCase):
    """`success=True` may never sit beside a traceback."""

    def _namespace(self):
        rows = []
        return {
            "rows": rows,
            "tool_code_parts": ["print(1)"],
            "tool_timing": {"started": None},
            "tool_counter": {"n": 7},
            "HARD_TOOL_CAP": 24,
            "language": "python",
            "trace": lambda *a, **k: None,
            "publish_status": lambda *a, **k: None,
            "_play_chirp": lambda *a, **k: None,
            "_flog": _NullLog(),
            "LAST_TOOL_SCRIPT": {},
            "chat_upsert_tool": lambda text, key=None, status=None: rows.append(
                (text, key, status)),
            "time": types.SimpleNamespace(
                monotonic=lambda: 1.0,
                strftime=lambda fmt: "2026-11-06"),
            "SERVER_TOOL_CAP": [99],
            "server_state": {"tool_cap_nudged": True},
            "STATUS_THINKING": "thinking",
            "STATUS_ERROR": "error",
            "_redact_text": lambda s: s,
            "_console_shows_failure": None,   # real one, injected below
            "_ownership": lambda ss: types.SimpleNamespace(is_external=False),
            "_external_reply_pending": lambda ss: False,
        }

    def test_a_code_end_without_a_status_reports_captured_not_ok(self):
        from source.server.server import _console_shows_failure
        ns = self._namespace()
        ns["_console_shows_failure"] = _console_shows_failure
        block = _extract_code_end_block()
        driver = ("def _drive():\n"
                  "    output = {'type': 'code', 'end': True}\n"
                  + textwrap.indent(block, "    "))
        exec(compile(driver, SERVER_PY, "exec"), ns)
        ns["_drive"]()
        self.assertIsNone(
            ns["LAST_TOOL_SCRIPT"].get("success"),
            "a code-end frame proves the code was captured, not that it ran; "
            "the old default True claimed success for a failed run")
        self.assertIn("captured", ns["rows"][0][0])

    def test_a_traceback_console_marks_the_tool_failed(self):
        from source.server.server import _console_shows_failure
        ns = self._namespace()
        ns["_console_shows_failure"] = _console_shows_failure
        ns["LAST_TOOL_SCRIPT"] = {
            "tool_number": 7, "success": None, "code": "print(1)"}
        block = _extract_console_verdict_block()
        driver = ("def _drive():\n"
                  "    console_text = TEXT\n"
                  "    tool_counter = TOOL_COUNTER\n"
                  + textwrap.indent(block, "    "))
        ns["TEXT"] = ("FileNotFoundError: [WinError 3] The system cannot "
                      "find the path specified: 'X'\n"
                      "Traceback (most recent call last):")
        ns["TOOL_COUNTER"] = {"n": 7}
        exec(compile(driver, SERVER_PY, "exec"), ns)
        ns["_drive"]()
        self.assertIs(ns["LAST_TOOL_SCRIPT"]["success"], False,
                      "a traceback-bearing run must not report success")
        self.assertTrue(ns["rows"], "the tool row must be rewritten FAILED")
        self.assertIn("FAILED", ns["rows"][-1][0])

    def test_a_clean_console_promotes_pending_to_success(self):
        from source.server.server import _console_shows_failure
        ns = self._namespace()
        ns["_console_shows_failure"] = _console_shows_failure
        ns["LAST_TOOL_SCRIPT"] = {
            "tool_number": 7, "success": None, "code": "print(1)"}
        block = _extract_console_verdict_block()
        driver = ("def _drive():\n"
                  "    console_text = TEXT\n"
                  "    tool_counter = TOOL_COUNTER\n"
                  + textwrap.indent(block, "    "))
        ns["TEXT"] = "hello world"
        ns["TOOL_COUNTER"] = {"n": 7}
        exec(compile(driver, SERVER_PY, "exec"), ns)
        ns["_drive"]()
        self.assertIs(ns["LAST_TOOL_SCRIPT"]["success"], True)

    def test_failure_detection_is_precise(self):
        from source.server.server import _console_shows_failure
        self.assertTrue(_console_shows_failure(
            "Traceback (most recent call last):\n  File ..."))
        self.assertTrue(_console_shows_failure(
            "Exception in thread Thread-18 (_readerthread):"))
        self.assertTrue(_console_shows_failure(
            "  File \"<input>\", line 1\nSyntaxError: invalid syntax"))
        self.assertFalse(_console_shows_failure("all good\n42"))
        self.assertFalse(_console_shows_failure(""))
        self.assertFalse(_console_shows_failure(None))

    def test_the_old_always_true_default_is_gone(self):
        self.assertNotIn('output.get("success", True)', _server_source())
        self.assertIn("success = output.get(\"success\")", _server_source())


class SubprocessDecodeTests(unittest.TestCase):
    """Readers must decode non-UTF8 bytes robustly, keeping the text visible."""

    HELPERS = (
        "windows_control.py",
        "tts_bootstrap.py",
        "proactive.py",
        "system_diagnostics.py",
    )

    def test_every_text_mode_subprocess_call_decodes_with_replace(self):
        root = os.path.join(os.getcwd(), "source", "server")
        for name in self.HELPERS:
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                text = handle.read()
            calls = re.findall(
                r"subprocess\.run\((?:[^()]|\([^()]*\))*?\)", text, re.S)
            text_calls = [c for c in calls if "text=True" in c]
            self.assertTrue(text_calls, "%s has no text-mode subprocess" % name)
            for call in text_calls:
                self.assertIn(
                    'errors="replace"', call,
                    "%s: a text-mode subprocess.run without errors='replace' "
                    "dies on charmap bytes (friday.log 13:37:47 "
                    "UnicodeDecodeError in _readerthread): %s"
                    % (name, call[:160]))

    def test_replace_decodes_bad_bytes_without_losing_the_text(self):
        import subprocess
        # \x81/\x8d/\x90/\x9d are UNDEFINED in cp1252 (the Windows locale
        # codec subprocess text-mode uses by default) - exactly the bytes that
        # crashed friday.log's `_readerthread` with UnicodeDecodeError.
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(b'ok \\x81\\x90 tail')"],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        self.assertIn("ok", result.stdout)
        self.assertIn("tail", result.stdout)
        self.assertIn("\ufffd", result.stdout,
                     "undecodable bytes must stay visible")


# ===========================================================================
# 5. Source discovery: verified paths, never guessed.
# ===========================================================================


class VerifiedPathTests(unittest.TestCase):
    def test_every_reported_path_exists(self):
        import os as _os
        from source.server.self_awareness import project_paths
        paths = project_paths()
        for name, path in paths.items():
            self.assertTrue(_os.path.exists(path),
                            "%s -> %s does not exist" % (name, path))

    def test_the_expected_roots_are_present(self):
        from source.server.self_awareness import project_paths
        paths = project_paths()
        for required in ("project_root", "source", "server", "skills",
                         "screenshots"):
            self.assertIn(required, paths)
        self.assertTrue(paths["project_root"].endswith("software"))
        self.assertTrue(paths["source"].endswith(
            os.path.join("software", "source")))

    def test_the_screenshots_path_is_the_one_actually_written(self):
        from source.server.self_awareness import project_paths
        from source.server.self_improve import scratchdir_path
        self.assertEqual(project_paths()["screenshots"], scratchdir_path())

    def test_self_knowledge_advertises_the_verified_paths(self):
        from source.server.self_awareness import self_knowledge, project_paths
        text = self_knowledge(max_chars=100000)
        self.assertIn("YOUR FILES (absolute and verified on disk", text)
        self.assertIn(project_paths()["project_root"], text)
        # the tail must survive the added line
        self.assertIn("Never guess about your own system - read it.",
                      self_knowledge())

    def test_the_prompt_no_longer_advertises_a_stale_screenshot_dir(self):
        from source.server.profiles import default as profile
        self.assertNotIn(r"Z:\obsolete\screenshots", profile.system_message_template,
                         "the prompt must not hard-code a directory FRIDAY "
                         "no longer writes to")
        shots = profile._friday_verified_shots_dir()
        self.assertTrue(os.path.isdir(shots))
        self.assertIn(shots, profile.system_message_template)

    def test_the_prompt_paths_are_resolved_from_disk(self):
        from source.server.profiles import default as profile
        self.assertIn("__SHOTS_DIR__", _profile_template_source())
        self.assertNotIn("__SHOTS_DIR__", profile.system_message_template)


def _profile_template_source():
    path = os.path.join(os.getcwd(), "source", "server", "profiles",
                        "default.py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main(verbosity=2)
