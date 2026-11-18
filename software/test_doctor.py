"""Tests for the FRIDAY doctor (health probes, no secrets, never raises)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.getcwd())

from source.server import doctor


class DoctorTests(unittest.TestCase):
    def test_run_doctor_never_raises_without_brain_probes(self):
        report = doctor.run_doctor(probe_brain=False)
        for key in ("checked_at", "machine", "brain_tiers", "ollama", "stt_chain",
                    "tts", "ports", "keys", "notes"):
            self.assertIn(key, report)

    def test_summary_line_is_short_and_honest(self):
        line = doctor.summary_line()
        self.assertIn("FRIDAY health", line)
        self.assertLess(len(line), 300)

    def test_no_secrets_ever_appear(self):
        report = doctor.run_doctor(probe_brain=False)
        blob = repr(report) + doctor.summary_line()
        for secret_marker in ("gsk_", "sk-or-v1-", "AIza", "api_key", "token"):
            self.assertNotIn(secret_marker, blob, f"doctor leaked {secret_marker}")

    def test_openai_style_probe_reports_no_key(self):
        result = doctor._probe_openai_style("t", "https://example.invalid", "m", "")
        self.assertFalse(result["ok"])
        self.assertIn("no API key", result["detail"])

    def test_openai_style_probe_handles_transport_error(self):
        def boom(*args, **kwargs):
            raise ConnectionError("down")
        with mock.patch("requests.post", boom):
            result = doctor._probe_openai_style("t", "https://example.invalid", "m", "k")
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "ConnectionError")

    def test_gemini_probe_reports_no_key(self):
        result = doctor._probe_gemini("g", "gemini-3.5-flash", "")
        self.assertFalse(result["ok"])

    def test_port_probe_returns_bool(self):
        self.assertIn(doctor._probe_port(10101), (True, False))

    def test_short_flattens_text(self):
        self.assertEqual(doctor._short("a\n  b   c"), "a b c")

    def test_compact_mode_is_much_smaller(self):
        # Regression: the full payload flooded the model's context mid-turn and
        # left it unable to produce a spoken answer (empty assistant row).
        import json

        full = doctor.run_doctor(probe_brain=False, full=True)
        compact = doctor.run_doctor(probe_brain=False, full=False)
        assert len(json.dumps(compact)) < len(json.dumps(full))
        for key in ("ollama_reachable", "stt_ready", "tts_ok", "down_tiers", "notes"):
            assert key in compact
        # the compact form must not carry the bulky per-tier detail
        assert "brain_tiers" not in compact

    def test_summary_line_uses_compact_path(self):
        line = doctor.summary_line()
        assert "FRIDAY health" in line
        assert "{" not in line, "summary_line must stay prose, not dump a dict"

    def test_brain_tier_list_is_the_real_chain(self):
        names = [stage for stage, _b, _m, _p in doctor.BRAIN_STAGES]
        # groq fast/strong, then gemini, then openrouter
        self.assertEqual(names[0], "groq-fast")
        self.assertEqual(names[1], "groq-strong")
        self.assertIn("gemini-3.5-flash", names)
        self.assertTrue(any(n.startswith("openrouter") for n in names))

    def test_stt_chain_uses_registered_pool_names(self):
        # Regression guard: the chain used "stt_gemini" while the registry calls
        # it "gemini_stt", which silently disabled the whole Gemini STT stage.
        from source.server import api_pools

        for pool_name in doctor.STT_CHAIN:
            self.assertIn(pool_name, api_pools.POOL_SCHEMA, pool_name)


if __name__ == "__main__":
    unittest.main()
    print("PASS: doctor")
