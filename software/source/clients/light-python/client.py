import asyncio
import base64
import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake
import pyaudio
import json
from collections import deque
import logging
import os
from pathlib import Path
import time
from source.server.audio_startup import enabled as audio_startup_enabled

CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RECORDING_RATE = 16000
PLAYBACK_RATE = 24000

SILENCE_LIMIT_SEC = 1.5
VAD_FRAME_BYTES = 512 * 2
VAD_WARMUP_SEC = 3.0
PREROLL_BYTES = int(RECORDING_RATE * 2 * 0.5)
MAX_RECORDING_SEC = 60.0
_log = logging.getLogger("friday")
PROTOCOL = "friday.audio.v2"
MAX_OUTPUT_BUFFER = 30 * PLAYBACK_RATE * 2


async def _capture_work(operation, *args, **kwargs):
    """Run one ordered input operation without delaying output/control IO.

    Cancelling the await does not cancel a native read. Wait for that one
    operation before the caller closes the stream or starts a new session.
    Never launch a replacement read while an earlier read is still running.
    """
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Repeated shutdown requests must not cancel the wrapper and allow a
        # still-running native operation to race stream cleanup either.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            worker.result()
        except BaseException:
            pass
        raise


class DevicePlayback:
    """One PortAudio stream. Only its own callback can consume its buffers."""
    def __init__(self, playback_id):
        self.id = playback_id
        self.pending = deque()
        self.tail = b""
        self.received = self.consumed = 0
        self.reported = 0
        self.sealed = self.completed = self.cancelled = False
        self.stream = None
        self.received_at = time.monotonic()
        self.first_callback_at = None
        self.starved_callbacks = self.underflows = self.silence_frames = 0
        self.peak_bytes = 0

    def callback(self, _input, frames, _time_info, _status):
        size = frames * 2
        if self.cancelled:
            return bytes(size), pyaudio.paAbort
        parts = []
        left = size
        while left:
            if not self.tail:
                try:
                    self.tail = self.pending.popleft()
                except IndexError:
                    break
            chunk, self.tail = self.tail[:left], self.tail[left:]
            parts.append(chunk)
            left -= len(chunk)
        self.consumed += size - left
        if size != left and self.first_callback_at is None:
            self.first_callback_at = time.monotonic()
        if _status and int(_status) & pyaudio.paOutputUnderflow:
            self.underflows += 1
        complete = self.sealed and self.received == self.consumed
        if left and not complete:
            self.starved_callbacks += 1
            self.silence_frames += left // 2
        if complete:
            self.completed = True
        return b"".join(parts) + bytes(left), pyaudio.paComplete if complete else pyaudio.paContinue

    def append(self, pcm):
        if (self.sealed or not pcm or len(pcm) % 2
                or self.received - self.consumed + len(pcm) > MAX_OUTPUT_BUFFER):
            raise ValueError("Invalid or overflowing playback buffer")
        self.received += len(pcm)
        self.pending.append(pcm)
        self.peak_bytes = max(self.peak_bytes, self.received - self.consumed)

    def metrics(self):
        return {"first_callback_ms": round((self.first_callback_at-self.received_at)*1000, 3) if self.first_callback_at is not None else None,
                "starved_callbacks": self.starved_callbacks, "device_underflows": self.underflows,
                "inserted_silence_frames": self.silence_frames,
                "peak_buffer_ms": round(self.peak_bytes / (PLAYBACK_RATE * 2) * 1000, 3)}

    def close(self):
        self.cancelled = True
        stream, self.stream = self.stream, None
        if stream is not None:
            # Pa_CloseStream aborts an active stream instead of draining it.
            stream.close()


def vad_model_path():
    override = os.environ.get("FRIDAY_VAD_MODEL_PATH")
    if override:
        return Path(override).expanduser()
    torch_home = os.environ.get("TORCH_HOME")
    if torch_home:
        cache = Path(torch_home).expanduser()
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache").expanduser() / "torch"
    return cache / "hub" / "snakers4_silero-vad_master" / "src" / "silero_vad" / "data" / "silero_vad.onnx"


class SileroSpeechDetector:
    """Independent offline CPU detector; never shares server recurrent state."""

    def __init__(self, session=None):
        import numpy as np

        self.np = np
        if session is None:
            import onnxruntime as ort

            model = vad_model_path()
            if not model.is_file():
                raise RuntimeError(f"Microphone VAD model unavailable: {model}. FRIDAY_VAD_MODEL_PATH must point to a cached Silero ONNX model; no download was attempted.")
            options = ort.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"], sess_options=options)
        inputs = {entry.name: entry for entry in session.get_inputs()}
        outputs = {entry.name: entry for entry in session.get_outputs()}
        compatible = (
            set(inputs) == {"input", "state", "sr"}
            and inputs["input"].type == "tensor(float)" and len(inputs["input"].shape) == 2
            and inputs["state"].type == "tensor(float)" and inputs["state"].shape == [2, None, 128]
            and inputs["sr"].type == "tensor(int64)" and inputs["sr"].shape == []
            and set(outputs) == {"output", "stateN"}
            and outputs["output"].type == "tensor(float)" and len(outputs["output"].shape) == 2
            and outputs["stateN"].type == "tensor(float)" and len(outputs["stateN"].shape) == 3
        )
        if not compatible:
            raise RuntimeError("Microphone VAD cached model has an incompatible Silero ONNX contract; no fallback or download was attempted.")
        self.session = session
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def is_speech(self, frame, sample_rate):
        if sample_rate != 16000 or len(frame) != 1024:
            raise ValueError("Silero microphone VAD requires 512 mono int16 samples at 16000 Hz")
        np = self.np
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32).reshape(1, 512) / 32768.0
        audio = np.concatenate((self.context, samples), axis=1)
        probability, state = self.session.run(["output", "stateN"], {
            "input": audio, "state": self.state, "sr": np.array(sample_rate, dtype=np.int64),
        })
        if probability.shape != (1, 1) or state.shape != (2, 1, 128) or not np.isfinite(probability).all() or not np.isfinite(state).all():
            raise RuntimeError("Microphone VAD returned invalid probability or recurrent state")
        self.state = state
        self.context = samples[:, -64:].copy()
        return float(probability[0, 0]) > 0.4


class SpeechFrames:
    """Keep PCM residuals between reads; Silero requires exact 32 ms frames."""

    def __init__(self, detector):
        self.detector = detector
        self.pending = b""

    def voiced(self, data):
        self.pending += data
        speech = False
        while len(self.pending) >= VAD_FRAME_BYTES:
            frame, self.pending = self.pending[:VAD_FRAME_BYTES], self.pending[VAD_FRAME_BYTES:]
            speech = self.detector.is_speech(frame, RECORDING_RATE) or speech
        return speech

def can_listen_now(voice_paused, server_busy, response_speaking, current_time, cooldown_start):
    return (
        not voice_paused
        and (not server_busy or response_speaking)
        and current_time - cooldown_start > 1.2
    )


class Device:
    def __init__(self):
        self.server_url = "0.0.0.0:10001"
        self.startup_check = audio_startup_enabled()
        self.p = None if self.startup_check else pyaudio.PyAudio()
        self.websocket = None
        self.recording = False
        self.input_stream = None
        self.output_stream = None
        self.play_audio = not self.startup_check

        self.server_busy = False
        self.was_busy_on_record = False
        self.voice_paused_by_user = False
        self.cooldown_start_time = 0.0
        self.last_action_time = 0.0
        self.response_speaking = False
        self.hot_window_active = False
        self.debug = False
        self.trace_start = time.monotonic()
        self.trace_recording_start = None
        self.trace_first_audio = False

        # ROUND ZB-C: playback acknowledgement + caption state.
        # The server runs MUTED, so nothing it measures is audible. Only this
        # client knows when PCM actually reached the sound device - these fields
        # are the source of `client_playback_observable` on the server.
        self.speech_utterance = ""
        self.caption = ""
        self.stale_audio = False      # set by speech_stop until the next frame
        self.dropped_bytes = 0
        self.pb_bytes = 0             # bytes written for the current utterance
        self.pb_first_sent = False
        self.pb_last_sent = 0.0
        self.audio_session = None
        self.voice_revision = -1
        self.input_epoch = 0
        self.playback = None
        self.retired_playbacks = deque(maxlen=32)
        self.audio_v2 = False

    def trace(self, event):
        if self.debug:
            elapsed = time.monotonic() - self.trace_start
            print(f"[trace +{elapsed:.3f}s] client {event}", flush=True)

    async def connect_with_retry(self, max_retries=50, retry_delay=2):
        for attempt in range(max_retries):
            try:
                ws_url = "ws://" + str(self.server_url)
                self.websocket = await websockets.connect(
                    ws_url,
                    open_timeout=120,
                    ping_interval=None,
                    subprotocols=[PROTOCOL],
                )
                return
            except (OSError, InvalidHandshake):
                if attempt % 8 == 0 and attempt != 0:
                    print("Loading...")
                await asyncio.sleep(retry_delay)
        raise Exception("Failed to connect to the server after multiple attempts")

    async def send_audio(self):
        # Fail before opening the microphone when the offline model is absent
        # or incompatible; never announce readiness or fall back silently.
        frames = SpeechFrames(await _capture_work(SileroSpeechDetector))
        self.input_stream = self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RECORDING_RATE,
            input=True,
            frames_per_buffer=CHUNK
        )

        device = self.p.get_default_input_device_info()
        _log.info("microphone selected index=%s name=%s rate=%s vad=silero_onnx_cpu warmup_seconds=%.1f",
                  device.get("index"), device.get("name"), RECORDING_RATE, VAD_WARMUP_SEC)
        preroll = deque()
        preroll_size = 0
        warmup_bytes = 0
        ready = False
        onset_chunks = 0
        silence_seconds = 0.0
        captured_bytes = 0
        epoch = self.input_epoch

        while True:
            try:
                data = await _capture_work(self.input_stream.read, CHUNK, exception_on_overflow=False)
                if epoch != self.input_epoch:
                    epoch = self.input_epoch
                    preroll.clear()
                    preroll_size = onset_chunks = 0
                    frames.pending = b""
                    continue
                voiced = await _capture_work(frames.voiced, data)
                if epoch != self.input_epoch:
                    # Pause/resume or reconnect can be acknowledged while VAD
                    # runs. Do not submit that older software-gate chunk.
                    epoch = self.input_epoch
                    preroll.clear()
                    preroll_size = onset_chunks = 0
                    frames.pending = b""
                    continue
                chunk_seconds = len(data) / (RECORDING_RATE * 2)
                if not ready:
                    warmup_bytes += len(data)
                    if warmup_bytes < VAD_WARMUP_SEC * RECORDING_RATE * 2:
                        await asyncio.sleep(0.001)
                        continue
                    ready = True
                    _log.info("microphone ready vad=silero_onnx_cpu warmup_complete=True")
                    print("Hands-free voice input ready. Say Friday and your command.")
                    # Warmup audio is never replayed as a command.
                    continue

                current_time = time.time()
                can_listen = can_listen_now(
                    self.voice_paused_by_user, self.server_busy,
                    self.response_speaking, current_time, self.cooldown_start_time,
                )
                started = False
                if not self.recording:
                    if can_listen:
                        preroll.append(data)
                        preroll_size += len(data)
                        while preroll_size > PREROLL_BYTES:
                            excess = preroll_size - PREROLL_BYTES
                            first = preroll.popleft()
                            if len(first) > excess:
                                preroll.appendleft(first[excess:])
                                preroll_size -= excess
                            else:
                                preroll_size -= len(first)
                        onset_chunks = onset_chunks + 1 if voiced else 0
                        if onset_chunks >= 2:
                            self.recording = True
                            started = True
                            self.was_busy_on_record = self.server_busy
                            self.trace_first_audio = False
                            self.trace_recording_start = time.monotonic()
                            silence_seconds = 0.0
                            captured_bytes = preroll_size
                            self.trace("recording_start")
                            _log.info("microphone recording_start preroll_seconds=%.3f", preroll_size / (RECORDING_RATE * 2))
                            print("[Listening...]")
                            await self.websocket.send(json.dumps({
                                "role": "user", "type": "audio", "format": "bytes.wav", "start": True,
                                **({"voice_revision": self.voice_revision} if self.audio_v2 else {}),
                            }))
                            for buffered in list(preroll):
                                if epoch != self.input_epoch:
                                    break
                                await self.websocket.send(buffered)
                            preroll.clear()
                            preroll_size = 0
                    else:
                        onset_chunks = 0
                        preroll.clear()
                        preroll_size = 0

                if self.recording:
                    if not started:
                        await self.websocket.send(data)
                        captured_bytes += len(data)
                    if epoch != self.input_epoch:
                        continue
                    silence_seconds = 0.0 if voiced and can_listen else silence_seconds + chunk_seconds
                    duration = time.monotonic() - self.trace_recording_start
                    capped = duration >= MAX_RECORDING_SEC or captured_bytes >= MAX_RECORDING_SEC * RECORDING_RATE * 2
                    if capped or silence_seconds >= SILENCE_LIMIT_SEC:
                        self.recording = False
                        reason = "duration_cap" if capped else "speech_silence"
                        self.trace(f"recording_end duration={duration:.3f}s reason={reason}")
                        _log.info("microphone recording_end duration=%.3f audio_seconds=%.3f reason=%s",
                                  duration, captured_bytes / (RECORDING_RATE * 2), reason)
                        self.trace_recording_start = None
                        onset_chunks = 0
                        print("[Thinking...]")
                        end = {"role": "user", "type": "audio", "format": "bytes.wav", "end": True}
                        if self.audio_v2:
                            end["voice_revision"] = self.voice_revision
                        if capped:
                            end["capture_incomplete"] = True
                        await self.websocket.send(json.dumps(end))
                        if epoch != self.input_epoch:
                            continue
                        self.server_busy = True
                        self.last_action_time = time.time()

                if self.server_busy and (not self.voice_paused_by_user) and (time.time() - self.last_action_time > 45.0):
                    self.server_busy = False
                    self.cooldown_start_time = time.time()

            except Exception as e:
                if isinstance(e, ConnectionClosed):
                    raise
                print("Error in send_audio: " + str(e))

            await asyncio.sleep(0.001)

    async def _send_playback(self, event, **extra):
        """Best-effort acknowledgement. Never raises, never blocks for long."""
        if self.websocket is None:
            return
        payload = {
            "role": "client", "type": "playback",
            "event": event, "utterance": self.speech_utterance,
        }
        if event == "first_audio" and self.caption:
            payload["caption"] = self.caption
        payload.update(extra)
        try:
            await self.websocket.send(json.dumps(payload))
        except Exception:
            pass

    def _flush_playback(self):
        """Drop audio already sitting in the output device.

        After a stop, anything still buffered there is STALE output - flushing
        is what makes "stop" also mean "stop what you already handed the card".
        """
        try:
            if self.output_stream is not None:
                stream, self.output_stream = self.output_stream, None
                stream.close()
        except Exception:
            pass

    async def receive_audio(self):
        if self.audio_v2:
            return await self.receive_audio_v2()
        while True:
            try:
                data = await self.websocket.recv()

                if isinstance(data, bytes):
                    if self.stale_audio:
                        # A stop happened: these bytes are stale output. Drop
                        # them instead of letting an old phrase play late.
                        self.dropped_bytes += len(data)
                        self.pb_bytes += len(data)
                        now = time.monotonic()
                        if now - self.pb_last_sent >= 0.5:
                            self.pb_last_sent = now
                            await self._send_playback("stale_dropped",
                                                      bytes=self.pb_bytes)
                        continue
                    self.response_speaking = True
                    if self.play_audio and not self.recording:
                        if self.output_stream is None:
                            self.output_stream = self.p.open(format=FORMAT, channels=CHANNELS,
                                rate=PLAYBACK_RATE, output=True, frames_per_buffer=CHUNK)
                        if not self.trace_first_audio:
                            self.trace_first_audio = True
                            self.trace("first_audio")
                        self.output_stream.write(data)
                        self.last_action_time = time.time()
                        # Acknowledge what actually reached the device.
                        self.pb_bytes += len(data)
                        now = time.monotonic()
                        if not self.pb_first_sent:
                            self.pb_first_sent = True
                            await self._send_playback("first_audio",
                                                      bytes=self.pb_bytes)
                        elif now - self.pb_last_sent >= 0.4:
                            self.pb_last_sent = now
                            await self._send_playback("progress",
                                                      bytes=self.pb_bytes)

                elif isinstance(data, str):
                    try:
                        msg = json.loads(data)
                        if isinstance(msg, dict):
                            if msg.get("type") == "speech_frame":
                                # ROUND ZB-C: the server labels each phrase, so
                                # the client can caption it and ack THAT phrase.
                                self.speech_utterance = str(msg.get("utterance") or "")
                                self.caption = str(msg.get("text") or "")
                                self.stale_audio = False
                                self.pb_bytes = 0
                                self.pb_first_sent = False
                                self.pb_last_sent = 0.0
                                self.trace("speech_frame seq=%s" % msg.get("seq"))
                            elif msg.get("speech_stop") is True:
                                # ROUND ZB-C: one stop ends voice + captions + any
                                # bounce. Discard what is queued, not just what
                                # has not arrived yet.
                                dropped = self.pb_bytes
                                self.speech_utterance = str(msg.get("utterance") or "")
                                self.stale_audio = True
                                self.caption = ""
                                self.response_speaking = False
                                self._flush_playback()
                                self.pb_bytes = 0
                                self.pb_first_sent = False
                                self.trace("speech_stop stale audio dropped")
                                if dropped:
                                    await self._send_playback("stale_dropped",
                                                              bytes=dropped)
                            elif msg.get("hot_window") is True:
                                self.hot_window_active = bool(msg.get("active"))
                                self.trace("hot_window_open")
                            elif msg.get("voice_paused") is True:
                                self.voice_paused_by_user = True
                                self.server_busy = True
                                self.hot_window_active = False
                                print("\n[Voice listening suspended. Press ENTER in the server terminal to resume.]")
                            elif msg.get("voice_paused") is False:
                                self.voice_paused_by_user = False
                                self.server_busy = False
                                self.response_speaking = False
                                self.hot_window_active = False
                                self.cooldown_start_time = time.time()
                                print("[Ready!]")
                            elif msg.get("ignored") is True:
                                self.trace("ready ignored")
                                self.server_busy = self.was_busy_on_record
                                self.cooldown_start_time = time.time()
                                if msg.get("capture_incomplete") is True:
                                    print("[Recording duration limit reached. Command was not executed; pause, then repeat.]")
                                elif self.server_busy:
                                    print("[Ignored background noise. Task continues...]")
                                else:
                                    print("[Ignored. Standing by...]")
                            elif msg.get("end") is True:
                                self.trace("ready response_end")
                                self.server_busy = False
                                self.response_speaking = False
                                self.hot_window_active = False
                                self.cooldown_start_time = time.time()
                                print("[Ready!]")
                    except json.JSONDecodeError:
                        pass

            except ConnectionClosed:
                return

    def apply_voice_input(self, paused):
        self.input_epoch += 1
        self.recording = False
        self.trace_recording_start = None
        self.hot_window_active = False
        self.voice_paused_by_user = paused
        self.cooldown_start_time = time.time()

    async def _ack_voice(self, msg):
        session, revision, paused = msg.get("audio_session"), msg.get("voice_revision"), msg.get("voice_paused")
        if (session != self.audio_session or type(revision) is not int
                or revision <= self.voice_revision or type(paused) is not bool):
            return
        self.voice_revision = revision
        applied = False
        try:
            self.apply_voice_input(paused)
            applied = True
        except Exception:
            self.voice_paused_by_user = True
        await self.websocket.send(json.dumps({"type": "voice_input_ack", "audio_session": session,
            "voice_revision": revision, "voice_paused": paused, "applied": applied}))

    async def _report_output(self, event, playback_id, count, metrics=None):
        await self.websocket.send(json.dumps({"type": "playback", "event": event,
            "audio_session": self.audio_session, "playback": playback_id, "bytes": count,
            "metrics": metrics if metrics is not None else self.playback.metrics() if self.playback else {}}))

    async def _end_output(self, event, playback_id):
        playback = self.playback
        if playback is not None and playback.id != playback_id:
            return
        if playback_id in self.retired_playbacks:
            return
        self.retired_playbacks.append(playback_id)
        self.playback = None
        self.response_speaking = False
        self.cooldown_start_time = time.time()
        count = 0
        if playback:
            try:
                playback.close()
            except Exception:
                event = "failed"
            count = playback.consumed
        await self._report_output(event, playback_id, count, playback.metrics() if playback else {})

    async def observe_output(self):
        playback = self.playback
        if playback is None:
            return
        try:
            active = playback.stream.is_active()
        except Exception:
            await self._end_output("failed", playback.id)
            return
        if not active:
            await self._end_output("drained" if playback.completed else "failed", playback.id)
        elif playback.consumed > playback.reported:
            playback.reported = playback.consumed
            await self._report_output("progress", playback.id, playback.reported)

    async def handle_audio_message(self, msg):
        if msg.get("type") == "audio_session":
            if self.audio_session is None and isinstance(msg.get("audio_session"), str):
                self.audio_session = msg["audio_session"]
                await self._ack_voice(msg)
                _log.info("audio client connected protocol=%s", PROTOCOL)
            return
        if "voice_revision" in msg:
            await self._ack_voice(msg)
            return
        if msg.get("hot_window") is True:
            self.hot_window_active = bool(msg.get("active"))
        elif msg.get("ignored") is True:
            self.server_busy = self.was_busy_on_record
            self.cooldown_start_time = time.time()
        elif msg.get("end") is True:
            self.server_busy = False
            self.hot_window_active = False
            if self.playback is None:
                self.cooldown_start_time = time.time()
        if self.audio_session is None or msg.get("audio_session") != self.audio_session:
            return
        playback_id = msg.get("playback")
        if not isinstance(playback_id, str) or playback_id in self.retired_playbacks:
            return
        if msg.get("speech_stop") is True:
            await self._end_output("cancelled", playback_id)
        elif msg.get("type") == "audio_end":
            if self.playback is not None and self.playback.id == playback_id:
                if msg.get("bytes") != self.playback.received:
                    await self._end_output("failed", playback_id)
                else:
                    self.playback.sealed = True
        elif msg.get("type") == "audio_pcm":
            if self.playback is not None and self.playback.id != playback_id:
                await self._end_output("failed", self.playback.id)
            try:
                if not self.play_audio or self.recording:
                    raise RuntimeError("Local speech is suppressed")
                pcm = base64.b64decode(msg.get("pcm", ""), validate=True)
                if self.playback is None:
                    playback = self.playback = DevicePlayback(playback_id)
                    playback.append(pcm)
                    playback.stream = self.p.open(format=FORMAT, channels=CHANNELS, rate=PLAYBACK_RATE,
                        output=True, start=False, frames_per_buffer=CHUNK, stream_callback=playback.callback)
                    playback.stream.start_stream()
                else:
                    self.playback.append(pcm)
                self.response_speaking = True
                self.last_action_time = time.time()
            except Exception:
                await self._end_output("failed", playback_id)

    async def receive_audio_v2(self):
        while True:
            try:
                data = await asyncio.wait_for(self.websocket.recv(), timeout=0.1)
            except asyncio.TimeoutError:
                await self.observe_output()
                continue
            except ConnectionClosed:
                return
            if isinstance(data, str):
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await self.handle_audio_message(msg)
            await self.observe_output()

    async def main(self):
        while True:
            await self.connect_with_retry()
            self.audio_v2 = self.websocket.subprotocol == PROTOCOL
            self.voice_paused_by_user = self.audio_v2
            self.audio_session = None
            self.voice_revision = -1
            self.retired_playbacks.clear()
            self.stale_audio = False
            self.recording = self.response_speaking = self.server_busy = self.hot_window_active = False
            tasks = [asyncio.create_task(self.receive_audio())]
            if not self.startup_check:
                tasks.append(asyncio.create_task(self.send_audio()))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except (OSError, ConnectionClosed):
                pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.playback is not None:
                    try:
                        self.playback.close()
                    except Exception:
                        pass
                    self.playback = None
                self._flush_playback()
                if self.input_stream is not None:
                    try:
                        self.input_stream.close()
                    finally:
                        self.input_stream = None
                await self.websocket.close()
            await asyncio.sleep(0.25)

    def start(self):
        asyncio.run(self.main())

def run(server_url, debug):
    device = Device()
    device.server_url = server_url
    device.debug = debug
    device.start()
