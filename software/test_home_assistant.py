import asyncio
import unittest
from unittest.mock import AsyncMock

from source.server.home_assistant import HomeAssistantAdapter, HomeAssistantConfig, load_config


class Response:
    def __init__(self, payload, error=None):
        self.payload, self.error = payload, error
    def raise_for_status(self):
        if self.error:
            raise RuntimeError(self.error)
    def json(self):
        return self.payload


class FakeHTTP:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    async def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return self.responses.pop(0)
    async def aclose(self):
        return None


class FakeWS:
    def __init__(self):
        self.sent = []
        self.incoming = [self._encode({"type": "auth_required"})]
        self.closed = False
    def _encode(self, value):
        return __import__("json").dumps(value)
    async def send(self, value):
        self.sent.append(__import__("json").loads(value))
        if self.sent[-1]["type"] == "auth":
            self.incoming.append(self._encode({"type": "auth_ok"}))
        else:
            self.incoming.append(self._encode({"id": self.sent[-1]["id"], "success": True, "result": {"ok": True}}))
    async def recv(self):
        while not self.incoming:
            await asyncio.sleep(0)
        return self.incoming.pop(0)
    async def close(self):
        self.closed = True


class HomeAssistantTests(unittest.TestCase):
    def config(self, **extra):
        values = {"FRIDAY_HOME_ASSISTANT": "1", "FRIDAY_HA_URL": "http://ha.local", "FRIDAY_HA_TOKEN": "secret", "FRIDAY_HA_ALLOWED_SERVICES": "light.turn_on"}
        values.update(extra)
        return load_config(values)

    def test_disabled_config(self):
        adapter = HomeAssistantAdapter(config=load_config({}))
        self.assertFalse(asyncio.run(adapter.start()))
        self.assertFalse(adapter.status()["enabled"])

    def test_url_token_and_allowlist(self):
        config = self.config()
        self.assertEqual(config.url, "http://ha.local")
        self.assertNotIn("secret", repr(HomeAssistantAdapter(config=config).status()))
        http = FakeHTTP([])
        adapter = HomeAssistantAdapter(config=config, http_client=http, confirmation_callback=lambda *_: True)
        denied = asyncio.run(adapter.call_service("switch", "turn_on"))
        self.assertIn("allowlisted", denied["error"])

    def test_states_are_bounded(self):
        http = FakeHTTP([Response([{"entity_id": str(i)} for i in range(5)])])
        adapter = HomeAssistantAdapter(config=self.config(), http_client=http)
        result = asyncio.run(adapter.list_states(2))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["states"]), 2)

    def test_call_success_failure_and_confirmation(self):
        http = FakeHTTP([Response([{"entity_id": "light.a"}]), Response({}, "bad")])
        confirm = AsyncMock(return_value=True)
        adapter = HomeAssistantAdapter(config=self.config(), http_client=http, confirmation_callback=confirm)
        success = asyncio.run(adapter.call_service("light", "turn_on", {"entity_id": "light.a"}))
        failure = asyncio.run(adapter.call_service("light", "turn_on"))
        self.assertTrue(success["ok"])
        self.assertFalse(failure["ok"])
        self.assertEqual(confirm.await_count, 2)
        denied = HomeAssistantAdapter(config=self.config(), http_client=FakeHTTP([]), confirmation_callback=lambda *_: False)
        self.assertIn("denied", asyncio.run(denied.call_service("light", "turn_on"))["error"])

    def test_websocket_response_correlation_and_shutdown(self):
        ws = FakeWS()
        adapter = HomeAssistantAdapter(config=self.config(), websocket_factory=lambda _: asyncio.sleep(0, result=ws))
        result = asyncio.run(adapter.websocket_request({"type": "get_states"}))
        self.assertTrue(result["ok"])
        asyncio.run(adapter.stop())
        self.assertTrue(ws.closed)


if __name__ == "__main__":
    unittest.main()
