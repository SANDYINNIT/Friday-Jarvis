"""Privacy-safe display projection over existing voice and screen observations."""

import math
import time


def _number(value):
    return value if type(value) in (float, int) and math.isfinite(value) and value >= 0 else None


def _screen(state):
    result = {"background": "unavailable", "background_enabled": None,
              "history_seconds": None, "frame_age": None,
              "request": "observed" if state.get("active_vision_pool") == "active" else "unobserved"}
    capture = state.get("screen_capture")
    if capture is None:
        return result
    fps = _number(getattr(capture, "target_fps", None))
    if fps is not None:
        result["background_enabled"] = fps > 0
    try:
        from .screen_capture_service import DEFAULT_MAX_AGE, history_seconds

        result["history_seconds"] = _number(max(0.0, history_seconds()))
        health = capture.health()
        age = _number(health.get("last_frame_age"))
        if health.get("running") is True:
            result.update(background="fresh" if age is not None and age <= DEFAULT_MAX_AGE
                          else "waiting" if age is None else "stale", frame_age=age)
        elif health.get("running") is False:
            result["background"] = "stopped"
        if health.get("last_error") or health.get("degraded"):
            result["reported_issue"] = True
    except Exception:
        result["background"] = "error"
    return result


def voice_screen_snapshot(status, state, speech):
    if status.state == "listening" and status.detail == "Listening for a command":
        activity = "listening"
    elif status.state == "thinking" and status.detail == "Transcribing and routing":
        activity = "transcribing"
    elif status.state == "thinking":
        activity = "processing"
    elif status.state == "error":
        activity = "error"
    else:
        activity = "unobserved"
    paused = state.get("is_paused")
    deadline = _number(state.get("hot_window_expires_at"))
    followup = bool(not paused and state.get("hot_window_active") and deadline
                    and deadline > time.monotonic()) if deadline is not None else None
    outcome = speech.get("playback_outcome")
    if outcome in ("buffered", "drained", "cancelled", "stop_requested", "failed", "disconnected", "no_audio"):
        playback = "client_cancelled" if outcome == "cancelled" else outcome
    elif "error" in speech:
        playback = "unavailable"
    elif speech.get("stale"):
        playback = "cancelled"
    elif speech.get("drained"):
        playback = "drained"
    elif speech.get("client_playback_observable"):
        playback = "recent" if speech.get("playback_recent") else "unobserved"
    elif speech.get("frames") or status.state == "speaking":
        playback = "unconfirmed"
    else:
        playback = "unobserved"

    synthesis = None
    engine = state.get("tts_engine")
    stage = getattr(engine, "LAST_STAGE", None)
    if stage in ("gemini", "local", "sapi", "none"):
        synthesis = stage
    degraded = None
    try:
        if callable(reader := getattr(engine, "degraded_status", None)):
            degraded = reader().get("degraded") is True
    except Exception:
        pass
    link = state.get("audio_link")
    return {
        "available": True,
        "sequence": status.sequence,
        "listening": {"paused": paused if type(paused) is bool else None,
                      "client_receipt": link.voice_snapshot() if link is not None else {"state": "unconfirmed"},
                      "activity": activity, "followup": followup,
                      "wake_match_at": _number(state.get("last_wake_match_at"))},
        "speaking": {"playback": playback, "synthesis_result": synthesis,
                     "degraded": degraded,
                     "first_audio_seconds": _number(speech.get("first_audio_seconds"))
                     if speech.get("client_playback_observable") and not speech.get("stale") else None},
        "screen": _screen(state),
    }
