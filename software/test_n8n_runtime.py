"""Tests for the optional n8n integration (hermetic: fake transport, temp config)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.getcwd())

from source.server import n8n_runtime as n8n


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTP:
    def __init__(self, get_response=None, post_response=None):
        self.get_response = get_response or FakeResponse(200, {"status": "ok"})
        self.post_response = post_response or FakeResponse(200, {"ran": True})
        self.posts = []
        self.gets = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        return self.get_response

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers or {}})
        return self.post_response


def write_config(payload):
    path = os.path.join(tempfile.gettempdir(), "opencode", "n8n_test_config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


class N8NRuntimeTests(unittest.TestCase):
    def test_absent_config_means_disabled(self):
        config = n8n.load_config(environ={"FRIDAY_N8N_CONFIG": "nope-does-not-exist.json"}, path="nope-does-not-exist.json")
        self.assertFalse(config["enabled"])

    def test_workflows_enable_integration(self):
        config = n8n.load_config(environ={"FRIDAY_N8N": "1"},
                                path="nope.json")
        # No file -> no workflows -> still disabled.
        self.assertFalse(config["enabled"])
        self.assertIn("not configured", config["reason"])

    def test_env_workflow_url_enables(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_BASE_URL": "http://localhost:5678",
            "FRIDAY_N8N_WEBHOOK_MORNING": "http://localhost:5678/webhook/morning",
        }, path="nope.json")
        self.assertTrue(config["enabled"])
        self.assertIn("morning", config["workflows"])
        self.assertEqual(config["workflows"]["morning"]["webhook_url"],
                         "http://localhost:5678/webhook/morning")

    def test_master_switch_off_disables(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "0",
            "FRIDAY_N8N_WEBHOOK_X": "http://localhost:5678/webhook/x",
        }, path="nope.json")
        self.assertFalse(config["enabled"])

    def test_invalid_webhook_url_is_dropped(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_WEBHOOK_BAD": "not-a-url",
        }, path="nope.json")
        self.assertNotIn("bad", config["workflows"])

    def test_call_succeeds_and_unwraps_n8n_envelope(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_WEBHOOK_MORNING": "http://localhost:5678/webhook/morning",
        }, path="nope.json")
        http = FakeHTTP(post_response=FakeResponse(200, [{"json": {"opened": 3}}]))
        adapter = n8n.N8NAdapter(config=config, http_post=http.post, http_get=http.get)
        result = adapter.call("morning", {"day": "monday"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"], {"opened": 3})
        self.assertEqual(http.posts[0]["json"], {"day": "monday"})

    def test_call_unknown_workflow_reports_known(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_WEBHOOK_MORNING": "http://localhost:5678/webhook/morning",
        }, path="nope.json")
        http = FakeHTTP()
        adapter = n8n.N8NAdapter(config=config, http_post=http.post, http_get=http.get)
        result = adapter.call("nonexistent")
        self.assertFalse(result["ok"])
        self.assertIn("morning", result["error"])
        self.assertIn("fallback", result)

    def test_call_when_disabled_returns_fallback(self):
        adapter = n8n.N8NAdapter(config=n8n.load_config(environ={}, path="nope.json"))
        result = adapter.call("anything")
        self.assertFalse(result["ok"])
        self.assertIn("fallback", result)

    def test_transport_failure_never_raises(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_WEBHOOK_X": "http://localhost:5678/webhook/x",
        }, path="nope.json")

        def boom(*args, **kwargs):
            raise ConnectionError("n8n is down")

        adapter = n8n.N8NAdapter(config=config, http_post=boom, http_get=boom)
        result = adapter.call("x")
        self.assertFalse(result["ok"])
        self.assertIn("fallback", result)

    def test_http_error_is_reported(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_WEBHOOK_X": "http://localhost:5678/webhook/x",
        }, path="nope.json")
        http = FakeHTTP(post_response=FakeResponse(500, None, "boom"))
        adapter = n8n.N8NAdapter(config=config, http_post=http.post, http_get=http.get)
        result = adapter.call("x")
        self.assertFalse(result["ok"])
        self.assertIn("500", result["error"])

    def test_status_never_leaks_secret(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_SECRET": "supersecret",
            "FRIDAY_N8N_WEBHOOK_X": "http://localhost:5678/webhook/x",
        }, path="nope.json")
        http = FakeHTTP()

        def boom(*args, **kwargs):
            raise ConnectionError("n8n is down supersecret")

        adapter = n8n.N8NAdapter(config=config, http_post=boom, http_get=boom)
        adapter.call("x")
        self.assertNotIn("supersecret", repr(adapter.status()))
        self.assertNotIn("supersecret", adapter._last_error)

    def test_status_hides_url_when_disabled(self):
        adapter = n8n.N8NAdapter(config=n8n.load_config(environ={}, path="nope.json"))
        self.assertEqual(adapter.status()["url"], "")

    def test_connection_help_mentions_file_and_env(self):
        config = n8n.load_config(environ={}, path=os.path.join(tempfile.gettempdir(), "opencode", "friday_n8n.json"))
        adapter = n8n.N8NAdapter(config=config)
        help_text = adapter.connection_help()
        self.assertIn("n8n.json", help_text)
        self.assertIn("FRIDAY_N8N", help_text)
        self.assertIn("Webhook", help_text)

    def test_list_workflows_without_config_includes_help(self):
        adapter = n8n.N8NAdapter(config=n8n.load_config(environ={}, path="nope.json"))
        listing = adapter.list_workflows()
        self.assertFalse(listing["ok"])
        self.assertIn("help", listing)

    def test_timeout_is_clamped(self):
        self.assertEqual(n8n._bounded_timeout(9999), n8n.MAX_TIMEOUT_SECONDS)
        self.assertEqual(n8n._bounded_timeout("nope"), n8n.DEFAULT_TIMEOUT_SECONDS)
        self.assertGreaterEqual(n8n._bounded_timeout(0.01), 0.5)

    def test_unwrap_leaves_plain_payload(self):
        self.assertEqual(n8n._unwrap_n8n({"a": 1}), {"a": 1})
        self.assertEqual(n8n._unwrap_n8n([{"json": {"b": 2}}]), {"b": 2})

    def test_reachability_is_cached_not_reprobed(self):
        # Regression: STATE_TTL_SECONDS was documented but never applied, so
        # every available() call fired two live HTTP requests.
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_WEBHOOK_X": "http://localhost:5678/webhook/x",
        }, path="nope.json")
        http = FakeHTTP()
        adapter = n8n.N8NAdapter(config=config, http_post=http.post, http_get=http.get)
        adapter.available()
        first = len(http.gets)
        self.assertGreater(first, 0)
        for _ in range(5):
            adapter.available()
        self.assertEqual(len(http.gets), first, "probe was not cached")

    def test_cache_bypassed_when_forced(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_WEBHOOK_X": "http://localhost:5678/webhook/x",
        }, path="nope.json")
        http = FakeHTTP()
        adapter = n8n.N8NAdapter(config=config, http_post=http.post, http_get=http.get)
        adapter.available()
        first = len(http.gets)
        adapter.available(force=True)
        self.assertGreater(len(http.gets), first)

    def test_failed_call_invalidates_the_cache(self):
        config = n8n.load_config(environ={
            "FRIDAY_N8N": "1",
            "FRIDAY_N8N_WEBHOOK_X": "http://localhost:5678/webhook/x",
        }, path="nope.json")
        http = FakeHTTP()
        adapter = n8n.N8NAdapter(config=config, http_post=http.post, http_get=http.get)
        adapter.available()
        http.post_response = FakeResponse(503, None, "unavailable")
        adapter.call("x")
        before = len(http.gets)
        adapter.available()
        self.assertGreater(len(http.gets), before, "a failure should force a re-probe")

    def test_get_adapter_is_shared(self):
        first = n8n.get_adapter()
        second = n8n.get_adapter()
        self.assertIs(first, second)
        self.assertIsNot(n8n.get_adapter(reload=True), first)


if __name__ == "__main__":
    unittest.main()
    print("PASS: n8n runtime")
