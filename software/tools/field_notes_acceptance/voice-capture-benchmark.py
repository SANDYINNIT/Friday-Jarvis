"""Compare scheduling only, with fake 64ms reads through real send_audio."""
import asyncio
import importlib
import json
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
client = importlib.import_module('source.clients.light-python.client')
actual_work = client._capture_work


async def blocking_work(operation, *args, **kwargs):
    # Historical execution behavior, reproduced in memory only.
    return operation(*args, **kwargs)


async def measure(work):
    loop = asyncio.get_running_loop()
    delays = []

    class Input:
        count = 0

        def read(self, *_args, **_kwargs):
            self.count += 1
            if self.count > 8:
                raise asyncio.CancelledError()
            stamp = time.perf_counter()
            loop.call_soon_threadsafe(lambda: delays.append(time.perf_counter() - stamp))
            time.sleep(client.CHUNK / client.RECORDING_RATE)
            return bytes(client.CHUNK * 2)

    class Audio:
        def open(self, **_kwargs):
            return Input()

        def get_default_input_device_info(self):
            return {'index': -1}

    class Detector:
        def is_speech(self, *_args):
            return False

    class Socket:
        async def send(self, _value):
            raise AssertionError('Synthetic silence must not submit audio')

    with patch.object(client.pyaudio, 'PyAudio', Audio), patch.object(client, 'SileroSpeechDetector', Detector), patch.object(client, '_capture_work', work):
        device = client.Device()
        device.websocket = Socket()
        try:
            await device.send_audio()
        except asyncio.CancelledError:
            pass
    await asyncio.sleep(0)
    return {'samples': len(delays), 'median_control_delivery_ms': round(statistics.median(delays) * 1000, 3), 'max_control_delivery_ms': round(max(delays) * 1000, 3)}


async def main():
    return {'scope': 'Fake 64ms capture read; actual send_audio; control-loop scheduling only, no devices/network', 'before_blocking_behavior': await measure(blocking_work), 'after_shipped_capture_work': await measure(actual_work)}


result = asyncio.run(main())
Path(__file__).with_name('voice-capture-benchmark.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
