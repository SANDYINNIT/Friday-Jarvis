"""Telegram polling contract. The Light admission/drain uses test_request_reply_ownership."""

import asyncio

from source.server import remote_telegram


def _config():
    return remote_telegram.TelegramConfig(
        token="test-token", allowed_chat_ids=frozenset({"7"}),
    )


def _update(update_id=1, chat_id="7", text="status"):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text}}


def _spy_send(sent):
    """Async stand-in for `TelegramRemoteAdapter._send`: records, never sends.

    `_send` is awaited by `process_update`, so a plain lambda would raise
    `TypeError: object NoneType can't be used in 'await' expression` - which is
    itself a useful reminder that every transmission in this suite is patched.
    """
    async def _send(chat_id, text):
        sent.append((str(chat_id), text))
        return True
    return _send


class _NoNetwork:
    """Stand-in for remote_telegram.requests: any use is a test failure."""

    class Timeout(Exception):
        pass

    def __init__(self):
        self.calls = 0

    def _boom(self, *_a, **_k):
        self.calls += 1
        raise AssertionError("the test opened a network connection")

    post = _boom
    get = _boom


class TestIngressIsMockedAndSequential:
    def _adapter(self, handler, **kwargs):
        return remote_telegram.TelegramRemoteAdapter(
            handler, config=_config(), min_message_interval=0, **kwargs)

    def test_poll_delivers_one_message_and_advances_the_offset(self, monkeypatch):
        net = _NoNetwork()
        monkeypatch.setattr(remote_telegram, "requests", net)
        handled, sent = [], []

        def handler(text, chat_id, message=None):
            handled.append((text, chat_id))
            return "queued-ack"

        adapter = self._adapter(handler)
        adapter._send = _spy_send(sent)

        async def run():
            async def fake_api(method, params):
                assert method == "getUpdates"
                return [_update(10)]

            adapter._api = fake_api
            await adapter._poll_once()

        asyncio.run(run())
        assert handled == [("status", "7")]
        assert adapter._offset == 11
        # The ONLY transmission is the handler's own reply, via the patched
        # sender - `requests` was never touched.
        assert sent == [("7", "queued-ack")]
        assert net.calls == 0

    def test_two_updates_are_processed_one_after_another(self, monkeypatch):
        """One-message-at-a-time is the adapter's deliberate contract."""
        net = _NoNetwork()
        monkeypatch.setattr(remote_telegram, "requests", net)
        order = []

        def handler(text, chat_id, message=None):
            order.append("start:%s" % text)
            order.append("end:%s" % text)
            return "ok"

        adapter = self._adapter(handler)
        sent = []
        adapter._send = _spy_send(sent)

        async def run():
            async def fake_api(_method, _params):
                return [_update(1, text="one"), _update(2, text="two")]

            adapter._api = fake_api
            await adapter._poll_once()

        asyncio.run(run())
        assert order == ["start:one", "end:one", "start:two", "end:two"], (
            "the poll loop interleaved two handlers: %r" % (order,))
        assert adapter._offset == 3
        assert sent == [("7", "ok"), ("7", "ok")], (
            "each update produced exactly one reply, in order")

    def test_a_message_inside_the_rate_window_is_refused_not_run(self, monkeypatch):
        """Intentional rate limit: per-chat spacing, answered, never executed."""
        net = _NoNetwork()
        monkeypatch.setattr(remote_telegram, "requests", net)
        handled, sent = [], []

        def handler(text, chat_id, message=None):
            handled.append(text)
            return "ran"

        adapter = remote_telegram.TelegramRemoteAdapter(
            handler, config=_config(), min_message_interval=3600)
        adapter._send = _spy_send(sent)

        async def run():
            first = await adapter.process_update(_update(1, text="one"))
            second = await adapter.process_update(_update(2, text="two"))
            return first, second

        first, second = asyncio.run(run())
        assert first.ok is True and handled == ["one"]
        assert second.reason == "rate_limited"
        assert handled == ["one"], "the rate-limited message was executed"
        assert sent[-1][1].startswith("Rate limit exceeded")
        assert net.calls == 0

    def test_a_stranger_gets_no_reply_and_no_side_effect(self, monkeypatch):
        net = _NoNetwork()
        monkeypatch.setattr(remote_telegram, "requests", net)
        handled, sent = [], []
        adapter = self._adapter(lambda *a, **k: handled.append(a))
        adapter._send = _spy_send(sent)

        result = asyncio.run(adapter.process_update(
            {"update_id": 9, "message": {"chat": {"id": "8"}, "text": "hi"}}))
        assert result.reason == "unauthorized"
        assert handled == [] and sent == []
        assert net.calls == 0
