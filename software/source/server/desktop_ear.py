"""Always-listening desktop ear.

Records a rolling ~2 minute loop of Windows system audio (games, video,
music, calls) using WASAPI loopback via pyaudio, then transcribes the last
N seconds on demand with a local faster-whisper model so FRIDAY can answer
"Did you catch that, Friday?".

Failures are silent and never block the voice pipeline.
"""

from __future__ import annotations

import os
import re
import threading
import time

CHUNK_SAMPLES = 1024
SAMPLE_RATE = 16000
BUFFER_LIMIT = 2048  # ~2.1 minutes at 16 kHz mono int16


def _env_flag(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip() not in ("0", "false", "off", "no")


class DesktopEar:
    """Background WASAPI loopback recorder with a fixed rolling buffer."""

    def __init__(self):
        self._chunks = None
        self._stream = None
        self._pyaudio = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.active = False
        self.error = ""

    def _find_loopback_device(self, pyaudio):
        count = pyaudio.get_device_count()
        for index in range(count):
            try:
                info = pyaudio.get_device_info_by_index(index)
                name = str(info.get("name") or "").lower()
                host_api = pyaudio.get_host_api_info(info.get("hostApi") or 0)
                host_name = str(host_api.get("name") or "").lower()
                if "loopback" in name and "wasapi" in host_name and info.get("maxInputChannels", 0) > 0:
                    return index
            except Exception:
                continue
        return None

    def _open_stream(self):
        import pyaudio

        self._pyaudio = pyaudio.PyAudio()
        device_index = self._find_loopback_device(self._pyaudio)
        if device_index is None:
            self.error = "No WASAPI loopback capture device available."
            return None
        self._chunks = []
        stream = self._pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=CHUNK_SAMPLES,
        )
        return stream

    def _write(self, data):
        if data is None:
            return
        with self._lock:
            self._chunks.append(bytes(data))
            if len(self._chunks) > BUFFER_LIMIT:
                self._chunks = self._chunks[-BUFFER_LIMIT:]

    def _run(self):
        try:
            stream = self._open_stream()
            if stream is None:
                return
            self._stream = stream
            self.active = True
            while not self._stop.is_set():
                try:
                    count = self._stream.get_read_available()
                    if count > 0:
                        data = self._stream.read(min(count, CHUNK_SAMPLES * 8), exception_on_overflow=False)
                        self._write(data)
                except Exception:
                    time.sleep(0.05)
        except Exception as error:
            self.error = str(error)
            self.active = False
        finally:
            self.active = False
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception:
                    pass
            if self._pyaudio is not None:
                try:
                    self._pyaudio.terminate()
                except Exception:
                    pass

    def start(self):
        if not _env_flag("FRIDAY_DESKTOP_EAR"):
            self.active = False
            return self
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="friday-desktop-ear", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def capture_recent(self, seconds=30.0):
        """Return the last ``seconds`` of PCM int16 mono 16 kHz, best-effort."""
        seconds = max(1.0, min(float(seconds), 120.0))
        max_chunks = max(1, int(SAMPLE_RATE * seconds / CHUNK_SAMPLES))
        with self._lock:
            chunks = self._chunks
            if not chunks:
                return b""
            chunks = list(chunks[-max_chunks:])
        return b"".join(chunks)


_transcriber_lock = threading.Lock()
_transcriber = None


def _get_transcriber():
    """Lazily build a RealtimeSTT recorder whose faster-whisper runs in its own
    process. faster-whisper loads torch/libiomp5md, which conflicts with the
    OpenMP runtime the server already loads via RealtimeTTS, so transcription
    must never happen in the server process (OMP Error #15)."""
    global _transcriber
    if _transcriber is None:
        from RealtimeSTT import AudioToTextRecorder

        _transcriber = AudioToTextRecorder(
            model=os.environ.get("FRIDAY_EAR_WHISPER_MODEL", "base"),
            compute_type="int8",
            spinner=False,
            use_microphone=False,
            enable_realtime_transcription=False,
            handle_buffer_overflow=False,
            min_gap_between_recordings=0.0,
            min_length_of_recording=0.0,
            silero_use_onnx=True,
            beam_size=1,
        )
        _transcriber.stop()
    return _transcriber


def transcribe_pcm(pcm_bytes, model_size=None):
    """Transcribe raw int16 mono 16 kHz PCM bytes via faster-whisper."""
    if not pcm_bytes:
        return ""
    model_size = model_size or os.environ.get("FRIDAY_EAR_WHISPER_MODEL", "base")
    with _transcriber_lock:
        recorder = _get_transcriber()
        chunk_bytes = 2 * 1024
        recorder.start()
        for i in range(0, len(pcm_bytes), chunk_bytes):
            piece = pcm_bytes[i:i + chunk_bytes]
            if len(piece) < chunk_bytes:
                piece += b"\x00" * (chunk_bytes - len(piece))
            recorder.feed_audio(piece)
        recorder.stop()
        return recorder.text()


_CATCH_RE = re.compile(
    r"did you catch that|did you hear|catch that|what did you just hear|"
    r"what (?:was|is) (?:playing|on the screen|that)|what did i (?:just )?(?:hear|say|watch)|"
    r"(?:replay|rehear) (?:the )?last 30 seconds",
    re.IGNORECASE,
)


def handle_ear_command(text, ear):
    """Intercept 'Did you catch that?' and report the last 30s of desktop audio."""
    if not _CATCH_RE.search(text or ""):
        return None
    if ear is None or not getattr(ear, "active", False):
        return "My desktop ear is not capturing audio right now, Sir."
    pcm = ear.capture_recent(30.0)
    if not pcm:
        return "I didn't manage to capture clean audio in the last thirty seconds, Sir."
    try:
        transcription = transcribe_pcm(pcm)
    except Exception:
        transcription = ""
    if not transcription.strip():
        return "I heard nothing intelligible in the last thirty seconds, Sir."
    return f"In the last thirty seconds of your audio, I caught: \"{transcription.strip()}\""