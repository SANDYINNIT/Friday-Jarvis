"""Regression tests for model self-awareness + visible scripts (2026-09-30).

Sir: "Make it so I can see what script is it using, also I hope its self aware
with what models its using."

The concrete failure being fixed: asked "what feature are you using to listen to
my voice", FRIDAY replied only "I am using my voice recognition capabilities" -
a generic non-answer, because her prompt was STATIC and nothing told her which
engine/model was actually serving. The conversation stream likewise showed only
`TOOL #1 - writing code...` followed by `OUT #1: python.exe`, hiding the script.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from source.server import brain_router  # noqa: E402
from source.server import self_awareness  # noqa: E402
from source.server.status_ui import ChatBuffer  # noqa: E402


class LiveModelAwarenessTests(unittest.TestCase):
    """She must be able to name the REAL serving model, not guess."""

    def test_live_serving_state_never_raises_without_a_router(self):
        original = brain_router._LIVE_ROUTER.get("router")
        try:
            brain_router._LIVE_ROUTER["router"] = None
            state = brain_router.live_serving_state()
            self.assertFalse(state["ready"])
            self.assertIsNone(state["current_model"])
        finally:
            brain_router._LIVE_ROUTER["router"] = original

    def test_router_publishes_itself_for_self_awareness(self):
        original = brain_router._LIVE_ROUTER.get("router")
        try:
            router = brain_router.BrainRouter(enabled=False)
            self.assertIs(brain_router._LIVE_ROUTER["router"], router)
            self.assertTrue(brain_router.live_serving_state()["ready"])
        finally:
            brain_router._LIVE_ROUTER["router"] = original

    def test_runtime_models_names_every_subsystem(self):
        models = self_awareness.runtime_models()
        for section in ("brain", "stt", "tts", "vision"):
            self.assertIn(section, models)

    def test_model_names_are_strings_not_dataclass_reprs(self):
        """Regression: select_model() returns a ModelInfo, and printing its
        repr produced 'ModelInfo(name=...)' - a confident, wrong answer."""
        models = self_awareness.runtime_models()
        for value in (models["vision"]["local_model"], models["brain"]["registry_general"]):
            self.assertIsInstance(value, str)
            self.assertNotIn("ModelInfo(", value)
            self.assertNotIn("provider=", value)

    def test_stt_answer_names_the_real_engine_and_model(self):
        """The exact question Sir asked must be answerable with real names."""
        stt = self_awareness.runtime_models()["stt"]
        self.assertIn("faster-whisper", stt["local_engine"].lower())
        self.assertTrue(stt["local_model"], "the local STT model name must be reported")
        self.assertIn("RealtimeSTT", stt["local_engine"])

    def test_stt_cloud_chain_is_reported_first(self):
        stt = self_awareness.runtime_models()["stt"]
        self.assertIn("stt_groq", stt["cloud_chain_tried_first"])
        self.assertIn("gemini_stt", stt["cloud_chain_tried_first"])

    def test_model_report_mentions_all_four_subsystems(self):
        report = self_awareness.model_report()
        for needle in ("Thinking", "Listening", "Speaking", "Vision"):
            self.assertIn(needle, report)

    def test_model_report_includes_real_stt_model_name(self):
        stt_model = self_awareness.runtime_models()["stt"]["local_model"]
        self.assertIn(stt_model, self_awareness.model_report())

    def test_self_state_exposes_models(self):
        self.assertIn("models", self_awareness.self_state())

    def test_who_am_i_names_the_serving_model(self):
        text = self_awareness.who_am_i()
        self.assertIn("serving", text.lower())


class ScriptVisibilityTests(unittest.TestCase):
    """Sir must SEE which script is running, not just 'python.exe'."""

    def test_upsert_tool_replaces_instead_of_appending(self):
        chat = ChatBuffer()
        chat.upsert_tool("TOOL #1 - writing code...", key="tool-1-code", status="streaming")
        chat.upsert_tool("TOOL #1 - script:\nprint(1)", key="tool-1-code", status="complete")
        rows = chat.snapshot()
        self.assertEqual(len(rows), 1, "one keyed row, not one row per fragment")
        self.assertIn("print(1)", rows[0]["content"])
        self.assertEqual(rows[0]["status"], "complete")

    def test_upsert_tool_keeps_distinct_keys_separate(self):
        chat = ChatBuffer()
        chat.upsert_tool("script", key="tool-1-code")
        chat.upsert_tool("output", key="tool-1-out")
        self.assertEqual(len(chat.snapshot()), 2)

    def test_upsert_tool_shows_growing_script(self):
        chat = ChatBuffer()
        chat.upsert_tool("import os", key="k", status="streaming")
        chat.upsert_tool("import os\nprint(os.getcwd())", key="k", status="streaming")
        chat.upsert_tool("import os\nprint(os.getcwd())", key="k", status="complete")
        content = chat.snapshot()[0]["content"]
        self.assertIn("getcwd", content, "the newest full script must be visible")

    def test_upsert_tool_never_raises_on_empty_content(self):
        chat = ChatBuffer()
        chat.upsert_tool("first", key="k")
        chat.upsert_tool("", key="k")
        self.assertEqual(chat.snapshot()[0]["content"], "first")

    def test_last_script_is_safe_when_nothing_ran(self):
        result = self_awareness.last_script()
        self.assertIn("ran", result)
        if not result["ran"]:
            self.assertIn("note", result)


class PromptWiringTests(unittest.TestCase):
    """The generated kernel code must expose these helpers and compile."""

    def test_setup_code_compiles_and_injects_helpers(self):
        from source.server.profiles.default import setup_code

        compile(setup_code, "<setup_code>", "exec")
        self.assertIn("model_report", setup_code)
        self.assertIn("last_script", setup_code)

    def test_prompt_teaches_the_voice_in_answer(self):
        from source.server.profiles.default import system_message_template

        text = system_message_template
        self.assertIn("faster-whisper", text.lower())
        self.assertIn("base.en", text)
        self.assertIn("model_report", text)


if __name__ == "__main__":
    unittest.main()
    print("PASS: model self-awareness + script visibility")


class RunawayScriptGuardTests(unittest.TestCase):
    """A script with an unbounded loop must never hang a turn forever.

    Observed live on 2026-09-30: after a Groq 429 failover the brain wrote
    python that printed "Task is not complete. Continuing..." forever. No `end`
    event ever arrived, so the tool counter never advanced, the tool-cap guard
    could not fire, and FRIDAY never answered at all.
    """

    def _server_text(self):
        import os as _os
        path = _os.path.join(ROOT, "source", "server", "server.py")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def test_flood_and_wallclock_limits_exist(self):
        text = self._server_text()
        self.assertIn("TOOL_FLOOD_EVENT_CAP", text)
        self.assertIn("TOOL_WALL_CLOCK_LIMIT", text)

    def test_limits_are_env_overridable(self):
        text = self._server_text()
        self.assertIn("FRIDAY_TOOL_FLOOD_CAP", text)
        self.assertIn("FRIDAY_TURN_WALL_CLOCK", text)

    def test_guard_actually_stops_the_turn(self):
        text = self._server_text()
        self.assertIn("turn_guard", text)
        self.assertIn("flood_stopped", text)
        self.assertIn("RUNAWAY SCRIPT STOPPED", text)

    def test_guard_speaks_instead_of_going_silent(self):
        """A killed turn produces no assistant text, so she must say so."""
        text = self._server_text()
        self.assertIn("flood_wrap", text)
        self.assertIn("queue_speech_only(flood_wrap)", text)

    def test_guard_resets_each_turn(self):
        text = self._server_text()
        self.assertIn('turn_guard["console_events"] = 0', text)
        self.assertIn('turn_guard["flood_stopped"] = False', text)


class ScriptCaptureKeyTests(unittest.TestCase):
    """The script-capture bug: FRIDAY read the WRONG payload key.

    `tool_code_parts` had been fed from `output.get("code")`, but Open
    Interpreter sends the script on a `code` event under `content`. Proven by
    logging every output type live:
        type='code' format='python' keys=['content','format','role','type']
    So the script was NEVER captured, the "TOOL #n result" row was dead code,
    and Sir only ever saw "OUT #1: python.exe". This locks the fix in place.
    """

    def _server_text(self):
        path = os.path.join(ROOT, "source", "server", "server.py")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def test_capture_reads_the_content_key(self):
        text = self._server_text()
        self.assertIn('output.get("code") or output.get("content")', text)

    def test_capture_logs_when_a_script_arrives(self):
        self.assertIn("tool script captured", self._server_text())

    def test_script_row_is_upserted_not_appended(self):
        text = self._server_text()
        self.assertIn('key=f"tool-{tool_counter[\'n\']}-code"', text)

    def test_end_event_does_not_capture(self):
        """The end event carries no content; capturing it would blank the row."""
        self.assertIn('not output.get("end")', self._server_text())

    def test_last_tool_script_is_recorded_for_self_awareness(self):
        self.assertIn("LAST_TOOL_SCRIPT", self._server_text())


class InternalControlTextTests(unittest.TestCase):
    """FRIDAY's own control instructions must never be spoken to the user.

    Observed on Telegram: the bot sent `interpreter.loop_message` verbatim
    ("I understand. I will continue only when the user's task is unfinished
    and stop immediately if the task is complete...") because the model echoed
    its own prompt. Same family as the loop-breaker suffix leaking.
    """

    def test_loop_message_echo_is_detected(self):
        from source.server.server import _is_internal_control_text

        echoed = (
            "I understand. I will continue only when the user's task is unfinished "
            "and stop immediately if the task is complete. I will not repeat "
            "acknowledgements or ask what to do next."
        )
        self.assertTrue(_is_internal_control_text(echoed))

    def test_loop_breakers_are_detected(self):
        from source.server.server import _is_internal_control_text

        for text in ("The task is done.", "The task is impossible.", "I have stopped."):
            self.assertTrue(_is_internal_control_text(text), text)

    def test_loop_guard_marker_is_detected(self):
        from source.server.server import _is_internal_control_text

        self.assertTrue(
            _is_internal_control_text("[System loop-guard] This turn has run 12 tools")
        )

    def test_real_answers_pass_through(self):
        from source.server.server import _is_internal_control_text

        for text in (
            "The left screen is running Claude Code, a terminal-based coding assistant.",
            "That app is a Jira board showing Sprint 42.",
            "I can see your browser with a Spanish news page open.",
        ):
            self.assertFalse(_is_internal_control_text(text), text)

    def test_clean_user_text_drops_internal_and_keeps_real(self):
        from source.server.server import _clean_user_text

        self.assertEqual(_clean_user_text("The task is done."), "")
        self.assertEqual(
            _clean_user_text("Here is your answer."), "Here is your answer."
        )


class ScreenFollowUpContextTests(unittest.TestCase):
    """Sir asked 'what is the left one about' and got a generic non-answer.

    The reference regex only matched literal words like 'screenshot', so
    demonstrative follow-ups matched nothing and no context was injected.
    """

    def _regex(self):
        from source.server.server import _SCREENSHOT_REFERENCE_RE

        return _SCREENSHOT_REFERENCE_RE

    def test_real_follow_ups_now_match(self):
        regex = self._regex()
        for question in (
            "What do you see?",
            "What is the left one about tho? Like what's being run? What does it do.",
            "But what does the application do that is open?",
            "what does the application do that is open on the left screen????",
            "what is on my right",
            "what does that app do",
        ):
            self.assertTrue(regex.search(question), question)

    def test_relative_clause_forms_match(self):
        """People say "the application THAT IS OPEN", not "the open
        application". Missing these produced a generic pixel description for a
        perfectly clear question."""
        regex = self._regex()
        for question in (
            "what does the application that is open do",
            "what is the app that is open used for",
            "what's the window that's open",
            "what does the app you have open do",
            "what does the open application do",
        ):
            self.assertTrue(regex.search(question), question)

    def test_unrelated_turns_do_not_match(self):
        regex = self._regex()
        for question in (
            "what is the weather tomorrow",
            "thanks",
            "play some music",
            "set a timer for 10 minutes",
        ):
            self.assertIsNone(regex.search(question), question)

    def test_context_ttl_is_configurable(self):
        import os

        from source.server.server import _SCREEN_CONTEXT_TTL_SECONDS

        self.assertGreater(_SCREEN_CONTEXT_TTL_SECONDS, 0)
        self.assertIsNotNone(os.environ.get("FRIDAY_SCREEN_CONTEXT_TTL", "900"))


class RunawayGuardDiscriminationTests(unittest.TestCase):
    """The guard must trip on REPEATED output, not on busy-but-progressing work.

    First version counted every console event and killed a legitimate vision
    turn, after which FRIDAY told the phone "the code I wrote started looping"
    - a confident false statement about work that never looped.
    """

    def _server_text(self):
        path = os.path.join(ROOT, "source", "server", "server.py")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def test_guard_tracks_consecutive_repeats(self):
        text = self._server_text()
        self.assertIn("same_run", text)
        self.assertIn("last_console", text)

    def test_flood_trip_uses_same_run_not_raw_count(self):
        text = self._server_text()
        self.assertIn('over_events = turn_guard["same_run"] > TOOL_FLOOD_EVENT_CAP', text)

    def test_message_distinguishes_loop_from_timeout(self):
        """Never claim a loop when the real cause was elapsed time."""
        text = " ".join(self._server_text().split())
        self.assertIn("repeating itself", text)
        self.assertIn("ran past my time", text)

    def test_module_level_filter_never_touches_start_server_local_logger(self):
        """`_flog` is a local of start_server(); a module-level helper that
        logs through it raises NameError in the Telegram delivery path."""
        from source.server.server import _clean_user_text

        # Must not raise even though no server is running.
        self.assertEqual(_clean_user_text("The task is done."), "")
