"""ROUND ZB-L0: a bare wake word must arm the bounded follow-up window.

Confirmed defect (server.py, the `clean_content == ""` branch): a bare
"Friday." published "Ready for a command" and returned WITHOUT calling
`open_hot_window()`. `open_hot_window()` was only ever called from the refresh
path (inside `if hot_window:`) and from two HTTP sites - never from the
bare-wake branch. The effect: the next utterance had to say "Friday" again,
which is exactly the interaction Sir reported on 2026-10-06.

The behaviour under test is narrow and local: this branch must call the EXISTING
`open_hot_window()` (same 30s default, same 10-60 clamp, same timer, still
refusing while paused) and then return through the unchanged path.

The real `_VoiceHarness` from test_intent_judge_removed.py is reused, so these
tests drive the genuine extracted `new_input` with the genuine wake, echo,
duplicate and window helpers. Window state is set directly on the harness's own
`server_state` - which is precisely what `open_hot_window()` mutates - so the
REAL `_hot_window_is_active` / `_hot_window_candidate` decide acceptance.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from source.server import server as srv  # noqa: E402
from source.server.speech_filters import reject_reason  # noqa: E402
from test_intent_judge_removed import _VoiceHarness  # noqa: E402


def _arm(harness, seconds=30.0):
    """Put the harness's window in the state `open_hot_window()` would leave."""
    state = harness.server_state
    state["hot_window_active"] = True
    state["hot_window_expires_at"] = time.monotonic() + seconds


def _expire(harness):
    state = harness.server_state
    state["hot_window_active"] = True
    state["hot_window_expires_at"] = time.monotonic() - 1.0


class BareWakeArmsWindowTests(unittest.TestCase):
    def test_bare_wake_word_calls_open_hot_window(self):
        harness = _VoiceHarness("friday.")
        harness.run()
        self.assertIn(
            "open_hot_window", harness.names(),
            "a bare wake word must arm the existing bounded follow-up window")
        self.assertTrue(harness.ignored(),
                        "the branch still ends the turn, as before")

    def test_bare_wake_word_also_publishes_ready(self):
        harness = _VoiceHarness("friday.")
        harness.run()
        statuses = [str(args[1]) for name, args, _ in harness.calls
                    if name == "publish_status"]
        self.assertTrue(any("Ready for a command" in s for s in statuses),
                        "the Ready for a command notice must be preserved")

    def test_wake_word_with_content_does_not_arm_the_window(self):
        """A real request must go straight through, not arm a window."""
        harness = _VoiceHarness("friday, hello")
        harness.run()
        self.assertNotIn("open_hot_window", harness.names())
        self.assertFalse(harness.ignored())
        self.assertIn("handle_special_command", harness.names())

    def test_wake_aliases_are_unaffected(self):
        for transcript in ("frida.", "fry day.", "fri-day.", "fire day."):
            with self.subTest(transcript=transcript):
                harness = _VoiceHarness(transcript)
                harness.run()
                self.assertIn("open_hot_window", harness.names())


class FollowUpWindowAcceptanceTests(unittest.TestCase):
    """Desired flow: bare "Friday." then a command WITHOUT the wake word."""

    def test_wakeless_followup_is_accepted_while_the_window_is_active(self):
        harness = _VoiceHarness("open opencode")
        _arm(harness)
        self.assertTrue(srv._hot_window_is_active(harness.server_state))
        harness.run()
        self.assertFalse(
            harness.ignored(),
            "a wake-less command inside the live window must be accepted")
        self.assertIn("handle_special_command", harness.names())

    def test_wakeless_followup_is_rejected_once_the_window_expires(self):
        harness = _VoiceHarness("open opencode")
        _expire(harness)
        self.assertFalse(srv._hot_window_is_active(harness.server_state))
        harness.run()
        self.assertTrue(
            harness.ignored(),
            "after expiry a wake-less utterance must be rejected again")
        self.assertNotIn("handle_special_command", harness.names())

    def test_wakeless_followup_is_rejected_with_no_window_at_all(self):
        harness = _VoiceHarness("open opencode")
        self.assertFalse(srv._hot_window_is_active(harness.server_state))
        harness.run()
        self.assertTrue(harness.ignored())
        self.assertNotIn("handle_special_command", harness.names())


class WindowSafetyRegressionsTests(unittest.TestCase):
    """Every pre-existing in-window protection must still hold."""

    def test_recent_tts_echo_is_still_rejected_inside_the_window(self):
        harness = _VoiceHarness("the answer is ready")
        _arm(harness)
        srv._remember_tts_text(harness.server_state, "The answer is ready.")
        harness.run()
        self.assertTrue(harness.ignored(),
                        "an echoed reply must still be suppressed")
        self.assertNotIn("handle_special_command", harness.names())

    def test_duplicate_protection_still_rejects_a_repeat(self):
        harness = _VoiceHarness("tell me more")
        _arm(harness)
        harness.server_state["last_wake_text"] = "tell me more"
        harness.server_state["last_wake_time"] = time.time()
        harness.run()
        self.assertTrue(harness.ignored(),
                        "a repeat inside 3s must still be rejected")
        self.assertNotIn("handle_special_command", harness.names())

    def test_empty_transcript_is_still_rejected(self):
        harness = _VoiceHarness("   ")
        _arm(harness)
        harness.run()
        self.assertTrue(harness.ignored())
        self.assertNotIn("handle_special_command", harness.names())

    def test_paused_state_still_blocks_window_candidates(self):
        harness = _VoiceHarness("continue")
        _arm(harness)
        harness.server_state["is_paused"] = True
        self.assertIsNone(srv._hot_window_candidate(
            "continue", harness.server_state, False))
        harness.run()
        self.assertTrue(harness.ignored())

    def test_hallucination_gate_still_runs_before_the_wake_window(self):
        """Gate ordering unchanged: a caption artifact is rejected even with a
        live window and even though it contains the wake word."""
        harness = _VoiceHarness("friday [music]")
        _arm(harness)
        harness.run()
        self.assertTrue(harness.ignored())
        self.assertNotIn("handle_special_command", harness.names())
        self.assertNotIn("_begin_single_turn", harness.names())

    def test_repetitive_speech_is_still_rejected_with_the_wake_word(self):
        # The ratio is computed over the WHOLE normalized transcript including
        # the wake word, so this needs enough repeats to clear 0.6:
        # 9 words / 3 unique = 0.667. A shorter version (7 words / 3 unique =
        # 0.571) is legitimately below the threshold and is NOT rejected.
        harness = _VoiceHarness(
            "friday thank you thank you thank you thank you")
        _arm(harness)
        harness.run()
        self.assertTrue(harness.ignored())
        self.assertNotIn("handle_special_command", harness.names())

    def test_a_bare_wake_plus_light_echo_is_not_repetition(self):
        """Documents the threshold honestly, so nobody tightens it by accident."""
        self.assertIsNone(reject_reason(
            "friday thank you thank you thank you"))

    def test_window_length_clamps_are_unchanged(self):
        from source.server.server import _configured_hot_window_seconds
        old = os.environ.pop("FRIDAY_HOT_WINDOW_SECONDS", None)
        try:
            self.assertEqual(_configured_hot_window_seconds(), 30.0)
            os.environ["FRIDAY_HOT_WINDOW_SECONDS"] = "4"
            self.assertEqual(_configured_hot_window_seconds(), 10.0)
            os.environ["FRIDAY_HOT_WINDOW_SECONDS"] = "600"
            self.assertEqual(_configured_hot_window_seconds(), 60.0)
        finally:
            os.environ.pop("FRIDAY_HOT_WINDOW_SECONDS", None)
            if old is not None:
                os.environ["FRIDAY_HOT_WINDOW_SECONDS"] = old


if __name__ == "__main__":
    unittest.main()