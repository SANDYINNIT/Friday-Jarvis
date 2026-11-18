"""Actual Voice & Screen bridge with synthetic status and transports."""

from queue import Queue
import time
from types import SimpleNamespace

from source.server import server, status_ui


def bridge_for(state, queue=None):
    bus = status_ui.StatusBus()
    runtime = SimpleNamespace(server_state=state, output_queue=SimpleNamespace(sync_q=queue))
    return status_ui.WebviewBridge(bus, runtime), bus, runtime


def test_readers_project_state_without_media_or_raw_details():
    def forbidden(*_args):
        raise AssertionError("reader tried to activate media")

    state = {
        "is_paused": False, "hot_window_active": True, "hot_window_expires_at": time.monotonic() + 60,
        "last_wake_match_at": time.time(), "active_vision_pool": "active",
        "screen_capture": SimpleNamespace(start=forbidden, frame=forbidden, poll_once=forbidden,
            health=lambda: {"running": True, "last_frame_age": 0.1, "last_error": "<script>private</script>"}),
        "tts_engine": SimpleNamespace(synthesize=forbidden, LAST_STAGE="gemini",
                                     degraded_status=lambda: {"degraded": False, "reason": "private endpoint"}),
    }
    bridge, bus, runtime = bridge_for(state, Queue())
    runtime.stt = SimpleNamespace(start=forbidden, text=forbidden)
    frame = server.emit_speech_frame(state, "<img>private caption")
    bus.publish("listening", "Listening for a command")
    first = bridge.get_voice_screen()
    assert first["listening"]["activity"] == "listening"
    assert first["listening"]["followup"] and first["listening"]["control_available"]
    assert first["screen"]["background"] == "fresh" and first["screen"]["reported_issue"]
    assert first["screen"]["request"] == "observed"
    assert first["speaking"]["playback"] == "unconfirmed"
    assert first["speaking"]["synthesis_result"] == "gemini"
    assert "private" not in repr(first)
    server.record_playback(state, {"event": "first_audio", "utterance": frame["utterance"], "bytes": 4})
    bus.publish("thinking", "Transcribing and routing")
    second = bridge.get_voice_screen()
    assert second["listening"]["activity"] == "transcribing"
    assert second["speaking"]["playback"] == "recent"
    bus.publish("thinking", "FRIDAY is working")
    assert bridge.get_voice_screen()["listening"]["activity"] == "processing"
    assert runtime.output_queue.sync_q.empty()


def test_capture_degrades_and_missing_runtime_is_unavailable():
    capture = SimpleNamespace(health=lambda: {"running": True, "last_frame_age": 1000})
    bridge, _, _ = bridge_for({"screen_capture": capture})
    assert bridge.get_voice_screen()["screen"]["background"] == "stale"
    capture.health = lambda: {"running": True, "last_frame_age": None}
    assert bridge.get_voice_screen()["screen"]["background"] == "waiting"
    capture.health = lambda: {"running": False}
    assert bridge.get_voice_screen()["screen"]["background"] == "stopped"
    capture.health = lambda: (_ for _ in ()).throw(OSError("private endpoint"))
    assert bridge.get_voice_screen()["screen"]["background"] == "error"
    assert status_ui.WebviewBridge(status_ui.StatusBus()).get_voice_screen() == {"available": False}


def test_session_control_acknowledges_queue_and_preserves_active_status_on_failure():
    queue = Queue(maxsize=1)
    state = {"is_paused": False, "hot_window_active": True, "hot_window_expires_at": time.monotonic()+30}
    bridge, bus, runtime = bridge_for(state, queue)
    resumed = []
    runtime.resume_owned_voice = lambda: resumed.append(True)
    snapshot = bus.publish("thinking", "FRIDAY is working")
    queue.put_nowait("busy")
    assert bridge.set_paused(True)["accepted"] is False
    assert state["is_paused"] is False and state["hot_window_active"] is True
    assert queue.get_nowait() == "busy"
    assert bridge.set_paused(True)["accepted"] is True
    assert queue.get_nowait() == {"voice_paused": True, "end": True}
    assert state["is_paused"] is True and state["hot_window_active"] is False
    assert bridge.set_paused(False)["accepted"] is True
    assert queue.get_nowait() == {"voice_paused": False, "end": True}
    assert bus.snapshot() == snapshot and resumed == [True]
    assert bridge_for(state)[0].set_paused(True)["accepted"] is False


def test_old_playback_cannot_revive_stop_or_replace_a_new_phrase():
    state = {}
    bridge, _, _ = bridge_for(state)
    old = server.emit_speech_frame(state, "old")["utterance"]
    server.notify_speech_stop(state, "stop")
    assert server.record_playback(state, {"event": "first_audio", "utterance": old, "bytes": 5}) == {}
    assert bridge.get_voice_screen()["speaking"]["playback"] == "cancelled"
    current = server.emit_speech_frame(state, "new")["utterance"]
    assert current != old
    assert server.record_playback(state, {"event": "progress", "utterance": old, "bytes": 900}) == {}
    assert server.record_playback(state, {"event": "progress", "bytes": 900}) == {}
    assert bridge.get_voice_screen()["speaking"]["playback"] == "unconfirmed"
    assert server.record_playback(state, {"event": "first_audio", "utterance": current, "bytes": 10})["bytes"] == 10
    assert server.record_playback(state, {"event": "drained", "utterance": old}) == {}
    assert bridge.get_voice_screen()["speaking"]["playback"] == "recent"
    server.record_playback(state, {"event": "drained", "utterance": current})
    assert server.record_playback(state, {"event": "progress", "utterance": current, "bytes": 11}) == {}
    assert bridge.get_voice_screen()["speaking"]["playback"] == "drained"
