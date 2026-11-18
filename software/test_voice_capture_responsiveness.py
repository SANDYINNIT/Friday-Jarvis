"""Real capture coroutine, fake input/VAD: it must not block output/control IO."""
import asyncio
import importlib
import threading

import pytest

client = importlib.import_module('source.clients.light-python.client')


@pytest.mark.parametrize('blocked_stage', ['read', 'vad'])
def test_capture_keeps_output_and_control_loop_responsive(monkeypatch, blocked_stage):
    entered = threading.Event()
    release = threading.Event()
    inside = threading.Event()
    visits = []

    def block(stage):
        if stage == blocked_stage and not entered.is_set():
            inside.set()
            entered.set()
            release.wait(0.3)
            inside.clear()

    class Input:
        def read(self, *_args, **_kwargs):
            block('read')
            return bytes(client.CHUNK * 2)

    class Audio:
        def open(self, **_kwargs):
            return Input()

        def get_default_input_device_info(self):
            return {'index': 0}

    class Detector:
        def is_speech(self, *_args):
            block('vad')
            return False

    class Socket:
        async def send(self, _packet):
            pytest.fail('Warmup/silent fixture must not submit capture')

    monkeypatch.setattr(client.pyaudio, 'PyAudio', Audio)
    monkeypatch.setattr(client, 'SileroSpeechDetector', Detector)

    async def scenario():
        device = client.Device()
        device.websocket = Socket()
        loop = asyncio.get_running_loop()

        # A separate thread gives the old blocking path a bounded escape.
        # The event-loop marker must run while capture is still waiting.
        def schedule_marker():
            entered.wait(1)
            loop.call_soon_threadsafe(lambda: visits.append(inside.is_set()))
            release.wait(0.15)
            release.set()

        watchdog = threading.Thread(target=schedule_marker)
        watchdog.start()
        task = asyncio.create_task(device.send_audio())
        try:
            while not visits:
                await asyncio.sleep(0.001)
            assert visits == [True], f'{blocked_stage} blocked playback/control delivery until capture finished'
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            watchdog.join(1)

    asyncio.run(scenario())


@pytest.mark.parametrize('cancellations', [1, 2])
def test_cancelling_capture_waits_for_the_owned_native_operation(cancellations):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def read():
        calls.append('read')
        entered.set()
        release.wait(1)
        finished.set()

    async def scenario():
        task = asyncio.create_task(client._capture_work(read))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        try:
            task.cancel()
            await asyncio.sleep(0.01)
            if cancellations == 2:
                task.cancel()
                await asyncio.sleep(0.01)
            assert not task.done(), 'Cleanup may not close a stream while its read is running'
            assert not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and calls == ['read']

    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['read', 'vad'])
def test_pause_receipt_during_capture_discards_the_old_gate_chunk(monkeypatch, stage):
    entered, release = threading.Event(), threading.Event()
    packets = []
    reads = []

    def delayed(which):
        if which == stage and not entered.is_set():
            entered.set()
            release.wait(1)

    class Input:
        def read(self, *_args, **_kwargs):
            reads.append(1)
            if len(reads) > 1:
                raise asyncio.CancelledError()
            delayed('read')
            return b'\x01\x01' * client.CHUNK

    class Audio:
        def open(self, **_kwargs):
            return Input()

        def get_default_input_device_info(self):
            return {'index': 0}

    class Detector:
        def is_speech(self, *_args):
            delayed('vad')
            return True

    class Socket:
        async def send(self, value):
            packets.append(value)

    monkeypatch.setattr(client.pyaudio, 'PyAudio', Audio)
    monkeypatch.setattr(client, 'SileroSpeechDetector', Detector)

    async def scenario():
        device = client.Device()
        device.audio_session = 'fixture'
        device.voice_revision = 0
        device.audio_v2 = True
        device.websocket = Socket()
        task = asyncio.create_task(device.send_audio())
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            await device._ack_voice({'audio_session': 'fixture', 'voice_revision': 1, 'voice_paused': True})
            assert device.voice_paused_by_user
            assert len(packets) == 1 and 'voice_input_ack' in packets[0]
            release.set()
            await task
        except asyncio.CancelledError:
            pass
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(packets) == 1, 'Old-gate audio must never be sent after pause acknowledgement'

    asyncio.run(scenario())
