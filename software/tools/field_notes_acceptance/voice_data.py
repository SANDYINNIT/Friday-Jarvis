"""Synthetic audio, capture and provider boundaries for the shipped status page."""

import time
from types import SimpleNamespace

from source.server.speech_playback import emit_speech_frame, notify_speech_stop, record_playback


class VoiceData:
    def __init__(self, runtime, bus):
        self.runtime, self.bus = runtime, bus
        self.calls = []
        self.health = {"running": True, "last_frame_age": 0.4}
        self.health_error = False
        self.frames = {}
        state = runtime.server_state
        state.update(hot_window_active=True, hot_window_expires_at=time.monotonic() + 300,
                     last_wake_match_at=time.time(), active_vision_pool=None)
        state["screen_capture"] = SimpleNamespace(target_fps=2, health=self.read_health, start=self.forbid,
                                                  frame=self.forbid, poll_once=self.forbid)
        state["tts_engine"] = SimpleNamespace(synthesize=self.forbid, LAST_STAGE="local",
                                              degraded_status=lambda: {"degraded": False})
        runtime.stt = SimpleNamespace(start=self.forbid, stop=self.forbid, text=self.forbid)
        runtime.resume_owned_voice = lambda: self.calls.append("resume")

    def forbid(self, *_args):
        self.calls.append("media call")
        raise AssertionError("Voice & Screen attempted a media operation")

    def read_health(self):
        if self.health_error:
            raise RuntimeError("<script>PRIVATE_RUNTIME_DETAIL</script>")
        return self.health

    def control(self, command):
        action = command["action"].removeprefix("voice:")
        state = self.runtime.server_state
        if action == "status":
            self.bus.publish(command["state"], command["detail"])
        elif action == "health":
            self.health = command["value"]
        elif action == "health-error":
            self.health_error = command["value"]
        elif action == "reader-error":
            self.runtime.voice_reader_error = command["value"]
        elif action == "queue-error":
            self.runtime.voice_queue.reject = command["value"]
        elif action == "vision":
            state["active_vision_pool"] = "active" if command["value"] else None
        elif action == "frame":
            self.frames[command["name"]] = emit_speech_frame(state, "<img onerror=window.bad=true>")["utterance"]
        elif action == "ack":
            record_playback(state, {"event": command["event"], "bytes": 8,
                                    "utterance": self.frames[command["name"]]})
        elif action == "stop-audio":
            notify_speech_stop(state, "fixture_stop")
        return {"paused": state["is_paused"], "queued": self.runtime.voice_queue.frames,
                "calls": self.calls, "utterances": self.frames}
