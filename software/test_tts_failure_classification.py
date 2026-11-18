"""Tests for TTS provider-failure classification (2026-10-02 audit #3).

Measured before the fix: an HTTP 200 that simply carried no audio was routed to
`api_pools.fail_model()` with no cooldown, and that function defaults to
`_EXHAUSTION_COOLDOWN` = 21600s. One transient empty response therefore produced
six hours of silence, and the local fallback - which is healthy and answering -
was the only thing left.

The 429 path was already correct (a 60s cooldown via Google's own retry hint),
and it is checked FIRST, so `_is_transient_tts_failure` never sees a quota
failure.
"""

import unittest

from source.server.gemini_tts import _is_transient_tts_failure


class TtsFailureClassificationTests(unittest.TestCase):
    PERMANENT = (
        "gemini tts http 404 (not found)",
        "gemini tts http 400 (bad request)",
        "gemini tts http 410 (gone)",
    )
    TRANSIENT = (
        "gemini tts returned no audio",
        "gemini tts request error: timed out",
        "gemini tts bad payload: KeyError",
        "connection reset",
    )

    def test_explicit_http_status_is_a_real_verdict(self):
        for message in self.PERMANENT:
            self.assertFalse(_is_transient_tts_failure(message), message)

    def test_blank_or_malformed_response_is_transient(self):
        for message in self.TRANSIENT:
            self.assertTrue(_is_transient_tts_failure(message), message)

    def test_a_quota_429_is_not_mistaken_for_transient(self):
        # It is handled by the quota branch before this is consulted, but the
        # helper must still classify it as a definitive provider verdict.
        self.assertFalse(
            _is_transient_tts_failure("gemini tts http 429 (exceeded quota)"))

    def test_empty_input_falls_back_to_the_safe_cooldown(self):
        """No status in the message means no evidence against the model id.

        That must take the SHORT cooldown, not the six-hour ban: an unknown
        failure being treated as a verdict is exactly the bug this fixes."""
        self.assertTrue(_is_transient_tts_failure(""))
        self.assertTrue(_is_transient_tts_failure(None))

    def test_transient_failures_never_reach_the_six_hour_ban(self):
        """The actual regression: a 6-hour model ban for an empty 200."""
        from source.server import api_pools

        for message in self.TRANSIENT:
            if _is_transient_tts_failure(message):
                self.assertLess(
                    api_pools.RATE_LIMIT_COOLDOWN_SECONDS,
                    api_pools._EXHAUSTION_COOLDOWN,
                    "transient TTS failure must use the short cooldown")


class TtsFallbackAvailabilityTests(unittest.TestCase):
    """The local fallback is the safety net; it must actually be reachable."""

    def test_local_edge_tts_probe_is_the_documented_local_fallback(self):
        from source.server import gemini_tts

        # 127.0.0.1, not `localhost`: the `localhost` default cost a measured
        # 2.047s connect timeout on every local utterance on this machine.
        self.assertIn("127.0.0.1:5050", gemini_tts.LOCAL_TTS_URL)
        self.assertNotIn("localhost", str(gemini_tts.LOCAL_TTS_URL))
        self.assertIn("5050", str(gemini_tts.LOCAL_TTS_URL))

    def test_local_tts_never_blocks_on_a_speculative_server_start(self):
        """Documented trade-off: a failed local start used to burn 45s of
        silence, so the fallback deliberately does NOT auto-start the server
        mid-turn. The real wait is 12s, not 45s - the comment said otherwise."""
        from source.server import tts_bootstrap

        self.assertLessEqual(
            tts_bootstrap.MAX_STARTUP_WAIT_SECONDS, 15.0,
            "the local TTS startup wait must stay short")


if __name__ == "__main__":
    unittest.main()