"""Track audio-client acknowledgements for current, explicitly tagged speech."""

import time
import uuid
from threading import RLock


_speech_state_lock = RLock()


def speech_snapshot(server_state):
    """Queued -> synthesized speech metrics for /voice/trace.

    Deliberately reports `client_playback_observable: False` unless the OUTPUT
    CLIENT has actually acknowledged audio: the server runs muted and the
    client plays the PCM, so `tts_end` is stream teardown, not the end of what
    Sir heard. ROUND ZB-C adds real client acknowledgements, so this now also
    carries the utterance, its phrase frames and the measured
    feed -> first-audible delta - but only when the client reported it.
    """
    if not isinstance(server_state, dict):
        return {"pending": 0, "client_playback_observable": False,
                "client_playback_note": "no speech queued yet"}
    with _speech_state_lock:
        playback = dict(server_state.get("playback") or {})
        frames = list(server_state.get("speech_frames") or [])
        stale = bool(server_state.get("speech_stale"))
        stop_reason = str(server_state.get("speech_stop_reason") or "")
    queue = server_state.get("speech_queue")
    progress_at = playback.get("last_at")
    base = {
        "pending": 0,
        "frames": frames,
        "frames_pending": len(frames),
        "utterance": frames[-1].get("utterance") if frames else "",
        "stale": stale,
        "stop_reason": stop_reason,
        # Observable ONLY because a client told us so.
        "client_playback_observable": bool(playback.get("client_seen")),
        "client_playback_note": (
            "output client acknowledged audio"
            if playback.get("client_seen")
            else "no playback acknowledgement received from the output client"
        ),
        "first_audio_seconds": playback.get("first_audio_seconds"),
        "bytes_played": int(playback.get("bytes") or 0),
        "last_progress_at": progress_at,
        "playback_recent": bool(progress_at and not stale and not playback.get("drained")
                                and -0.001 <= time.time() - progress_at <= 3),
        "drained": bool(playback.get("drained")),
        "dropped_bytes": int(playback.get("dropped") or 0),
    }
    link = server_state.get("audio_link")
    if link is not None:
        observed = link.playback_snapshot()
        if observed:
            base.update(observed)
            base["client_playback_note"] = "PortAudio output stream reported by the connected client"
    if queue is None:
        return base
    try:
        merged = dict(queue.snapshot())
        merged.update(base)
        return merged
    except Exception as error:
        base["error"] = type(error).__name__
        return base


def emit_speech_frame(server_state, text):
    """Label one fed phrase so the client can caption and acknowledge it.

    Returns the frame, or None. Called for every phrase handed to the TTS
    engine: the client has no way to infer phrase boundaries from a raw PCM
    stream, so the boundary has to arrive as a message.
    """
    with _speech_state_lock:
        if not isinstance(server_state, dict):
            return None
        seq = int(server_state.get("speech_seq") or 0) + 1
        frame = {
            "role": "assistant",
            "type": "speech_frame",
            "utterance": uuid.uuid4().hex,
            "seq": seq,
            "text": str(text or "")[:240],
            "at": round(time.time(), 3),
        }
        frames = list(server_state.get("speech_frames") or [])
        frames.append(frame)
        del frames[:-40]                       # bounded caption history
        server_state.update(speech_seq=seq, speech_stale=False,
                            speech_frames=frames, playback={})
        return frame


def notify_speech_stop(server_state, reason="", queue=None):
    """Tell the output client to DISCARD buffered speech, and forget captions.

    Called from every stop path (voice stop phrase, supersession, /chat/cancel
    and the HUD Stop) so work, voice, captions and any bounce all end together
    and no stale phrase can be spoken after the user said stop.
    """
    if not isinstance(server_state, dict):
        return {"stale": False}
    link = server_state.get("audio_link")
    target = link.cancel() if link is not None else {}
    with _speech_state_lock:
        frames = server_state.get("speech_frames") or []
        utterance = frames[-1]["utterance"] if frames else ""
        if utterance:
            server_state["stopped_speech_utterance"] = utterance
        server_state.update(speech_frames=[], playback_attempt=None, speech_stale=True,
                            speech_stop_reason=str(reason or ""), playback={})
    if queue is not None:
        try:
            queue.sync_q.put({"speech_stop": True, "utterance": utterance,
                              "reason": str(reason or ""), **target})
        except Exception:
            pass
    # Tell the HUD immediately so the caption and the globe bounce stop TOGETHER
    # with the audio, instead of on the next poll tick. Lazy import: this is
    # module-level and status_ui imports server, so it must not run at import.
    try:
        from .status_ui import emit_ui_event
        emit_ui_event("stop", {"reason": str(reason or ""),
                               "utterance": utterance})
    except Exception:
        pass
    return {"stale": True, "utterance": utterance}


def record_playback(server_state, chunk):
    """Record an acknowledgement from the OUTPUT CLIENT.

    This is the only evidence of *audible* speech that exists: the server runs
    muted, so everything before this point measures requests, not sound.
    Never raises - a bad client frame must not break the input path.
    """
    if not isinstance(server_state, dict) or not isinstance(chunk, dict):
        return {}
    event = chunk.get("event")
    utterance = chunk.get("utterance")
    count = chunk.get("bytes", 0)
    if not isinstance(utterance, str) or not utterance or type(count) is not int or count < 0:
        return {}
    with _speech_state_lock:
        playback = server_state.get("playback") or {}
        if (event == "stale_dropped" and server_state.get("speech_stale")
                and utterance == server_state.get("stopped_speech_utterance")):
            playback["dropped"] = max(playback.get("dropped", 0), count)
        else:
            frames = server_state.get("speech_frames") or []
            if ((event == "drained" and server_state.get("audio_link") is not None)
                    or event not in ("first_audio", "progress", "drained")
                    or server_state.get("speech_stale") or not frames
                    or utterance != frames[-1]["utterance"] or playback.get("drained")):
                return {}
            if event == "first_audio" and playback.get("client_seen"):
                return {}
            if event == "drained" and not playback.get("client_seen"):
                return {}
            if event == "progress" and count <= playback.get("bytes", 0):
                return {}
            now = time.time()
            playback.update(client_seen=True, last_event=event, last_at=round(now, 3),
                            utterance=utterance, drained=event == "drained")
            if event == "first_audio":
                playback["first_audio_seconds"] = round(max(0.0, now - frames[-1]["at"]), 3)
            if event in ("first_audio", "progress"):
                playback["bytes"] = count
        server_state["playback"] = playback
        return dict(playback)
