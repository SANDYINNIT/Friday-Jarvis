"""ROUND U (2026-10-04) - the Intent Judge is gone; the voice path is unchanged.

WHY
===
The Intent Judge ran on 34 of 138 voice turns and cost a median 6.52s (max
8.73s), yet produced ZERO useful decisions in 34/34 attempts: every call timed
out against a permanently cold `llama3.2:3b` (`load_duration` 26.26s against a
6s timeout, while the brain keep-alive pins only `qwen3:8b`) and fell back to
passing the query through unchanged. 0 rewrites, 0 rejections, `model: null` in
34/34 audit records. It was removed rather than repaired, and deliberately NOT
replaced with another classifier.

BEHAVIOURAL INVARIANT LOCKED HERE
================================
    before:  wake word -> Intent Judge -> unchanged query -> normal pipeline
    after:   wake word -> normal pipeline

The user's request must arrive at shared admission byte-identical.

WHY THESE TESTS ARE STRUCTURAL, NOT VACUOUS
===========================================
`new_input` is a closure inside `start_server` and cannot be imported, so
`_VoiceHarness` AST-parses `server.py`, extracts the REAL `new_input` source and
executes it. The genuine guards (`_find_wake_word`, `_strip_wake_word`,
`reject_reason`, `_is_recent_tts_echo`, `_hot_window_is_active`,
`_hot_window_candidate`, `_STOP_PHRASE_RE`) come from the real module; only 23
genuinely-external names are stubbed, and every stub RECORDS its calls. So the
tests assert on what production code actually did, not on a reimplementation.
Re-inserting the judge into `server.py` fails the absence guards and the
behavioural test simultaneously.
"""

import ast
import asyncio
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.getcwd())

import source.server.server as srv  # noqa: E402
from source.server.speech_filters import reject_reason  # noqa: E402

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")
INDEX_HTML = os.path.join(os.getcwd(), "source", "server", "ui", "index.html")

# Names that must never come back.
BANNED_IN_SERVER = (
    "intent_judge",
    "judge_enabled",
    "ambient_buffer",
    "judge_client",
    "AmbientBuffer",
    "JudgeClient",
    "_intent_judge",
    "judge_not_directed",
    "intent_judge_rewrote",
)


# ---------------------------------------------------------------------------
# Harness: execute the REAL new_input.
# ---------------------------------------------------------------------------


def _extract_new_input():
    with open(SERVER_PY, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    start_server = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "start_server"
    )
    node = next(
        n for n in ast.walk(start_server)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "new_input"
    )
    lines = source.splitlines()
    import textwrap
    return textwrap.dedent("\n".join(lines[node.lineno - 1 : node.end_lineno]))


class _Recorder:
    """Stub that records every call and returns a benign value."""

    def __init__(self, name, log, result=None):
        self._name = name
        self._log = log
        self._result = result

    def __call__(self, *args, **kwargs):
        self._log.append((self._name, args, kwargs))
        return self._result

    def __repr__(self):
        return "<stub %s>" % self._name


class _AsyncRecorder(_Recorder):
    """Recorder for coroutine functions production awaits."""

    async def __call__(self, *args, **kwargs):
        self._log.append((self._name, args, kwargs))
        return self._result


class _VoiceHarness:
    """Drive the real `new_input` end-of-utterance."""

    def __init__(self, transcript):
        self.calls = []
        log = self.calls
        source = _extract_new_input()

        # Real guards, taken from the production module.
        namespace = {
            "reject_reason": reject_reason,
            "_find_wake_word": srv._find_wake_word,
            "_strip_wake_word": srv._strip_wake_word,
            "_is_recent_tts_echo": srv._is_recent_tts_echo,
            "_hot_window_is_active": srv._hot_window_is_active,
            "_hot_window_candidate": srv._hot_window_candidate,
            "_STOP_PHRASE_RE": srv._STOP_PHRASE_RE,
            "_redact_text": srv._redact_text,
            "_automatic_memory_enabled": lambda *a, **k: False,
            "_begin_single_turn": _Recorder("_begin_single_turn", log),
            "_mark_turn_dispatch": _Recorder("_mark_turn_dispatch", log),
            # Round W: the voice path now re-arms the watchdog clock through the
            # shared helper. Use the REAL function - it is pure (touches only
            # server_state and time), so a stub would hide a real regression.
            "_stamp_progress": srv._stamp_progress,
            "_pending_push": _Recorder("_pending_push", log),
            # ROUND ZB-P1: the admission gate snapshots/restores the running
            # turn's watchdog baseline when it defers instead of dispatching.
            "snapshot_turn_baseline": srv.snapshot_turn_baseline,
            "restore_turn_baseline": srv.restore_turn_baseline,
            "_play_chirp": _Recorder("_play_chirp", log),
            "_record_fresh_screen_evidence": _Recorder(
                "_record_fresh_screen_evidence", log
            ),
            "_screen_context_prefix": _Recorder(
                "_screen_context_prefix", log, ""
            ),
            "_log_screen_call_ledger": _Recorder("_log_screen_call_ledger", log),
            # ROUND ZB-PE: the voice path consults a parked question before the
            # brain. These tests have none parked, so the resolver must decline
            # and there must be no reminder - exactly the ordinary-speech state.
            "_pending_resolve": lambda *a, **k: None,
            "_pending_reminder_if_any": lambda *a, **k: None,
            "STATUS_IDLE": "idle",
            "STATUS_THINKING": "thinking",
            "STATUS_LISTENING": "listening",
            "STATUS_ERROR": "error",
            "api_pools": types.SimpleNamespace(
                cloud_brain_enabled=lambda: False,
                mark_failure=lambda *a, **k: None,
                mark_success=lambda *a, **k: None,
            ),
            "asyncio": asyncio,
            "time": types.SimpleNamespace(
                time=lambda: 1000.0, monotonic=lambda: 0.0
            ),
            "build_memory_context": _Recorder("build_memory_context", log, ""),
            "extract_and_remember_automatic": _Recorder(
                "extract_and_remember_automatic", log
            ),
            "format_memory_response": _Recorder(
                "format_memory_response", log
            ),
            "parse_memory_command": _Recorder("parse_memory_command", log),
            "is_screen_request": _Recorder("is_screen_request", log, False),
            "screen_response": _Recorder("screen_response", log, ""),
            "route_command": _Recorder("route_command", log),
            "emit_input_terminal": _Recorder("emit_input_terminal", log),
            "transcribe_wav": _Recorder("transcribe_wav", log, transcript),
            "pcm_to_wav": _Recorder("pcm_to_wav", log, b""),
            "log_action": _Recorder("log_action", log),
            # The 23 externals.
            "_ACTIVITY_QUERY_RE": None,
            "_LIVE_STATE_QUERY_RE": None,
            "_flog": types.SimpleNamespace(
                info=lambda *a, **k: None,
                debug=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
            ),
            "append_outcome": _Recorder("append_outcome", log),
            "chat_append": _Recorder("chat_append", log),
            "close_hot_window": _Recorder("close_hot_window", log),
            "open_hot_window": _Recorder("open_hot_window", log),
            "handle_special_command": _Recorder("handle_special_command", log),
            "log_direct": _Recorder("log_direct", log),
            "old_input": _AsyncRecorder("old_input", log),
            "publish_status": _Recorder("publish_status", log),
            "queue_direct_response": _Recorder("queue_direct_response", log),
            "response_is_active": _Recorder("response_is_active", log, False),
            "turn_is_busy": _Recorder("turn_is_busy", log, False),
            "dispatch_text": _AsyncRecorder("dispatch_text", log),
            "trace": _Recorder("trace", log),
        }

        queue = []
        stt = types.SimpleNamespace(
            start=lambda: None, stop=lambda: None, text=lambda: transcript,
        )
        interp = types.SimpleNamespace(
            stt=stt,
            tts=types.SimpleNamespace(is_playing=lambda: False),
            messages=[],
        )
        output_queue = types.SimpleNamespace(
            sync_q=types.SimpleNamespace(
                put=lambda item: queue.append(item)
            )
        )
        server_state = {
            "is_paused": False,
            "pending_input": None,
            "last_wake_time": 0.0,
            "last_wake_text": "",
            "hot_window_seconds": 30,
            "hot_window_active": False,
            "hot_window_expires_at": 0.0,
            "hot_window_timer": None,
            "hot_window_lock": threading.Lock(),
            "recent_tts_texts": [],
            "tts_echo_buffer": "",
            "tts_echo_turn_active": False,
            "memory_store": None,
            "turn_channel": "",
            "turn_reply_token": None,
            "telegram_turn_mute": False,
            "turn_log": None,
            "last_progress": 0.0,
            "brain_router": types.SimpleNamespace(
                new_turn=lambda *a, **k: None,
                apply_for=lambda *a, **k: ("fixture", None, "fixture-model"),
                failover_to_next=lambda *a, **k: None,
                live_serving_state=lambda: {},
            ),
            "event_loop": None,
        }
        self_obj = types.SimpleNamespace(
            stt=stt,
            tts=interp.tts,
            output_queue=output_queue,
            messages=[],
            audio_chunks=[],
            play_audio=False,
        )
        namespace.update(
            {
                "interpreter": interp,
                "server_state": server_state,
                "self": self_obj,
            }
        )

        exec(compile(source, SERVER_PY, "exec"), namespace)
        self.new_input = namespace["new_input"]
        self.self_obj = self_obj
        self.queue = queue
        self.server_state = server_state
        self.transcript = transcript

    def run(self):
        asyncio.run(self.new_input(self.self_obj, {"end": True}))

    def names(self):
        return [name for name, _, _ in self.calls]

    def first_args(self, name):
        for called, args, _ in self.calls:
            if called == name:
                return args
        raise AssertionError("%r was never called" % name)

    def ignored(self):
        return any(
            isinstance(item, dict) and item.get("ignored") for item in self.queue
        )


# ---------------------------------------------------------------------------
# 1. Absence guards.
# ---------------------------------------------------------------------------


class JudgeAbsenceTests(unittest.TestCase):
    def setUp(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_server_py_has_no_judge_references_at_all(self):
        code = "\n".join(
            line for line in self.source.splitlines()
            if not line.strip().startswith("#")
        )
        for banned in BANNED_IN_SERVER:
            self.assertNotIn(
                banned, code,
                "server.py still references %r - the judge was removed in "
                "Round U" % banned,
            )

    def test_the_judge_module_is_deleted(self):
        self.assertFalse(
            os.path.exists(os.path.join(os.getcwd(), "source", "server",
                                        "intent_judge.py")),
            "intent_judge.py must not come back",
        )

    def test_server_py_imports_cleanly_without_the_judge(self):
        # A stray import would raise at boot; prove the module still loads.
        import importlib
        importlib.reload(srv)

    def test_no_replacement_classifier_was_introduced(self):
        """No new judge-shaped gate may appear on the voice path."""
        code = "\n".join(
            line for line in self.source.splitlines()
            if not line.strip().startswith("#")
        )
        for banned in (
            "classify_intent", "intent_classifier", "judge_model",
            "route_by_intent", "request_type_switch", "model_tier",
            "escalat", "classify(",
        ):
            self.assertNotIn(
                banned, code,
                "a replacement classifier/tier switch (%r) was introduced; "
                "Round U removed the judge and added nothing" % banned,
            )

    def test_the_provider_chain_is_untouched(self):
        """The judge was unrelated to fallback routing; prove it still is."""
        from source.server import brain_router
        self.assertEqual(brain_router.LOCAL_MODEL, "ollama_chat/qwen3:8b")
        self.assertIn("generativelanguage", brain_router.GEMINI_BASE)
        self.assertIn("groq", brain_router.GROQ_BASE)
        self.assertIn("openrouter", brain_router.OPENROUTER_BASE)


# ---------------------------------------------------------------------------
# 2 + 3. The real voice path: wake gate intact, request unchanged.
# ---------------------------------------------------------------------------


class VoicePathBehaviourTests(unittest.TestCase):
    def test_no_wake_word_is_still_ignored(self):
        harness = _VoiceHarness("open the calculator")
        harness.run()
        self.assertTrue(harness.ignored(), "a wake-less utterance must be ignored")
        self.assertNotIn(
            "handle_special_command", harness.names(),
            "a wake-less utterance must never reach the pipeline",
        )

    def test_wake_word_reaches_the_pipeline(self):
        harness = _VoiceHarness("friday what is 17 times 23")
        harness.run()
        self.assertFalse(harness.ignored())
        self.assertIn("dispatch_text", harness.names())

    def test_the_request_reaches_the_pipeline_unchanged(self):
        """THE INVARIANT: the user's words arrive exactly as spoken."""
        harness = _VoiceHarness("friday what is 17 times 23")
        harness.run()
        self.assertEqual(
            harness.first_args("dispatch_text")[0],
            "what is 17 times 23",
            "the request must arrive unchanged - no judge rewrite, no mangling",
        )

    def test_request_survives_with_punctuation_and_casing(self):
        harness = _VoiceHarness("Friday, open the calculator!")
        harness.run()
        self.assertEqual(
            harness.first_args("dispatch_text")[0], "open the calculator"
        )

    def test_wake_word_only_is_still_ignored(self):
        harness = _VoiceHarness("friday")
        harness.run()
        self.assertTrue(harness.ignored())

    def test_hallucination_gate_still_runs_first(self):
        # The gate rejects this before the wake-word gate is even consulted.
        #
        # ROUND ZB-L0: this transcript used to be "friday please play some
        # music", which was ONLY rejected because of the bug being fixed in
        # this round - the optional-bracket caption rule matched the bare word
        # "music". "Please play some music" is ordinary speech, so after the
        # fix it correctly reaches the wake gate and dispatches. The INTENT of
        # this test is the ORDER (gate before wake-word gate), not that
        # particular string, so it now uses a genuine caption artifact,
        # "[music]", which nothing but the hallucination gate can reject.
        harness = _VoiceHarness("friday [music]")
        harness.run()
        self.assertTrue(harness.ignored())
        self.assertNotIn("dispatch_text", harness.names())

    def test_ordinary_speech_containing_music_is_no_longer_rejected(self):
        """The false positive that made the gate above unreliable."""
        harness = _VoiceHarness("friday play some music")
        harness.run()
        self.assertFalse(harness.ignored())
        self.assertIn("dispatch_text", harness.names())

    def test_repetitive_speech_still_rejected(self):
        harness = _VoiceHarness("thank you. thank you. thank you.")
        harness.run()
        self.assertTrue(harness.ignored())

    def test_no_judge_style_call_appears_on_the_voice_path(self):
        harness = _VoiceHarness("friday what is the time")
        harness.run()
        for banned in ("judge", "_intent_judge", "ambient_buffer", "JudgeClient"):
            self.assertNotIn(banned, harness.names())
        self.assertNotIn("judge_not_directed", str(harness.calls))


# ---------------------------------------------------------------------------
# 4. HUD truth.
# ---------------------------------------------------------------------------


class HudTruthTests(unittest.TestCase):
    def setUp(self):
        with open(INDEX_HTML, encoding="utf-8", errors="replace") as handle:
            self.html = handle.read()

    def test_no_intent_judge_claims_remain(self):
        for banned in ("Intent Judge", "Ambient intent judge", "intent judge"):
            self.assertNotIn(
                banned, self.html,
                "the HUD still advertises the removed judge (%r)" % banned,
            )

    def test_the_llm_grid_is_still_well_formed(self):
        # Match the class prefix, not a fixed literal: the 2026-10-16 HUD pass
        # deliberately dropped the `connected` suffix because the HUD had no
        # probe behind it ("neutral startup statuses" / "replace unverified
        # startup claims"). Pinning `llm-pill connected` went stale on that
        # principled change and broke collection of the whole suite.
        self.assertEqual(self.html.count('class="llm-pill'), 3)

        # ...and hold the line on WHY it changed: an unprobed service must not
        # be advertised as connected. Configured != reachable != serving.
        self.assertNotIn(
            'class="llm-pill connected"', self.html,
            "the HUD claims 'connected' for an llm pill without a probe behind it",
        )

        for label in ("Ollama (Qwen3)", "Edge TTS", "Memory"):
            self.assertIn(label, self.html)

    def test_the_workflows_pane_still_lists_the_real_workflow(self):
        self.assertIn("Voice-command-to-action routing matrix", self.html)

    def test_the_removed_badge_is_the_only_list_item_that_went(self):
        # The pane keeps its one genuine workflow; the judge badge is gone.
        pane_start = self.html.index("ACTIVE SYSTEM WORKFLOWS")
        pane_end = self.html.index("</div>", self.html.index("list-container", pane_start))
        pane = self.html[pane_start:pane_end]
        self.assertEqual(pane.count('class="list-item"'), 1)


# ---------------------------------------------------------------------------
# 5. Round S coverage survived the removal.
# ---------------------------------------------------------------------------


class RoundSCoverageSurvivedTests(unittest.TestCase):
    def test_json_patch_removal_no_longer_imports_the_judge(self):
        path = os.path.join(os.getcwd(), "test_json_patch_removal.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        code = "\n".join(
            line for line in body.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn(
            "from source.server.intent_judge", code,
            "a test must not import the deleted module",
        )

    def test_malformed_json_coverage_still_exists_somewhere(self):
        path = os.path.join(os.getcwd(), "test_json_patch_removal.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        for needed in ("JSONDecodeError", "AuditLog", "load_config"):
            self.assertIn(needed, body)

    def test_the_deleted_test_files_are_gone(self):
        for name in ("test_intent_judge.py", "test_ambient_intent.py"):
            self.assertFalse(os.path.exists(os.path.join(os.getcwd(), name)))


# ---------------------------------------------------------------------------
# 6. Structural: the wake gate must still exist and still return.
# ---------------------------------------------------------------------------


class WakeGateStructureTests(unittest.TestCase):
    def setUp(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            self.source = handle.read()
        self.tree = ast.parse(self.source)

    def test_the_missing_wake_branch_still_exists_and_returns(self):
        found = False
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.If):
                continue
            rendered = ast.unparse(node.test)
            if "hot_window" in rendered and "found_wake" in rendered:
                body = "\n".join(ast.unparse(s) for s in node.body)
                if "wake_rejected reason=final_text_missing_wake" in body:
                    self.assertIn(
                        "return", ast.unparse(node.body[-1]),
                        "the wake-less branch must still return",
                    )
                    found = True
        self.assertTrue(found, "the wake-word rejection branch was removed")

    def test_no_judge_shaped_gate_sits_between_the_wake_gate_and_the_pipeline(self):
        """No JUDGE may be re-inserted where it used to be.

        Narrower and honest: legitimate gates remain in this span (notably
        "wake word only, nothing to say" -> ignore). What must never return is
        a gate that consults an intent/judge decision.
        """
        start_server = next(
            n for n in self.tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "start_server"
        )
        new_input = next(
            n for n in ast.walk(start_server)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "new_input"
        )
        gate_lineno = None
        pipeline_lineno = None
        for node in ast.walk(new_input):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value == "wake_rejected reason=final_text_missing_wake":
                    gate_lineno = node.lineno
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "dispatch_text"
            ):
                pipeline_lineno = node.lineno
        self.assertIsNotNone(gate_lineno, "wake gate not found")
        self.assertIsNotNone(pipeline_lineno, "pipeline call not found")
        self.assertLess(gate_lineno, pipeline_lineno)

        markers = ("judge", "directed", "ambient", "intent")
        for node in ast.walk(new_input):
            if not isinstance(node, ast.If):
                continue
            if not (gate_lineno < node.lineno < pipeline_lineno):
                continue
            rendered = ast.unparse(node).lower()
            for marker in markers:
                self.assertNotIn(
                    marker, rendered,
                    "a judge-shaped gate (%r) was re-inserted between the wake "
                    "gate and the pipeline: %s" % (marker, ast.unparse(node.test)),
                )

    def test_the_legitimate_empty_content_gate_survives(self):
        """Wake word with nothing to say must still be ignored."""
        harness = _VoiceHarness("friday")
        harness.run()
        self.assertTrue(harness.ignored())


if __name__ == "__main__":
    unittest.main(verbosity=2)
