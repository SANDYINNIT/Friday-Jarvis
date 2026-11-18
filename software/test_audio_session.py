"""Exercise the shipped WebSocket, client and PortAudio callback contract without devices."""
import asyncio
import importlib
import json

import pytest

from source.server.audio_link import PROTOCOL
from source.server.status_ui import StatusBus, WebviewBridge
from test_request_reply_ownership import isolated_server

client = importlib.import_module("source.clients.light-python.client")


class DeviceStream:
    def __init__(self, callback):
        self.callback = callback
        self.active = True
        self.closed = False

    def start_stream(self):
        pass

    def is_active(self):
        return self.active

    def close(self):
        self.closed = True
        self.active = False


class FakeDevices:
    def __init__(self):
        self.streams = []

    def open(self, **kwargs):
        stream = DeviceStream(kwargs["stream_callback"])
        self.streams.append(stream)
        return stream


class SocketFixture:
    def __init__(self, runtime):
        self.runtime = runtime
        self.input = asyncio.Queue()
        self.output = asyncio.Queue()
        self.device = client.Device()
        self.device.websocket = self
        self.device.p = FakeDevices()
        self.device.play_audio = True
        self.device.audio_v2 = True

    async def send(self, value):
        await self.input.put({"type": "websocket.receive", "text": value})

    async def start(self):
        await self.input.put({"type": "websocket.connect"})
        self.worker = asyncio.create_task(self.runtime.server.app({
            "type": "websocket", "path": "/", "subprotocols": [PROTOCOL],
            "client": ("127.0.0.1", 12345),
        }, self.input.get, self.output.put))
        assert (await self.next())["subprotocol"] == PROTOCOL
        hello = await self.message()
        await self.device.handle_audio_message(hello)
        return hello

    async def next(self):
        return await asyncio.wait_for(self.output.get(), 2)

    async def message(self, expected=None):
        while True:
            value = json.loads((await self.next())["text"])
            if expected is None or expected == value.get("type"):
                return value

    async def close(self):
        await self.input.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.worker, 2)


async def until(condition):
    async def wait():
        while not condition():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 2)


def test_real_light_voice_receipts_reconnect_and_apply_failure(isolated_server, monkeypatch):
    runtime, _ = isolated_server
    link = runtime.server_state["audio_link"]
    bridge = WebviewBridge(StatusBus(), runtime)
    # This uses the shipped client class but never constructs PortAudio.
    monkeypatch.setattr(client.pyaudio, "PyAudio", FakeDevices)
    monkeypatch.setattr(runtime, "resume_owned_voice", lambda: None)

    async def scenario():
        await runtime.server.app.router.startup()
        first = SocketFixture(runtime)
        hello = await first.start()
        await until(lambda: link.voice_snapshot()["state"] == "applied")
        assert bridge.get_voice_screen()["listening"]["client_receipt"]["revision"] == 0
        duplicate = SocketFixture(runtime)
        await runtime.server.app({
            "type": "websocket", "path": "/", "subprotocols": [PROTOCOL],
            "client": ("127.0.0.1", 12346),
        }, duplicate.input.get, duplicate.output.put)
        assert await duplicate.next() == {"type": "websocket.close", "code": 1013}
        assert bridge.set_paused(True)["accepted"]
        pause = await first.message()
        assert pause["voice_paused"] is True
        assert bridge.get_voice_screen()["listening"]["client_receipt"]["state"] == "sent"
        assert bridge.set_paused(False)["accepted"]
        resume = await first.message()
        await first.device.handle_audio_message(resume)
        await first.device.handle_audio_message(pause)
        await until(lambda: link.voice_snapshot()["state"] == "applied")
        assert first.device.voice_paused_by_user is False
        assert not link.accept_voice(hello["audio_session"], {**pause, "applied": True})
        assert bridge.set_paused(True)["accepted"]
        pause = await first.message()
        await first.device.handle_audio_message(pause)
        await until(lambda: link.voice_snapshot()["state"] == "applied")
        assert first.device.voice_paused_by_user is True
        assert bridge.set_paused(False)["accepted"]
        resume = await first.message()
        def fail(_paused):
            raise OSError("synthetic input gate failure")
        first.device.apply_voice_input = fail
        await first.device.handle_audio_message(resume)
        await until(lambda: link.voice_snapshot()["state"] == "failed")
        assert first.device.voice_paused_by_user is True
        await first.close()
        assert link.voice_snapshot()["state"] == "disconnected"
        second = SocketFixture(runtime)
        replacement = await second.start()
        assert replacement["audio_session"] != hello["audio_session"]
        await until(lambda: link.voice_snapshot()["state"] == "applied")
        assert not link.accept_voice(hello["audio_session"], {**resume, "applied": True})
        assert second.device.voice_paused_by_user is False
        await second.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("end", ["drained", "cancelled", "failed", "disconnected"])
def test_real_light_pcm_and_client_terminal_outcomes(isolated_server, monkeypatch, end):
    from source.server.server import _AttemptOutput, notify_speech_stop
    runtime, _ = isolated_server
    link = runtime.server_state["audio_link"]
    bridge = WebviewBridge(StatusBus(), runtime)
    monkeypatch.setattr(client.pyaudio, "PyAudio", FakeDevices)

    async def scenario():
        await runtime.server.app.router.startup()
        socket = SocketFixture(runtime)
        hello = await socket.start()
        state = runtime.server_state
        token = state["playback_attempt"] = object()
        link.begin(token)
        runtime.on_tts_chunk(b"\x01\x02" * 16, token)
        pcm = await socket.message("audio_pcm")
        await socket.device.handle_audio_message(pcm)
        stream, = socket.device.p.streams
        assert stream.callback(None, 16, None, 0)[1] == client.pyaudio.paContinue
        await socket.device.observe_output()
        await until(lambda: link.playback.bytes_reported == 32)
        def enqueue(frame):
            runtime.output_queue.sync_q.put(_AttemptOutput(token, frame, playback=True))
        link.finish_synthesis(token, enqueue)
        await socket.device.handle_audio_message(await socket.message("audio_end"))
        assert bridge.get_voice_screen()["speaking"]["playback"] == "buffered"
        assert stream.callback(None, 16, None, 0)[1] == client.pyaudio.paComplete
        await socket.device.observe_output()
        assert link.playback.outcome == "buffered"  # API still has a buffer
        if end == "drained":
            stream.active = False
            await socket.device.observe_output()
        elif end == "cancelled":
            notify_speech_stop(state, "test", queue=runtime.output_queue)
            assert bridge.get_voice_screen()["speaking"]["playback"] == "stop_requested"
            await socket.device.handle_audio_message(await socket.message())
        elif end == "failed":
            def fail():
                raise OSError("synthetic output failure")
            stream.is_active = fail
            await socket.device.observe_output()
        else:
            await socket.close()
        await until(lambda: link.playback.outcome == end)
        assert not link.accept_playback(hello["audio_session"], {
            **pcm, "event": "drained", "bytes": 32})
        if end != "disconnected":
            assert stream.closed
            await socket.close()
        replacement = SocketFixture(runtime)
        await replacement.start()
        assert runtime.server_state["playback_attempt"] is None
        runtime.on_tts_chunk(b"\x01\x02" * 16, token)
        assert link.outgoing(link.session, pcm, True) is None
        assert replacement.device.p.streams == []
        if end == "cancelled":
            new = state["playback_attempt"] = object()
            link.begin(new)
            runtime.on_tts_chunk(b"\x03\x04" * 16, new)
            newer = await replacement.message("audio_pcm")
            await replacement.device.handle_audio_message(newer)
            # Old PortAudio callbacks carry only their own now-retired buffers.
            assert stream.callback(None, 16, None, 0)[1] == client.pyaudio.paAbort
            assert not link.accept_playback(hello["audio_session"], {
                **pcm, "event": "cancelled", "bytes": 32})
            assert replacement.device.playback.id == newer["playback"]
            replacement.device.playback.close()
        await replacement.close()

    asyncio.run(scenario())
