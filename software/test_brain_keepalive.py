"""Regression tests for the local-brain keep-alive (2026-09-30).

Measured on this machine, a two-token reply to qwen3:8b:

    cold (just unloaded)  43.2 s
    after one keep-alive  2.2 s

Ollama unloads models after 5 minutes by default and FRIDAY idles between
turns, so every question after a pause paid a ~43-49s model load. That hidden
cost is what made captions come back empty, made vision look unreliable, and
produced "every thinking model I have is unavailable right now" - the brain was
never slow, it was being reloaded inside every timeout window.
"""

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from source.server import brain_keepalive as bk  # noqa: E402


class ModelNameTests(unittest.TestCase):
    def test_strips_litellm_prefixes(self):
        self.assertEqual(bk._model_name("ollama_chat/qwen3:8b"), "qwen3:8b")
        self.assertEqual(bk._model_name("ollama/qwen3:8b"), "qwen3:8b")

    def test_keeps_bare_tags(self):
        for tag in ("qwen3:8b", "qwen2.5:0.5b", "llama3.2:3b", "moondream:latest"):
            self.assertEqual(bk._model_name(tag), tag)

    def test_handles_empty(self):
        self.assertEqual(bk._model_name(""), "")
        self.assertEqual(bk._model_name(None), "")


class PingTests(unittest.TestCase):
    def test_ping_never_raises_and_reports_failure(self):
        # No server at this port: must return False, not explode.
        os.environ["OLLAMA_CHAT_URL"] = "http://127.0.0.1:1"
        try:
            self.assertIs(bk.ping_once("qwen3:8b", timeout=1), False)
        finally:
            os.environ.pop("OLLAMA_CHAT_URL", None)

    def test_ping_counts_and_records_state(self):
        before = bk.status()["pings"]
        bk.ping_once("qwen3:8b", timeout=1)
        self.assertGreaterEqual(bk.status()["pings"], before)

    def test_status_never_raises(self):
        status = bk.status()
        for key in ("enabled", "running", "model", "keep_alive", "why"):
            self.assertIn(key, status)

    def test_status_explains_the_measured_cost(self):
        # Self-awareness must be able to answer "why is the brain slow?".
        self.assertIn("49.3", bk.status()["why"])
        self.assertIn("2.2", bk.status()["why"])


class ConfigurationTests(unittest.TestCase):
    def test_interval_is_below_ollama_default_unload(self):
        # Ollama's default keep_alive is 5 minutes; the ping must beat that or
        # the model unloads between pings and nothing is gained.
        self.assertLess(bk.DEFAULT_INTERVAL_SECONDS, 300)

    def test_ping_asks_ollama_to_hold_the_model(self):
        self.assertTrue(bk.KEEP_ALIVE)
        self.assertNotEqual(str(bk.KEEP_ALIVE), "0")

    def test_ping_is_cheap(self):
        self.assertEqual(bk.PING_PROMPT, "ok")

    def test_can_be_disabled_by_env(self):
        os.environ["FRIDAY_BRAIN_KEEPALIVE"] = "0"
        try:
            self.assertFalse(bk.enabled())
        finally:
            os.environ.pop("FRIDAY_BRAIN_KEEPALIVE", None)

    def test_enabled_by_default(self):
        os.environ.pop("FRIDAY_BRAIN_KEEPALIVE", None)
        self.assertTrue(bk.enabled())


class ThreadLifecycleTests(unittest.TestCase):
    def test_start_and_stop_are_safe_and_idempotent(self):
        original = bk._state.get("thread")
        try:
            os.environ["FRIDAY_BRAIN_KEEPALIVE_INTERVAL"] = "0.05"
            first = bk.start("qwen3:8b", interval=0.05)
            self.assertIsNotNone(first)
            second = bk.start("qwen3:8b", interval=0.05)
            self.assertIs(first, second, "start() must not spawn duplicate threads")
            time.sleep(0.2)
            self.assertTrue(bk.status()["running"])
        finally:
            bk.stop()
            os.environ.pop("FRIDAY_BRAIN_KEEPALIVE_INTERVAL", None)
            bk._state["thread"] = original
        self.assertFalse(bk.status()["running"])


class SelfAwarenessWiringTests(unittest.TestCase):
    def test_self_state_exposes_keepalive(self):
        from source.server.self_awareness import self_state

        self.assertIn("brain_keepalive", self_state())

    def test_server_starts_the_keepalive(self):
        path = os.path.join(ROOT, "source", "server", "server.py")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        self.assertIn("brain_keepalive.start(", text)

    def test_boot_does_not_wait_for_the_brain(self):
        """warmup_services must stay brain-free so boot is fast; the keep-alive
        is what solves staleness instead."""
        path = os.path.join(ROOT, "main.py")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        warmup = text.split("def warmup_services", 1)[-1].split("\ndef ", 1)[0]
        self.assertNotIn("qwen3", warmup)
        self.assertNotIn("api/chat", warmup)


if __name__ == "__main__":
    unittest.main()
    print("PASS: brain keep-alive")
