import asyncio
import unittest
from unittest.mock import patch

from source.server.remote_telegram import TelegramRemoteAdapter, load_config


def update(update_id=1, chat_id="7", text="status"):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class TelegramTests(unittest.TestCase):
    def config(self, token="secret-token", ids="7"):
        return load_config({
            "FRIDAY_TELEGRAM_BOT_TOKEN": token,
            "FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS": ids,
            # Keep tests hermetic: never touch the real credentials file.
            "FRIDAY_CREDENTIALS_FILE": "NUL_TEST_MISSING_FILE",
        }, ignore_credentials=True)

    def test_disabled_without_token(self):
        adapter = TelegramRemoteAdapter(lambda *_: "ok", config=load_config({}, ignore_credentials=True))
        self.assertFalse(asyncio.run(adapter.start()))
        result = asyncio.run(adapter.process_update(update()))
        self.assertEqual(result.reason, "disabled")
        self.assertFalse(adapter.status()["enabled"])

    def test_allowlist_denial_is_silent(self):
        calls = []
        sends = []
        adapter = TelegramRemoteAdapter(
            lambda *_: calls.append(True), config=self.config(), min_message_interval=0)
        adapter._send = lambda chat, text: sends.append((chat, text))
        update_stranger = {"update_id": 1, "message": {
            "chat": {"id": "8"}, "text": "do things", "from": {"username": "Stranger"}}}
        result = asyncio.run(adapter.process_update(update_stranger))
        self.assertEqual(result.reason, "unauthorized")
        self.assertEqual(calls, [])
        self.assertEqual(sends, [])  # strangers: zero replies, zero actions

    def test_setup_mode_binds_owner_by_username(self):
        captured = {}

        async def handler(text, chat, message=None):
            captured["text"] = text
            return "bound"

        adapter = TelegramRemoteAdapter(
            handler,
            config=load_config({
                "FRIDAY_TELEGRAM_BOT_TOKEN": "secret-token",
                "FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS": "",
                "FRIDAY_TELEGRAM_OWNER_USERNAME": "@test_owner",
            }, ignore_credentials=True),
        )
        self.assertTrue(adapter.config.setup_mode)
        owner_update = {"update_id": 3, "message": {
            "chat": {"id": 9911}, "text": "its on?", "from": {"username": "test_owner"}}}
        sends = []

        with patch.object(adapter, "_send", side_effect=lambda chat, text: sends.append((chat, text))), \
             patch("source.server.remote_telegram.persist_config", lambda **kw: None):
            result = asyncio.run(adapter.process_update(owner_update))

        self.assertTrue(result.ok)
        self.assertEqual(adapter.config.allowed_chat_ids, frozenset({"9911"}))
        sent_chat, sent_text = sends[0]
        self.assertEqual(sent_chat, "9911")
        self.assertIn("9911", sent_text)
        self.assertIn("its on", sent_text.lower())

    def test_setup_mode_ignores_strangers_completely(self):
        adapter = TelegramRemoteAdapter(lambda *_: "ok", config=load_config({
            "FRIDAY_TELEGRAM_BOT_TOKEN": "secret-token",
            "FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS": "",
            "FRIDAY_TELEGRAM_OWNER_USERNAME": "@test_owner",
        }, ignore_credentials=True))

        sent = []

        async def fake_send(chat, text):
            sent.append((chat, text))

        adapter._send = fake_send
        stranger = {"update_id": 4, "message": {
            "chat": {"id": 5}, "text": "hi bot", "from": {"username": "SomebodyElse"}}}
        result = asyncio.run(adapter.process_update(stranger))
        self.assertEqual(result.reason, "setup_mode_unbound")
        # The identity helper may reply with the chat id, but NO binding and
        # the adapter stays unbound (no commands can ever run).
        self.assertTrue(adapter.config.setup_mode)
        self.assertEqual(adapter.config.allowed_chat_ids, frozenset())

    def test_offset_and_handler_response(self):
        sent = []
        calls = []

        def handler(text, chat, message=None):
            calls.append((text, chat))
            return "checked"

        adapter = TelegramRemoteAdapter(handler, config=self.config(), min_message_interval=0)

        async def run():
            with patch.object(adapter, "_api", side_effect=[[update(10)], []]) as api:
                await adapter._poll_once()
                self.assertEqual(adapter._offset, 11)
                await adapter._poll_once()
                self.assertEqual(api.call_args_list[1].args[1]["offset"], 11)
            return await adapter.process_update(update(10))

        with patch.object(adapter, "_send", side_effect=lambda chat, text: sent.append({"chat_id": chat, "text": text})):
            result = asyncio.run(run())
        self.assertTrue(result.ok)
        self.assertEqual(calls, [("status", "7"), ("status", "7")])
        self.assertEqual(sent[-1]["text"], "checked")

    def test_timeout_retry_and_shutdown(self):
        adapter = TelegramRemoteAdapter(lambda *_: "ok", config=self.config(), sleep=lambda _: asyncio.sleep(0))
        with patch.object(adapter, "_api", side_effect=[requests_timeout(), asyncio.CancelledError()]):
            async def run():
                task = asyncio.create_task(adapter._run())
                await asyncio.sleep(0)
                adapter._stop_event.set()
                await asyncio.gather(task, return_exceptions=True)
            asyncio.run(run())
        self.assertIn("timed out", adapter.status()["last_error"])

    def test_redaction(self):
        adapter = TelegramRemoteAdapter(lambda *_: "ok", config=self.config())
        async def run():
            with patch.object(adapter, "_api", side_effect=RuntimeError("secret-token leaked")):
                task = asyncio.create_task(adapter._run())
                await asyncio.sleep(0)
                adapter._stop_event.set()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())
        self.assertNotIn("secret-token", adapter.status()["last_error"])

    def test_shutdown(self):
        adapter = TelegramRemoteAdapter(lambda *_: "ok", config=self.config())
        async def run():
            with patch.object(adapter, "_api", side_effect=asyncio.CancelledError()):
                await adapter.start()
                await adapter.stop()
            return adapter.status()["running"]
        self.assertFalse(asyncio.run(run()))


def requests_timeout():
    import requests
    return requests.Timeout("network timeout")


if __name__ == "__main__":
    unittest.main()
