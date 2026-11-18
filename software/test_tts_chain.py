"""Tests for the TTS chain fix: cloud-first, no blocking local spawn, short bans.

Regression context (2026-09-30): a local-first chain called
ensure_edge_tts_server(), which blocks up to 45s waiting for a server that was
not running. Measured: 62 seconds between tts_start and first audio while cloud
TTS answers in 0.27s. The chain is now cloud-first, the local start is bounded,
and a TTS 429 no longer bans the voice for six hours.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.getcwd())

from source.server import gemini_tts as gt
from source.server import tts_bootstrap as tb


class TTSChainTests(unittest.TestCase):
    def test_startup_wait_is_bounded(self):
        # Must be far below the old hard-coded 45s.
        self.assertLessEqual(tb.MAX_STARTUP_WAIT_SECONDS, 20.0)
        self.assertGreater(tb.MAX_STARTUP_WAIT_SECONDS, 0)

    def test_local_pcm_can_refuse_to_start(self):
        """allow_start=False must not call the blocking bootstrap at all."""
        with mock.patch.object(tb, "ensure_edge_tts_server") as spawn:
            with mock.patch.object(tb, "probe", return_value=False):
                ok, pcm, _q, error = gt._local_pcm("hello", allow_start=False)
        self.assertFalse(ok)
        self.assertIsNone(pcm)
        self.assertIn("not running", error)
        spawn.assert_not_called()  # the whole point: never pay the wait

    def test_local_pcm_still_starts_when_allowed(self):
        with mock.patch.object(tb, "ensure_edge_tts_server", return_value=False) as spawn:
            with mock.patch.object(tb, "probe", return_value=False):
                with mock.patch.object(gt, "requests") as req:
                    req.post.return_value.status_code = 500
                    gt._local_pcm("hello", allow_start=True)
        spawn.assert_called()  # last-resort path may still try to start it

    def test_skip_path_does_not_latch_the_started_flag(self):
        """Regression: latching on the skip path disabled the local voice for
        the whole session, even after the server came up."""
        saved = gt._local_server_started
        gt._local_server_started = False
        try:
            with mock.patch.object(tb, "ensure_edge_tts_server"):
                with mock.patch.object(tb, "probe", return_value=False):
                    gt._local_pcm("hello", allow_start=False)
            self.assertFalse(gt._local_server_started,
                             "skip path must not latch _local_server_started")
        finally:
            gt._local_server_started = saved

    def test_cloud_is_tried_before_local(self):
        """Cloud must be the first thing speak() reaches.

        Checks the real call sites, ignoring prose/comments, so a docstring
        mention of the local voice cannot satisfy (or break) the assertion.
        """
        import re

        source = open(gt.__file__, encoding="utf-8").read()
        body = source.split("def speak(self, text):", 1)[1].split("\n    def ", 1)[0]
        # strip comments so documentation cannot influence the ordering
        code = "\n".join(line for line in body.splitlines()
                         if not line.strip().startswith("#"))
        cloud_call = re.search(r'pool\(\s*"gemini_tts"\s*\)', code)
        local_call = re.search(r"=\s*_local_pcm\(", code)
        self.assertIsNotNone(cloud_call, "speak() must reach the cloud TTS pool")
        self.assertIsNotNone(local_call, "speak() must still have a local fallback")
        self.assertLess(cloud_call.start(), local_call.start(),
                        "cloud TTS must be attempted before the local voice")

    def test_tts_429_does_not_ban_for_six_hours(self):
        """Regression: gemini_tts called fail(key) with no cooldown, so a single
        429 muted the voice for 6 hours (both accounts observed at 360 min)."""
        source = open(gt.__file__, encoding="utf-8").read()
        self.assertIn("api_pools.RATE_LIMIT_COOLDOWN_SECONDS", source)
        self.assertNotIn("quota_pool.fail(key)", source)

    def test_record_stage_updates_module_and_instance(self):
        voice = gt.CloudFirstTTSVoice()
        voice._record_stage("gemini")
        self.assertEqual(gt.LAST_STAGE, "gemini")
        self.assertEqual(voice.LAST_STAGE, "gemini")


if __name__ == "__main__":
    unittest.main()
    print("PASS: tts chain")
