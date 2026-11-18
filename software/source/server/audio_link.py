"""Connection-owned audio observations over the existing local Light WebSocket."""
import asyncio
import base64
import json
import os
import math
from dataclasses import dataclass, field
from threading import RLock, Thread
import time
from uuid import uuid4

PROTOCOL = "friday.audio.v2"
_TERMINAL = {"drained", "cancelled", "failed", "disconnected", "no_audio"}


@dataclass(eq=False)
class Playback:
    token: object
    session: str
    modern: bool
    id: str = field(default_factory=lambda: uuid4().hex)
    created: float = field(default_factory=time.time)
    workers: set = field(default_factory=set)
    final: bool = False
    sealable: bool = True
    sealed: bool = False
    bytes_sent: int = 0
    bytes_reported: int = 0
    outcome: str = "waiting"
    metrics: dict = field(default_factory=dict)
    first: float | None = None
    last: float | None = None


class AudioLink:
    def __init__(self, state):
        self.state = state
        self.lock = RLock()
        self.session = None
        self.modern = False
        self.revision = 0
        self.applied_revision = None
        self.control = "disconnected"
        self.last_request_error = None
        self.playback = None

    def connect(self, modern):
        with self.lock:
            if self.session is not None:
                return None
            self.session = uuid4().hex
            self.modern = modern
            self.applied_revision = None
            self.control = "queued" if modern else "unconfirmed"
            return self.session

    def disconnect(self, session):
        with self.lock:
            if self.session != session:
                return
            self.session = None
            self.modern = False
            self.applied_revision = None
            self.control = "disconnected"
            if self.playback and self.playback.session == session and self.playback.outcome not in _TERMINAL:
                self.playback.outcome = "disconnected"
            # Invalidate queued PCM and old synthesis callbacks before a new
            # socket can acquire the output reader.
            self.state["discarded_synthesis"] = self.state.get("playback_attempt") is not None
            self.state["playback_attempt"] = None

    def voice_frame(self):
        with self.lock:
            return {"voice_paused": self.state["is_paused"], "end": True,
                    "audio_session": self.session, "voice_revision": self.revision}

    def set_voice(self, paused, queue):
        with self.lock:
            frame = {**self.voice_frame(), "voice_paused": paused,
                     "voice_revision": self.revision + 1}
            try:
                queue.put_nowait(frame)
            except Exception:
                # This request was never accepted. Preserve the previous
                # revision and its still-valid delayed client receipt.
                self.last_request_error = "queue_rejected"
                return None
            self.revision += 1
            self.last_request_error = None
            self.state["is_paused"] = paused
            self.control = ("queued" if self.modern else "unconfirmed") if self.session else "disconnected"
            if paused:
                self.state.update(hot_window_active=False, hot_window_expires_at=0.0)
            return self.voice_snapshot()

    def voice_snapshot(self):
        with self.lock:
            return {"state": self.control, "revision": self.revision,
                    "connected": self.session is not None, "supported": self.modern,
                    "startup_check": self.state.get("audio_startup_check") is True}

    def accept_voice(self, session, message):
        with self.lock:
            if (not self.modern or self.session != session or message.get("audio_session") != session
                    or type(message.get("voice_revision")) is not int
                    or message["voice_revision"] != self.revision
                    or message.get("voice_paused") is not self.state["is_paused"]
                    or self.control not in ("queued", "sent")
                    or type(message.get("applied")) is not bool):
                return False
            self.control = "applied" if message["applied"] else "failed"
            if message["applied"]:
                self.applied_revision = self.revision
            return True

    def begin(self, token):
        with self.lock:
            self.playback = Playback(token, self.session, self.modern) if self.session else None

    def watch(self, token, worker, enqueue):
        with self.lock:
            playback = self.playback
            if not playback or not playback.modern or playback.token is not token:
                return
            if worker is None:
                playback.sealable = False
                return
            playback.workers.add(worker)

        def finished():
            worker.join()
            with self.lock:
                playback.workers.discard(worker)
                self._seal(playback, enqueue)

        Thread(target=finished, name="friday-synthesis-end", daemon=True).start()

    def finish_synthesis(self, token, enqueue):
        with self.lock:
            playback = self.playback
            if playback and playback.token is token:
                playback.final = True
                self._seal(playback, enqueue)

    def _seal(self, playback, enqueue):
        if (self.playback is playback and playback.session == self.session and
                playback.final and playback.sealable and not playback.workers and not playback.sealed):
            playback.sealed = True
            enqueue({"type": "audio_end", "audio_session": playback.session, "playback": playback.id})

    def packet(self, token, pcm):
        with self.lock:
            if not self.session:
                return pcm
            playback = self.playback
            if (not playback or playback.token is not token or playback.session != self.session
                    or playback.outcome in _TERMINAL or playback.outcome == "stop_requested"):
                return None
            return {"type": "audio_pcm", "audio_session": playback.session, "playback": playback.id,
                    "pcm": base64.b64encode(pcm).decode("ascii"), "size": len(pcm)}

    def cancel(self):
        with self.lock:
            playback = self.playback
            if (not playback or playback.session != self.session or
                    playback.outcome in _TERMINAL):
                return {}
            playback.outcome = "stop_requested"
            return {"audio_session": playback.session, "playback": playback.id}

    def outgoing(self, session, value, modern):
        """Fence queued output against the current connection before sending."""
        with self.lock:
            if session != self.session:
                return None
            if isinstance(value, bytes):
                return None if modern else value
            if not isinstance(value, dict):
                return value
            if "voice_revision" in value:
                if value["audio_session"] != session or value["voice_revision"] != self.revision:
                    return None
                if modern:
                    if self.control == "queued":
                        self.control = "sent"
                else:
                    return {"voice_paused": value["voice_paused"], "end": True}
            if value.get("type") in ("audio_pcm", "audio_end"):
                playback = self.playback
                if (not playback or value.get("playback") != playback.id
                        or value.get("audio_session") != session or playback.outcome in _TERMINAL
                        or playback.outcome == "stop_requested"):
                    return None
                if value["type"] == "audio_pcm":
                    if not modern:
                        return base64.b64decode(value["pcm"])
                    playback.bytes_sent += value["size"]
                elif playback.bytes_sent:
                    value = {**value, "bytes": playback.bytes_sent}
                    playback.outcome = "buffered"
                else:
                    playback.outcome = "no_audio"
                    return None
            if modern and value.get("speech_stop") and value.get("audio_session") != session:
                return None
            return value

    def accept_playback(self, session, message):
        with self.lock:
            playback = self.playback
            event, count = message.get("event"), message.get("bytes")
            if (not playback or session != self.session or session != playback.session
                    or message.get("audio_session") != session or message.get("playback") != playback.id
                    or playback.outcome in _TERMINAL or type(count) is not int
                    or not playback.bytes_reported <= count <= playback.bytes_sent):
                return False
            if event == "drained":
                if playback.outcome != "buffered" or count == 0 or count != playback.bytes_sent:
                    return False
            elif event == "progress":
                if count <= playback.bytes_reported or playback.outcome == "stop_requested":
                    return False
            elif event == "cancelled":
                if playback.outcome != "stop_requested":
                    return False
            elif event != "failed":
                return False
            playback.bytes_reported = count
            reported = message.get("metrics", {})
            if isinstance(reported, dict):
                for key in ("first_callback_ms", "starved_callbacks", "device_underflows", "inserted_silence_frames", "peak_buffer_ms"):
                    value = reported.get(key)
                    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1_000_000:
                        playback.metrics[key] = value
            playback.last = time.time()
            if count and playback.first is None:
                playback.first = max(0, playback.last - playback.created)
            if event != "progress":
                playback.outcome = event
            return True

    def playback_snapshot(self):
        with self.lock:
            p = self.playback
            if p is None or not p.modern:
                return {}
            return {"playback_outcome": p.outcome, "bytes_played": p.bytes_reported,
                    "client_timing": dict(p.metrics),
                    "timing_scope": "PortAudio callback consumption, not proof of audible perception",
                    "client_playback_observable": p.bytes_reported > 0,
                    "first_audio_seconds": p.first, "last_progress_at": p.last,
                    "drained": p.outcome == "drained",
                    "playback_recent": bool(p.last and p.bytes_reported and p.outcome in ("waiting", "buffered")
                                            and time.time() - p.last <= 3)}


class AudioWebSocket:
    """Negotiate audio v2 on the existing route. Keep legacy wire format."""
    def __init__(self, app, link, interpreter):
        self.app, self.link, self.interpreter = app, link, interpreter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket" or scope["path"] != "/":
            return await self.app(scope, receive, send)
        modern = PROTOCOL in scope.get("subprotocols", ())
        if modern and (scope.get("client", ("",))[0] not in ("127.0.0.1", "::1")
                       or os.getenv("INTERPRETER_REQUIRE_AUTH") != "False"):
            return await send({"type": "websocket.close", "code": 1008})
        session = self.link.connect(modern)
        if session is None:
            return await send({"type": "websocket.close", "code": 1013})
        try:
            if modern:
                await self._modern(session, receive, send)
            else:
                await self._legacy(session, scope, receive, send)
        finally:
            self.link.disconnect(session)

    @staticmethod
    async def _paired(*coroutines):
        tasks = [asyncio.create_task(coro) for coro in coroutines]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _modern(self, session, receive, send):
        await receive()  # websocket.connect
        await send({"type": "websocket.accept", "subprotocol": PROTOCOL})

        async def send_value(value):
            await send({"type": "websocket.send", "text": json.dumps(value)})

        hello = {"type": "audio_session", **self.link.voice_frame()}
        await send_value(hello)
        capture_revision = None

        async def read_input():
            nonlocal capture_revision
            while True:
                event = await receive()
                if event["type"] == "websocket.disconnect":
                    return
                if event.get("bytes") is not None:
                    if self._can_capture(capture_revision):
                        await self.interpreter.input(event["bytes"])
                    continue
                try:
                    value = json.loads(event.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if not isinstance(value, dict):
                    continue
                kind = value.get("type")
                if kind == "voice_input_ack":
                    if self.link.accept_voice(session, value) and capture_revision is not None and not self._can_capture(capture_revision):
                        await self.interpreter.input({"type": "audio_capture_cancel"})
                        capture_revision = None
                elif kind == "playback":
                    self.link.accept_playback(session, value)
                elif kind == "audio":
                    if value.get("start") is True and self._can_capture(value.get("voice_revision")):
                        if capture_revision is not None:
                            await self.interpreter.input({"type": "audio_capture_cancel"})
                        capture_revision = value["voice_revision"]
                        await self.interpreter.input(value)
                    elif value.get("end") is True and self._can_capture(capture_revision) and capture_revision == value.get("voice_revision"):
                        capture_revision = None
                        await self.interpreter.input(value)

        async def write_output():
            while True:
                value = self.link.outgoing(session, await self.interpreter.output(), True)
                if value is not None:
                    await send_value(value)

        try:
            await self._paired(read_input(), write_output())
        except OSError:
            pass  # send after disconnect, never replay
        finally:
            if capture_revision is not None:
                await self.interpreter.input({"type": "audio_capture_cancel"})

    def _can_capture(self, revision):
        with self.link.lock:
            return (type(revision) is int and revision == self.link.revision
                    and self.link.applied_revision == revision and not self.link.state["is_paused"])

    async def _legacy(self, session, scope, receive, send):
        disconnected = asyncio.Event()
        backlog = getattr(self.interpreter, "unsent_messages", None)
        if backlog is not None:
            # Legacy OI persists unsent output. Speech is never resumable.
            stale = [item for item in backlog if not isinstance(item, bytes)
                     and not (isinstance(item, dict) and (item.get("type") in
                              ("speech_frame", "audio", "audio_pcm", "audio_end")
                              or item.get("speech_stop") or "voice_paused" in item))]
            backlog.clear()
            backlog.extend(stale)

        async def watched_receive():
            event = await receive()
            if event["type"] == "websocket.disconnect":
                disconnected.set()
            return event

        async def filtered_send(event):
            if event["type"] == "websocket.send" and "text" in event:
                value = self.link.outgoing(session, json.loads(event["text"]), False)
                if value is None:
                    return
                event = ({"type": "websocket.send", "bytes": value} if isinstance(value, bytes)
                         else {**event, "text": json.dumps(value)})
            try:
                await send(event)
                if (event["type"] == "websocket.accept" and self.link.state["is_paused"]
                        and os.getenv("INTERPRETER_REQUIRE_AUTH") == "False"):
                    await send({"type": "websocket.send", "text": '{"voice_paused":true,"end":true}'})
            except OSError:
                disconnected.set()

        await self._paired(self.app(scope, watched_receive, filtered_send), disconnected.wait())
