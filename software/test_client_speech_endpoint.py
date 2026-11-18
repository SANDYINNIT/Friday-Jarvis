"""Exercise real client capture with deterministic PCM/VAD/socket boundaries."""
import asyncio
import importlib.util
import json
import random
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_path = Path(__file__).parent / "source" / "clients" / "light-python" / "client.py"
_spec = importlib.util.spec_from_file_location("speech_client", _path)
client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client)


def test_residual_frames_preserve_randomly_split_pcm():
    class Detector:
        def __init__(self):
            self.seen = []

        def is_speech(self, frame, rate):
            assert len(frame) == 1024 and rate == 16000
            self.seen.append(frame)
            return False

    detector = Detector()
    frames = client.SpeechFrames(detector)
    original = random.Random(71).randbytes(17003)
    rng = random.Random(21)
    offset = 0
    while offset < len(original):
        size = rng.randint(1, 2048)
        assert not frames.voiced(original[offset:offset + size])
        offset += size
    assert b"".join(detector.seen) + frames.pending == original
    assert len(frames.pending) < 1024


class Finished(BaseException):
    pass


def run_capture(chunks, *, paused=False, busy=False, speaking=False,
                after_read=None, cap=60.0, clock_jump_after=None):
    """No devices/network: invoke the real send_audio coroutine to exhaustion."""
    class Clock:
        now = 10.0

        def read(self):
            return self.now

    clock = Clock()
    packets = []
    vad_frames = []

    class Detector:
        def is_speech(self, frame, rate):
            vad_frames.append(frame)
            assert len(frame) == 1024 and rate == 16000
            return b"\x01\x01" in frame

    class Stream:
        def __init__(self):
            self.index = 0

        def read(self, count, exception_on_overflow):
            assert count == 1024 and exception_on_overflow is False
            if self.index >= len(chunks):
                raise Finished()
            chunk = chunks[self.index]
            self.index += 1
            clock.now += len(chunk) / 32000
            if self.index == clock_jump_after:
                clock.now += 60
            if after_read:
                after_read(self.index, device)
            return chunk

    class PyAudio:
        def open(self, **kwargs):
            assert kwargs == dict(format=client.FORMAT, channels=1, rate=16000,
                                  input=True, frames_per_buffer=1024)
            return Stream()

        def get_default_input_device_info(self):
            return {"index": 1, "name": "test microphone"}

    class Socket:
        async def send(self, packet):
            packets.append(packet)

    async def sleep(_):
        pass

    device = client.Device.__new__(client.Device)
    device.p = PyAudio()
    device.websocket = Socket()
    device.recording = False
    device.voice_paused_by_user = paused
    device.server_busy = busy
    device.response_speaking = speaking
    device.cooldown_start_time = 0
    device.last_action_time = 10
    device.debug = False
    device.trace_recording_start = None
    device.trace_first_audio = False
    device.input_epoch = 0
    device.audio_v2 = False
    with patch.object(client, "SileroSpeechDetector", return_value=Detector()) as factory, \
            patch.object(client.time, "time", clock.read), \
            patch.object(client.time, "monotonic", clock.read), \
            patch.object(client.asyncio, "sleep", sleep), \
            patch.object(client, "MAX_RECORDING_SEC", cap):
        with pytest.raises(Finished):
            asyncio.run(device.send_audio())
    factory.assert_called_once_with()
    return packets, vad_frames


QUIET = b"\x04\x04" * 1024  # RMS 1028: old >800 detector calls this speech.
VOICE = b"\x01\x01" * 1024
WARMUP = [QUIET] * 47  # 3.008 seconds of PCM processed, never dispatched.


def markers(packets):
    return [json.loads(p) for p in packets if isinstance(p, str)]


def test_loud_non_speech_does_not_activate_and_warmup_is_processed(capsys):
    packets, processed = run_capture(WARMUP + [QUIET] * 60)
    assert packets == []
    assert len(processed) == (107 * 2048) // 1024
    assert "ready" in capsys.readouterr().out


def test_warmup_speech_never_dispatches():
    packets, _ = run_capture([VOICE] * 47 + [QUIET] * 40)
    assert packets == []


def test_two_chunk_onset_preroll_exact_bytes_and_noise_endpoint():
    # Ten quiet chunks fill bounded preroll; triggering chunk occurs only once.
    tail = [QUIET] * 10 + [VOICE] * 4 + [QUIET] * 30
    packets, _ = run_capture(WARMUP + tail)
    control = markers(packets)
    assert len(control) == 2 and control[0]["start"] and control[1]["end"]
    assert "capture_incomplete" not in control[1]
    first_audio = b"".join(p for p in packets if isinstance(p, bytes))
    # Onset confirmed at tail[11], hence exact final 0.5 s ending there.
    preroll = b"".join(tail[:12])[-16000:]
    assert first_audio.startswith(preroll)
    remaining = first_audio[len(preroll):]
    assert remaining.startswith(b"".join(tail[12:14]))
    # Residual frame overlap can extend speech by at most one read.
    trailing = remaining[4096:]
    assert trailing == QUIET * (len(trailing) // 2048)
    assert 1.5 <= len(trailing) / 32000 <= 1.5 + 2 * 0.064


def test_single_voiced_chunk_does_not_activate():
    packets, _ = run_capture(WARMUP + [QUIET] * 10 + [VOICE] + [QUIET] * 30)
    assert markers(packets) == []


@pytest.mark.parametrize("paused,busy,speaking", [(True, False, False), (False, True, False)])
def test_pause_and_busy_block_capture(paused, busy, speaking):
    packets, _ = run_capture(WARMUP + [VOICE] * 10,
                             paused=paused, busy=busy, speaking=speaking)
    assert packets == []


def test_busy_tts_allows_existing_barge_in():
    packets, _ = run_capture(WARMUP + [VOICE] * 4 + [QUIET] * 30,
                             busy=True, speaking=True)
    assert markers(packets)[0]["start"]


def test_blocked_audio_cannot_leak_into_preroll():
    def release(index, device):
        if index == 53:
            device.voice_paused_by_user = False
    packets, _ = run_capture(WARMUP + [QUIET] * 5 + [VOICE] * 4 + [QUIET] * 30,
                             paused=True, after_read=release)
    audio = [p for p in packets if isinstance(p, bytes)]
    assert audio and b"".join(audio).startswith(VOICE * 2)


def test_independent_duration_cap_marks_incomplete_on_continuous_speech():
    packets, _ = run_capture(WARMUP + [VOICE] * 1000)
    control = markers(packets)
    assert len(control) == 2
    assert control[1]["end"] and control[1]["capture_incomplete"] is True
    seconds = sum(len(p) for p in packets if isinstance(p, bytes)) / 32000
    assert 60 <= seconds <= 60.064


def test_cooldown_clears_stale_onset_and_preroll():
    def cooldown(index, device):
        if index == 48:
            device.cooldown_start_time = 1000
    packets, _ = run_capture(WARMUP + [VOICE] * 10, after_read=cooldown)
    assert packets == []


def test_elapsed_duration_cap_does_not_depend_on_pcm_count():
    packets, _ = run_capture(WARMUP + [VOICE] * 10, clock_jump_after=50)
    assert markers(packets)[-1]["capture_incomplete"] is True
    assert sum(len(p) for p in packets if isinstance(p, bytes)) < 32000


def test_incomplete_server_reply_reports_no_execution_and_preserves_busy(capsys):
    class Socket:
        called = False

        async def recv(self):
            if self.called:
                raise Finished()
            self.called = True
            return json.dumps({"ignored": True, "end": True,
                               "capture_incomplete": True})

    class PyAudio:
        def open(self, **kwargs):
            assert kwargs["output"] is True
            return object()

    device = client.Device.__new__(client.Device)
    device.p = PyAudio()
    device.websocket = Socket()
    device.debug = False
    device.audio_v2 = False
    device.was_busy_on_record = True
    device.server_busy = False
    device.cooldown_start_time = 0
    with patch.object(client.time, "time", return_value=123):
        with pytest.raises(Finished):
            asyncio.run(device.receive_audio())
    assert device.server_busy is True and device.cooldown_start_time == 123
    assert "Command was not executed" in capsys.readouterr().out


class ModelSession:
    def __init__(self, probability=0.41):
        self.calls = []
        self.probability = probability

    def get_inputs(self):
        return [SimpleNamespace(name="input", type="tensor(float)", shape=[None, None]),
                SimpleNamespace(name="state", type="tensor(float)", shape=[2, None, 128]),
                SimpleNamespace(name="sr", type="tensor(int64)", shape=[])]

    def get_outputs(self):
        return [SimpleNamespace(name="output", type="tensor(float)", shape=[None, 1]),
                SimpleNamespace(name="stateN", type="tensor(float)", shape=[None, None, None])]

    def run(self, names, inputs):
        import numpy as np
        assert names == ["output", "stateN"]
        self.calls.append({key: value.copy() for key, value in inputs.items()})
        return np.array([[self.probability]], dtype=np.float32), inputs["state"] + 1


def test_silero_normalization_context_and_state_are_per_stream():
    import numpy as np
    session = ModelSession()
    detector = client.SileroSpeechDetector(session)
    samples = [-32768, 32767] + [1000] * 510
    assert detector.is_speech(struct.pack("<512h", *samples), 16000)
    first = session.calls[0]
    assert first["input"].shape == (1, 576)
    assert first["input"].dtype == np.float32
    assert first["input"][0, 64] == -1.0
    assert first["input"][0, 65] == 32767 / 32768
    assert not first["input"][0, :64].any() and not first["state"].any()
    assert first["sr"].shape == () and first["sr"].dtype == np.int64
    detector.is_speech(b"\x00\x00" * 512, 16000)
    second = session.calls[1]
    assert (second["state"] == 1).all()
    assert (second["input"][0, :64] == 1000 / 32768).all()
    other = client.SileroSpeechDetector(ModelSession())
    assert not other.state.any() and not other.context.any()


@pytest.mark.parametrize("probability,expected", [(0.39, False), (0.41, True)])
def test_silero_threshold_matches_live_acceptance(probability, expected):
    assert client.SileroSpeechDetector(ModelSession(probability)).is_speech(b"\x00" * 1024, 16000) is expected


@pytest.mark.parametrize("length,rate", [(640, 16000), (2048, 16000), (1024, 48000)])
def test_silero_rejects_invalid_frame_without_running_model(length, rate):
    session = ModelSession()
    with pytest.raises(ValueError):
        client.SileroSpeechDetector(session).is_speech(b"\x00" * length, rate)
    assert session.calls == []


def test_silero_rejects_incompatible_model_contract():
    session = ModelSession()
    session.get_inputs = lambda: []
    with pytest.raises(RuntimeError, match="incompatible"):
        client.SileroSpeechDetector(session)


def test_model_path_uses_offline_cache_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("FRIDAY_VAD_MODEL_PATH", raising=False)
    monkeypatch.delenv("TORCH_HOME", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    suffix = Path("hub/snakers4_silero-vad_master/src/silero_vad/data/silero_vad.onnx")
    assert client.vad_model_path() == tmp_path / "torch" / suffix
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "custom_torch"))
    assert client.vad_model_path() == tmp_path / "custom_torch" / suffix
    monkeypatch.setenv("FRIDAY_VAD_MODEL_PATH", str(tmp_path / "explicit.onnx"))
    assert client.vad_model_path() == tmp_path / "explicit.onnx"


def test_missing_offline_model_fails_before_opening_microphone(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FRIDAY_VAD_MODEL_PATH", str(tmp_path / "absent.onnx"))
    device = client.Device.__new__(client.Device)
    class NoMicrophone:
        def open(self, **kwargs):
            pytest.fail("must not open microphone without validated offline VAD")
    device.p = NoMicrophone()
    with pytest.raises(RuntimeError, match="no download was attempted"):
        asyncio.run(device.send_audio())
    assert "ready" not in capsys.readouterr().out


def test_silero_cpu_session_uses_one_thread_and_offline_path(monkeypatch, tmp_path):
    import onnxruntime as ort
    model = tmp_path / "existing.onnx"
    model.write_bytes(b"test fixture")
    monkeypatch.setenv("FRIDAY_VAD_MODEL_PATH", str(model))
    with patch.object(ort, "InferenceSession", return_value=ModelSession()) as factory:
        client.SileroSpeechDetector()
    args, kwargs = factory.call_args
    assert args == (str(model),)
    assert kwargs["providers"] == ["CPUExecutionProvider"]
    assert kwargs["sess_options"].inter_op_num_threads == 1
    assert kwargs["sess_options"].intra_op_num_threads == 1
