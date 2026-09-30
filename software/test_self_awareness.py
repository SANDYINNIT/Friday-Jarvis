"""Tests for self-awareness: live state, capabilities, and honest limitations."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.getcwd())

from source.server import self_awareness as sa


class SelfAwarenessTests(unittest.TestCase):
    def test_self_state_shape(self):
        state = sa.self_state()
        for key in ("collected_at", "process", "machine", "local_brain", "telegram",
                    "n8n", "server", "skills", "cloud_pools"):
            self.assertIn(key, state)
        self.assertIn(state["process"]["pid"], range(1, 10 ** 9))

    def test_self_state_never_raises_when_services_are_down(self):
        # Every probe must degrade to a reported error, never an exception.
        with mock.patch.object(sa, "_ollama_models", return_value={"ok": False, "error": "down"}):
            with mock.patch.object(sa, "_port_bound", return_value=False):
                state = sa.self_state()
        self.assertFalse(state["local_brain"]["ok"])
        self.assertFalse(state["server"]["listening"])

    def test_who_am_i_is_readable_and_honest(self):
        text = sa.who_am_i()
        self.assertIn("FRIDAY self-report", text)
        self.assertIn("Skills I've saved", text)
        # n8n must be described truthfully, never assumed connected.
        self.assertIn("n8n", text)

    def test_capabilities_lists_every_advertised_family(self):
        caps = sa.capabilities()
        for family in ("computer_health", "task_list", "n8n_workflows", "self_awareness",
                       "self_extension", "windows_control", "web"):
            self.assertIn(family, caps)
        self.assertIn("system_health", caps["computer_health"]["functions"])
        self.assertIn("TaskStore", caps["task_list"]["import"])

    def test_limitations_are_populated_and_honest(self):
        limits = sa.limitations()
        self.assertTrue(limits["cannot"])
        self.assertTrue(limits["degrade_gracefully"])
        self.assertTrue(limits["honesty_rules"])
        joined = " ".join(limits["cannot"]).lower()
        self.assertIn("wmic", joined)
        self.assertIn("kill", joined)

    def test_skill_status_reports_the_latch_caveat(self):
        status = sa.skill_status()
        self.assertIn("skills", status)
        self.assertIn("restart", status["restart_note"])
        self.assertIn("how_to_create", status)

    def test_awareness_brief_is_short_and_states_boundaries(self):
        brief = sa.awareness_brief()
        self.assertIn("=== WHO YOU ARE", brief)
        self.assertIn("self_awareness", brief)
        # The brief must NOT try to embed volatile numbers that could go stale.
        self.assertNotIn("CPU", brief)
        self.assertLess(len(brief), 1200)

    def test_no_secrets_in_state(self):
        # Tokens/keys must never appear in anything the model can read.
        state = repr(sa.self_state()) + sa.who_am_i()
        self.assertNotIn("api_key", state)
        self.assertNotIn("token", state.lower().replace("telegram token", ""))


if __name__ == "__main__":
    unittest.main()
    print("PASS: self awareness")
