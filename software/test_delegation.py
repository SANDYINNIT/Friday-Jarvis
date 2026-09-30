import os
import unittest
from unittest.mock import patch

from source.server.delegation import (
    MAX_CONTEXT_CHARS,
    MAX_RESULT_CHARS,
    DelegationRequest,
    DelegationResult,
    delegate,
    redact_secrets,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class DelegationTests(unittest.TestCase):
    def request(self, **changes):
        values = dict(provider="ollama", model="test", task="review", context="small")
        values.update(changes)
        return DelegationRequest(**values)

    def test_request_bounds(self):
        request = self.request(context="x" * (MAX_CONTEXT_CHARS + 10), timeout=999)
        self.assertEqual(len(request.context), MAX_CONTEXT_CHARS)
        self.assertEqual(request.timeout, 120.0)

    def test_secret_redaction(self):
        text = "api_key=abc123 Bearer secret password: swordfish"
        redacted = redact_secrets(text)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("swordfish", redacted)

    @patch("source.server.delegation.requests.post")
    def test_ollama_payload(self, post):
        post.return_value = FakeResponse({"message": {"content": "answer"}})
        result = delegate(self.request(task="Do this", context="only this"))
        payload = post.call_args.kwargs["json"]
        self.assertTrue(result.success)
        self.assertEqual(post.call_args.args[0], "http://localhost:11434/api/chat")
        self.assertEqual(payload["model"], "test")
        self.assertFalse(payload["stream"])
        self.assertIn("only this", payload["messages"][0]["content"])

    def test_unconfigured_provider_refusal(self):
        with patch("source.server.delegation.requests.post") as post:
            result = delegate(self.request(provider="openai-compatible"))
        self.assertFalse(result.success)
        self.assertIn("not configured", result.error)
        post.assert_not_called()

    @patch("source.server.delegation.requests.post", side_effect=TimeoutError("late"))
    def test_timeout_error_result(self, _post):
        result = delegate(self.request())
        self.assertFalse(result.success)
        self.assertIn("timed out", result.error)

    @patch("source.server.delegation.requests.post")
    def test_result_shape_and_result_bound(self, post):
        post.return_value = FakeResponse({"message": {"content": "x" * (MAX_RESULT_CHARS + 1)}})
        result = delegate(self.request())
        self.assertIsInstance(result, DelegationResult)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.model, "test")
        self.assertTrue(result.success)
        self.assertLessEqual(len(result.text), MAX_RESULT_CHARS)
        self.assertGreaterEqual(result.latency, 0)
        self.assertEqual(result.source, "ollama")


if __name__ == "__main__":
    unittest.main()
