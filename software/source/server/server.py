from fastapi import Request
from fastapi.responses import PlainTextResponse
from RealtimeSTT import AudioToTextRecorder
from RealtimeTTS import TextToAudioStream
import importlib
import asyncio
import types
import time
import tempfile
import wave
import os
import threading
import re
import unicodedata
import json
from datetime import datetime, timezone
from .screen_understanding import is_screen_request, screen_response, author_screen_photo
from .memory import (
    MemoryStore,
    build_memory_context,
    extract_and_remember_automatic,
    format_memory_response,
    parse_memory_command,
)
from .status_ui import (
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_LISTENING,
    STATUS_SPEAKING,
    STATUS_THINKING,
)
from .reminders import ReminderStore, ReminderWorker, parse_reminder
from .audit import log_action
from .speech_filters import reject_reason
from .intent_judge import AmbientBuffer, JudgeClient, judge as _intent_judge, judge_enabled
from .command_router import route as route_command
from .command_router import configure_calendar as _configure_calendar_route
from .schedule_store import ScheduleStore as _ScheduleStore
from .remote_telegram import load_config as load_telegram_config
from .digest import maybe_run_digest
from .redaction import redact as _redact
from .file_logger import setup_file_logging, friday_logger
from .desktop_ear import DesktopEar, handle_ear_command
from .social_guard import SocialGuard
from .note_taker import maybe_record_project_note
from .focus_tracker import FocusTracker
from .proactive import WelcomeBackMonitor
from . import api_pools
from .cloud_stt import pcm_to_wav, transcribe_wav
from .brain_router import BrainRouter
from . import brain_router as _brain_router_module

os.environ["INTERPRETER_REQUIRE_ACKNOWLEDGE"] = "False"
os.environ["INTERPRETER_REQUIRE_AUTH"] = "False"

TEXT_CHANNEL_CHAT_ID = "@text-channel"

# The most recent script FRIDAY wrote and ran this process, so she can be asked
# "which script did you just use?" and answer with the real code instead of a
# vague claim. Read by self_awareness.last_script(). No secrets: redacted.
LAST_TOOL_SCRIPT = {}

def emit_input_terminal(output_queue):
    """Release a light client whose audio request failed before dispatch."""
    output_queue.sync_q.put({"ignored": True, "end": True})

def _is_tts_text_speakable(content, existing_text):
    # Edge TTS rejects punctuation-only fragments such as "?". Sentence
    # punctuation is optional for synthesis, so never send it by itself.
    return any(character.isalnum() for character in content)


def _automatic_memory_enabled(interpreter):
    configured = os.environ.get("FRIDAY_AUTO_MEMORY")
    if configured is not None:
        return configured.strip() == "1"
    return bool(getattr(interpreter, "friday_auto_memory_enabled", False))


def _reminders_enabled(interpreter):
    configured = os.environ.get("FRIDAY_REMINDERS")
    if configured is not None:
        return configured.strip() == "1"
    return bool(getattr(interpreter, "friday_reminders_enabled", False))


def _begin_single_turn(interpreter, server_state, *, user="", source=None):
    if server_state.get("single_turn_loop") is None:
        server_state["single_turn_loop"] = getattr(interpreter, "loop", True)
        interpreter.loop = server_state["single_turn_loop"]
    # Multi-step chaining runs with the loop engine ON but SAFE: server.py's
    # per-turn llm-call loop guard (traced_completions, FRIDAY_LLM_LOOP_GUARD)
    # force-breaks any text-only repeat loop instead of letting it spin.
    _begin_turn_log(server_state, user=user, source=source)


_TURN_TEXT_CHAR_LIMIT = 12000
_TURN_SNIPPET_LIMIT = 12000
_TURN_THINKING_LIMIT = 200
_TURN_CHUNK_LIMIT = 200
_TURN_ACTION_LIMIT = 50


def _redact_text(value):
    text = str(value or "")
    return _redact(text) if text else ""


def _redact_for_phone(value):
    """Owner-bound Telegram replies: mask ONLY genuine secrets (tokens, keys,
    emails, cards, phones, IPs) but KEEP real filesystem paths. The full
    redact() turns ``C:\\work\\project`` into ``<PATH>``, which made SR's
    honest path answers arrive as literal "<PATH>" on the phone."""
    text = str(value or "")
    return _redact(text, mask_paths=False) if text else ""


# Follow-up turns that reference a screenshot FRIDAY just sent get the saved
# vision read injected into brain context so she never pleads blind.
_SCREENSHOT_REFERENCE_RE = re.compile(
    r"\bscreenshot\b|\b(?:the\s+)photo\b|\bthe\s+(?:picture|shot)\b|"
    r"\b(?:did\s+you|do\s+you)\s+(?:see|catch|lose|get)\b|"
    r"\bwhat\s+did\s+you\s+(?:just\s+)?(?:send)\b|\byou\s+(?:just\s+)?sent\b"
)


_CONSOLE_NOISE_RE = re.compile(r"[\s0-9=.<>+\-_,;:/]*[0-9][\s0-9=.<>+\-_,;:/]*")


def _console_noise(text):
    """Tiny numeric dribbles (kernel counters like '7', '1 2 3 4', '= 32')
    that confuse the console read without any actionable value."""
    stripped = str(text or "").strip()
    return bool(stripped) and bool(_CONSOLE_NOISE_RE.fullmatch(stripped))


def _looks_like_tool_json(content):
    if not isinstance(content, str):
        return False
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and any(
        key in parsed for key in ("tool_calls", "code", "function")
    )


def _mark_turn_dispatch(server_state, interpreter, *, user, content, source):
    """Remember the current turn's source request so a provider failure can
    be retried on the next pool without losing context."""
    server_state["turn_user_text"] = _redact_text(user)
    server_state["turn_content"] = content
    server_state["turn_source"] = source
    server_state["turn_retry_count"] = 0
    # Provider-hop budget for this turn. Enforced for EVERY channel (voice,
    # typed, Telegram) so a fully dead chain can never loop silently.
    server_state["turn_hop_count"] = 0
    server_state["turn_failed"] = False
    server_state["turn_error"] = ""
    server_state["turn_messages_len"] = len(getattr(interpreter, "messages", []))

    # Turn-scoped blacklist reset: strict forward-only failover — the chain
    # never revisits a candidate that failed earlier in this turn.

    # Disk-backed turn scratchpad: survives provider hops (files are not

    # rewound with interpreter.messages) so each replay inherits what the
    # previous replays tried instead of repeating them blindly.
    try:
        from .self_improve import scratchpad_note, scratchpad_reset

        scratchpad_reset()
        scratchpad_note(f"Sir asked: {str(content or user)[:160]}", tag="request")
    except Exception:
        pass


def _max_turn_retries():
    """Failover budget: every usable Brain cloud key plus model-level chain
    entries plus one local attempt — enough to walk the whole documented
    chain (Groq keys -> Gemini models -> OpenRouter models -> local)."""
    summary = api_pools.describe_pools()
    total_keys = sum(
        summary.get(name, {}).get("total", 0)
        for name in ("brain_groq", "brain_openrouter", "brain_gemini")
    )
    try:
        chain_models = len(_brain_router_module.GEMINI_MODEL_CHAIN) + len(_brain_router_module.OPENROUTER_MODEL_CHAIN)
    except Exception:
        chain_models = 6
    # Keys include one active key per model at a time; add a headroom so the
    # final local attempt is always reachable.
    return max(4, total_keys + chain_models + 1)


def _begin_turn_log(server_state, *, user="", source=None):
    server_state["turn_log"] = {
        "source": source,
        "user": _redact_text(user),
        "assistant_chunks": 0,
        "assistant_len": 0,
        "assistant_snippet": "",
        "thinking": [],
        "actions": [],
    }


def _record_turn_output(server_state, output):
    turn = server_state.get("turn_log")
    if turn is None or not isinstance(output, dict):
        return
    otype = output.get("type")
    if otype == "message":
        content = str(output.get("content") or "")
        if not content or turn["assistant_len"] >= _TURN_TEXT_CHAR_LIMIT:
            return
        turn["assistant_chunks"] += 1
        turn["assistant_len"] += len(content)
        turn["assistant_snippet"] = (turn["assistant_snippet"] + content)[-_TURN_SNIPPET_LIMIT:]
    elif otype == "code" and len(turn["actions"]) < _TURN_ACTION_LIMIT:
        language = str(output.get("language") or "code")
        # Record the code/tool execution attempt, regardless of whether it's 'start' or 'result'
        code = _redact_text(output.get("code") or output.get("result", ""))[:400]
        if not str(code).strip():
            # Empty/malformed tool calls (e.g. Groq "failed to parse tool
            # call arguments") execute nothing — they must NOT count as a
            # performed action, so a turn-level failover stays possible.
            return
        turn["actions"].append({"tool": language, "snippet": code})
        turn["actions"] = turn["actions"][-_TURN_CHUNK_LIMIT:]
    elif otype == "reasoning":
        text = str(output.get("content") or "")
        if text and len(turn["thinking"]) < _TURN_THINKING_LIMIT:
            turn["thinking"].append(_redact_text(text)[:800])


def _flush_turn_log(server_state):
    turn = server_state.get("turn_log")
    server_state["turn_log"] = None
    if not turn:
        return None
    actions = turn["actions"][:20]
    thinking = " ".join(turn["thinking"])
    log_action(
        actor="assistant", risk="conversation", operation="assistant_turn",
        allowed=True, source=turn.get("source"),
        user=turn.get("user", ""),
        response=_redact_text(turn["assistant_snippet"])[:2000],
        response_len=turn["assistant_len"],
        thinking=_redact_text(thinking)[:1000],
        actions=actions,
        action_count=len(turn["actions"]),
    )
    return turn


def _end_single_turn(interpreter, server_state):
    if server_state.get("single_turn_loop") is not None:
        interpreter.loop = server_state["single_turn_loop"]
        server_state["single_turn_loop"] = None


def configure_console_output(interpreter, debug):
    """Keep generated interpreter streams out of the normal voice console."""
    interpreter.verbose = bool(debug)
    interpreter.server.display = bool(debug)
    interpreter.print = bool(debug)


_ECHO_GRACE_SECONDS = 2.0
_ECHO_TEXT_LIMIT = 512
_ECHO_CANDIDATE_LIMIT = 4
_ECHO_WAKE_WORDS = ("friday", "frida", "fry day", "fryday", "fri-day", "friyay", "fire day")

# Spoken "be quiet" commands honored only mid-response (barge-in style).
_STOP_PHRASE_RE = re.compile(
    r"^.{0,20}?(?:stop|stop\s+talking|that'?s\s+enough|that\s+is\s+enough|"
    r"enough|shut\s+up|be\s+quiet|silence)\b[.!?,]*\s*$",
    flags=re.IGNORECASE,
)


def _configured_hot_window_seconds():
    try:
        return max(10.0, min(60.0, float(os.environ.get("FRIDAY_HOT_WINDOW_SECONDS", "30"))))
    except (TypeError, ValueError):
        return 30.0


def _hot_window_is_active(server_state, now=None):
    """Return whether the one-follow-up window is still within its deadline."""
    if now is None:
        now = time.monotonic()
    with server_state["hot_window_lock"]:
        if server_state["hot_window_active"] and server_state["hot_window_expires_at"] > now:
            return True
        server_state["hot_window_active"] = False
        server_state["hot_window_expires_at"] = 0.0
    return False


def _normalize_echo_text(text):
    """Normalize speech text without making approximate command matches."""
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _normalize_echo_match(text):
    if not isinstance(text, str):
        return ""
    for wake_word in sorted(_ECHO_WAKE_WORDS, key=len, reverse=True):
        text = re.sub(r"\b" + re.escape(wake_word) + r"\b", " ", text, flags=re.IGNORECASE)
    return _normalize_echo_text(text)


def _remember_tts_text(server_state, content, now=None):
    """Keep only small exact-match candidates from text actually sent to TTS."""
    if not _normalize_echo_text(content):
        return
    if now is None:
        now = time.monotonic()
    text = str(content)[:_ECHO_TEXT_LIMIT]
    buffer = server_state.get("tts_echo_buffer", "")
    buffer = (buffer + " " + text).strip()[-_ECHO_TEXT_LIMIT:]
    candidates = server_state.setdefault("recent_tts_texts", [])
    candidates.extend(((text, now), (buffer, now)))
    del candidates[:-_ECHO_CANDIDATE_LIMIT]
    server_state["tts_echo_buffer"] = buffer


def _is_recent_tts_echo(candidate, server_state, tts_is_playing, now=None):
    """Return true only for an exact normalized match in the TTS echo window."""
    candidate = _normalize_echo_match(candidate)
    if not candidate:
        return False
    if now is None:
        now = time.monotonic()

    for text, recorded_at in reversed(server_state.get("recent_tts_texts", [])):
        age = now - recorded_at
        if age < 0 or (not tts_is_playing and age > _ECHO_GRACE_SECONDS):
            continue
        if candidate == _normalize_echo_match(text):
            return True
    return False


def _hot_window_candidate(content, server_state, tts_is_playing, now=None):
    """Validate one hot-window transcript without dispatching it."""
    if not _hot_window_is_active(server_state, now):
        return None
    if server_state["is_paused"]:
        return None
    candidate = str(content or "").strip().lower().rstrip(",.?! ")
    if not candidate or _is_recent_tts_echo(candidate, server_state, tts_is_playing, now):
        return None
    last_time = server_state.get("last_wake_time", 0.0)
    if candidate == server_state.get("last_wake_text", "") and time.time() - last_time < 3.0:
        return None
    return candidate


def _find_wake_word(text):
    """Return the first supported wake word found in final STT text."""
    text = str(text or "").casefold()
    for wake_word in _ECHO_WAKE_WORDS:
        if re.search(r"\b" + re.escape(wake_word) + r"\b", text):
            return wake_word
    return ""


def _strip_wake_word(text, wake_word):
    if not wake_word:
        return str(text or "").strip(",.?! ")
    return re.sub(
        r"\b" + re.escape(wake_word) + r"\b",
        "",
        str(text or ""),
        count=1,
        flags=re.IGNORECASE,
    ).strip(",.?! ")


def _beeps_enabled():
    configured = os.environ.get("FRIDAY_BEEPS")
    if configured is not None:
        return configured.strip() == "1"
    return True


def _play_chirp(kind="wake"):
    if not _beeps_enabled():
        return
    tones = {"wake": (880, 90), "tool_start": (660, 60), "tool_complete": (990, 60)}
    frequency, duration = tones.get(kind, (880, 90))

    def _sound():
        try:
            import winsound
            winsound.Beep(frequency, duration)
        except Exception:
            pass

    threading.Thread(target=_sound, daemon=True).start()


def start_server(server_host, server_port, interpreter, voice, debug, status_bus=None):

    configure_console_output(interpreter, debug)
    # OI's display.find/find_text post to a legacy remote /point/ API that is
    # never running locally; loop retries on it burn API tokens. Route them
    # at FRIDAY's own vision chain + local OCR instead.
    from .display_patch import install_display_helpers

    try:
        install_display_helpers(interpreter)
    except Exception as display_patch_error:
        _flog_fallback = friday_logger()
        _flog_fallback.warning("display patch failed: %s", display_patch_error)

    # NOTE: the interpreter PACKAGE itself is now patched in place
    # (.venv/.../interpreter/core/computer/display/{display.py,friday_locate.py}),
    # so the model's kernel process inherits the FRIDAY locator on import —
    # no runtime injection needed. The old kernel-payload thread (which
    # spawned a Jupyter kernel at boot and popped a CMD flash / wedged
    # silent tray startups) was removed entirely.
    log_path = setup_file_logging(debug=debug)
    _flog = friday_logger()
    _llm_obj = getattr(interpreter, "llm", None)
    _model_label = getattr(_llm_obj, "model", None) if _llm_obj else None
    if isinstance(_llm_obj, dict):
        _model_label = _llm_obj.get("model", "?")
    _flog.info("starting host=%s port=%s voice=%s debug=%s model=%s", server_host, server_port, voice, debug, _model_label or "?")
    print(f"FRIDAY log file: {log_path}", flush=True)
    interpreter.server.host = server_host
    interpreter.server.port = server_port
    interpreter.context_mode = False 
    interpreter.context_mode = True 

    if voice == False:
        if status_bus is not None:
            status_bus.publish(STATUS_IDLE, "LiveKit server ready")
        interpreter.server.run()
        exit()

    trace_start = time.monotonic()
    server_state = {
        "is_paused": False,
        "pending_input": None,
        "last_wake_time": 0.0,
        "last_wake_text": "",
        "barge_in": False,
        "single_turn_loop": None,
        "pending_memory_response": None,
        "pending_screen_response": None,
        "recent_tts_texts": [],
        "tts_echo_buffer": "",
        "tts_echo_turn_active": False,
        "memory_store": None,
        "hot_window_seconds": _configured_hot_window_seconds(),
        "hot_window_active": False,
        "hot_window_opened_at": 0.0,
        "hot_window_expires_at": 0.0,
        "hot_window_timer": None,
        "hot_window_lock": threading.Lock(),
        "event_loop": None,
        "ambient_buffer": AmbientBuffer(),
        "judge_client": None,
        "pending_router_response": None,
        "schedule_store": _ScheduleStore(),
        "telegram_capture": None,
        "turn_channel": "",  # originating out-channel for /chat ("text"), telegram chat_id, or "" (local)
        "telegram_turn_mute": False,
        "turn_log": None,
        "pending_user_text": None,
        "last_user_text": "",
        "last_progress": time.monotonic(),  # fresh boot; watchdog starts clean
    }
    interpreter.server_state = server_state
    server_state["brain_router"] = BrainRouter(enabled=api_pools.cloud_brain_enabled())

    # Mission schedule store: static HUD calendar -> live FRIDAY calendar.
    _configure_calendar_route(lambda: server_state.get("schedule_store"))

    if getattr(interpreter, "friday_memory_enabled", False) or _automatic_memory_enabled(interpreter):
        try:
            server_state["memory_store"] = MemoryStore()
        except Exception as e:
            # Memory is optional and must never block voice startup.
            print(f"[memory disabled: {e}]")

    if status_bus is not None:
        interpreter.status_bus = status_bus

    def trace(event):
        _flog.debug(event)
        if debug:
            elapsed = time.monotonic() - trace_start
            print(f"[trace +{elapsed:.3f}s] server {event}", flush=True)

    def publish_status(state, detail=""):
        _flog.info("state=%s detail=%s", state, detail if detail else "-")
        if state == STATUS_THINKING and not server_state.get("pulse_active"):
            server_state["pulse_active"] = True
            server_state["pulse_started_at"] = time.monotonic()
        elif state != STATUS_THINKING:
            server_state["pulse_active"] = False
        if status_bus is not None:
            status_bus.publish(state, detail)

    def close_hot_window(reason):
        with server_state["hot_window_lock"]:
            if not server_state["hot_window_active"]:
                return
            server_state["hot_window_active"] = False
            server_state["hot_window_expires_at"] = 0.0
            timer = server_state["hot_window_timer"]
            server_state["hot_window_timer"] = None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        trace(f"hot_window_closed reason={reason}")
        publish_status(STATUS_IDLE, f"Follow-up window closed ({reason})")

    def open_hot_window():
        if server_state["is_paused"]:
            return False
        now = time.monotonic()
        with server_state["hot_window_lock"]:
            server_state["hot_window_active"] = True
            server_state["hot_window_opened_at"] = now
            server_state["hot_window_expires_at"] = now + server_state["hot_window_seconds"]
            timer = threading.Timer(server_state["hot_window_seconds"], close_hot_window, args=("expired",))
            timer.daemon = True
            server_state["hot_window_timer"] = timer
            timer.start()
        trace(f"hot_window_open seconds={server_state['hot_window_seconds']:.1f}")
        publish_status(STATUS_LISTENING, f"Follow-up window open ({server_state['hot_window_seconds']:.1f}s)")
        return True

    def response_is_active():
        respond_thread = getattr(interpreter, "respond_thread", None)
        return respond_thread is not None and respond_thread.is_alive()

    # Ambient PC self-awareness: a quiet daemon samples foreground window +
    # top processes + RAM/CPU on an interval, into memory topic 'pc_state',
    # so FRIDAY knows what Sir runs without being asked.
    try:
        from .self_improve import install_ambient_probe

        install_ambient_probe(server_state, active_check=response_is_active)
    except Exception as ambient_error:
        _flog.info("ambient probe not installed: %s", ambient_error)

    def queue_speech_only(content):
        """Speak text without finalising the turn.

        queue_direct_response() also emits `status: complete`, which is correct
        for a standalone reply but WRONG while a turn is still live: Open
        Interpreter emits its own end/complete afterwards and the turn is
        finalised twice (which also clears the ban on a candidate we just
        deliberately killed). The hard-stop path needs the voice without the
        finalisation.
        """
        text = str(content or "")
        if not text.strip():
            return
        interpreter.output_queue.sync_q.put({
            "role": "assistant",
            "type": "message",
            "content": text,
        })
        interpreter.output_queue.sync_q.put({
            "role": "assistant",
            "type": "message",
            "end": True,
        })

    def queue_direct_response(content, *, record_activity=True):
        """Send deterministic adapter output through the normal response/TTS queue."""
        text = str(content or "")
        if record_activity:
            # Deterministic replies have no LLM turn: the activity journal and
            # the telegram capture need the text handed over explicitly.
            remember_activity(content)
            server_state["last_direct_text"] = text
        # FIX (2026-09-30, second pass): this used to also call
        # _record_turn_output directly, which DOUBLE-RECORDED the text - the
        # message chunk we enqueue below is itself observed by new_output, which
        # records it. That inflated snapshot_chars and duplicated the chat row.
        # The enqueue is the single, correct recording path.
        #
        # Empty text: an empty message chunk is discarded downstream (no chat
        # row, no TTS) and would leave /chat waiting for a reply that never
        # arrives, so return before touching the queue.
        if not text.strip():
            return
        interpreter.output_queue.sync_q.put({
            "role": "assistant",
            "type": "message",
            "content": text,
        })
        interpreter.output_queue.sync_q.put({
            "role": "assistant",
            "type": "message",
            "end": True,
        })
        interpreter.output_queue.sync_q.put({
            "role": "server",
            "type": "status",
            "content": "complete",
        })

    def remember_activity(text):
        """Journal one deterministic action into memory so FRIDAY KNOWS what
        she did ('did you do it?' -> recall answers from her own actions)."""
        content_text = str(text or "")
        try:
            timestamp = datetime.now(timezone.utc).strftime("%H:%M")
            key = f"activity:{timestamp.replace(' ', '_')}_{abs(hash(content_text)) % 99999}"
            if server_state["memory_store"] is not None:
                server_state["memory_store"].remember(
                    key=key, topic="activity", source="activity",
                    content=f"[{timestamp}] FRIDAY: {content_text[:140]}",
                    scope="persistent", confidence=0.9,
                )
        except Exception as error:
            _flog.warning("activity journal failed: %s", error)

    def log_direct(actor, operation, outcome, spoken, *, risk="read_only", **extra):
        """Audit a deterministic adapter reply: what was said and why."""
        log_action(actor=actor, risk=risk, operation=operation, allowed=True,
                   outcome=outcome, spoken=_redact_text(spoken)[:400], **extra)

    interpreter.stt = AudioToTextRecorder(
        model="base.en",
        compute_type="int8",
        spinner=False,
        use_microphone=False,
        enable_realtime_transcription=False,
        handle_buffer_overflow=False,
        min_gap_between_recordings=0.2,
        min_length_of_recording=0.2,
        post_speech_silence_duration=0.6,
        silero_sensitivity=0.6,
        silero_use_onnx=True,
        silero_deactivity_detection=True,
        beam_size=1,
    )
    interpreter.stt.stop()  
    publish_status(STATUS_IDLE, "Voice pipeline ready")

    if not hasattr(interpreter, 'tts'):
        print("Setting TTS provider to default: openai")
        interpreter.tts = "openai"

    if interpreter.tts == "coqui":
        from RealtimeTTS import CoquiEngine
        engine = CoquiEngine()
    elif interpreter.tts == "openai":
        # Cloud-first voice spoken chain: gemini account 1 -> account 2 ->
        # local edge-tts voice (same warm engine contract, raw 24k PCM).
        from .gemini_tts import CloudFirstTTSEngine

        engine = CloudFirstTTSEngine()
    elif interpreter.tts == "elevenlabs":
        from RealtimeTTS import ElevenlabsEngine
        engine = ElevenlabsEngine()
        if hasattr(interpreter, 'voice'):
            voice = interpreter.voice
        else:
            voice = "Will"
        engine.set_voice(voice)
    else:
        raise ValueError(f"Unsupported TTS engine: {interpreter.tts}")
    interpreter.tts = TextToAudioStream(engine)

    reminder_worker = None

    from pathlib import Path as _Path

    desktop_ear = DesktopEar().start()
    social_guard = SocialGuard().start()
    server_state["desktop_ear"] = desktop_ear
    server_state["social_guard"] = social_guard
    server_state["focus"] = {"title": "", "exe": "", "since": "", "editor": False}

    def _notes_preview():
        try:
            notes_path = _Path(__file__).resolve().parents[2] / "memory" / "project_notes.md"
            if notes_path.is_file():
                tail = "".join(notes_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-14:])
                _flog.info("project notes context while editor focused:\n%s", _redact_text(tail)[:1600])
        except Exception:
            pass

    focus_tracker = FocusTracker(server_state, notes_preview_callable=_notes_preview).start()
    server_state["focus_tracker"] = focus_tracker

    def _quiet_speak(message):
        if social_guard.active():
            _flog.info("proactive: call is active, staying silent")
            return
        if not response_is_active():
            queue_direct_response(message)

    welcome_back = WelcomeBackMonitor(
        _quiet_speak,
        software_root=_Path(__file__).resolve().parents[2],
        flog=_flog,
    ).start()
    interpreter.welcome_back = welcome_back

    _flog.info("background systems: ear=%s guard=%s focus=%s welcome=%s",
               desktop_ear.active, social_guard.active(), bool(focus_tracker), bool(welcome_back))

    def handle_special_command(content, actor):
        """Deterministic adapters that preempt the model: ear, reminders."""
        candidate = str(content or "").strip()
        ear_reply = handle_ear_command(candidate, desktop_ear)
        if ear_reply is not None:
            log_direct(actor, "desktop_ear", "ok", ear_reply)
            _flog.info("ear: %s", _redact_text(ear_reply))
            return {"response": ear_reply}
        if _reminders_enabled(interpreter) and server_state.get("reminder_store") is not None:
            parsed = parse_reminder(candidate)
            if parsed is not None:
                due_at, title = parsed
                try:
                    created = server_state["reminder_store"].create(title, due_at)
                except Exception as error:
                    _flog.warning("reminder create failed: %s", error)
                    return {"response": "I couldn't set that reminder right now, Sir."}
                delta = (due_at - datetime.now(timezone.utc)).total_seconds()
                if delta < 3600:
                    when = f"in {max(1, int(delta // 60))} minutes"
                elif delta < 86400:
                    when = f"in {max(1, int(delta // 3600))} hours"
                else:
                    when = f"at {due_at.astimezone().strftime('%H:%M on %B %d')}"
                reply = f"Got it, Sir. I'll remind you {when} to {title}."
                log_direct(actor, "reminder", "ok", reply, title=title)
                return {"response": reply}
        return None

    interpreter.play_audio = False
    interpreter.audio_chunks = []
    tts_audio_seen = False
    tool_code_parts = []
    tool_counter = {"n": 0}
    SERVER_TOOL_CAP = [
        float(os.getenv("FRIDAY_TOOL_CAP", "12"))
    ]
    # Hard ceiling: the soft nudge above is advisory (models ignore it). If a
    # turn keeps dry-running tools past this count, kill the agent turn outright
    # instead of letting it loop for dozens of executions (Roblox loop: 91 runs).
    HARD_TOOL_CAP = int(float(os.getenv("FRIDAY_HARD_TOOL_CAP", "24")))

    # Runaway-script guard. The tool CAP above only counts code blocks that emit
    # a `start` event. A single script with an unbounded loop never ends, never
    # increments that counter, and therefore defeats the cap entirely - the turn
    # hangs forever and FRIDAY never replies. These two bounds catch it:
    # a cap on console events, and a wall-clock ceiling for the whole turn.
    TOOL_FLOOD_EVENT_CAP = int(float(os.getenv("FRIDAY_TOOL_FLOOD_CAP", "300")))
    TOOL_WALL_CLOCK_LIMIT = float(os.getenv("FRIDAY_TURN_WALL_CLOCK", "240"))
    # Per-turn guard state (reset for every turn).
    turn_guard = {"started": time.monotonic(), "console_events": 0, "flood_stopped": False}

    # Pure conversational openers. When the user only greets/chats, the first
    # text reply IS the answer — the loop-engine merge would just echo it back.
    # Task/action requests are NOT openers, so they keep the one-merge nudge
    # (lets a polite "ok, let me …" first line rebound into an actual tool call).
    _chat_opener_re = re.compile(
        r"""^\W{0,16}(hi\b|hello\b|hey\b|yo\b|howdy\b|greetings\b|wassup\b|"""
        r"""sup\b|good\s+(morning|afternoon|evening|day|night)\b|"""
        r"""thanks\b|thank\s+you\b|thanks\s+a\s+lot\b|nice\s+to\s+meet\b|"""
        r"""how\s+are\s+(you|y'all|ya)\b|how'?s\s+it\s+going\b|how'?s\s+your\s+day\b|"""
        r"""say\s+(hi|hello|hey)\b|who\s+are\s+you\b|are\s+you\s+(there|awake)\b|"""
        r"""what'?s\s+up\b|what\s+is\s+up\b|you?\s+there\b|wake\s+up\b)""",
        re.IGNORECASE | re.VERBOSE,
    )

    def _chat_opener(user_text):
        if not isinstance(user_text, str):
            return False
        return bool(_chat_opener_re.match(user_text.strip()))

    def _strip_user_request_prefix(user_text):
        # dispatch_text may prefix memory context: "<ctx>\n\nUser request:\n<text>".
        marker = "\n\nUser request:\n"
        if isinstance(user_text, str) and marker in user_text:
            return user_text.split(marker, 1)[1]
        return user_text or ""

    if not getattr(interpreter.llm, "_friday_trace_wrapped", False):
        original_completions = interpreter.llm.completions
        # Per-turn guard: OI's loop-message engine (respond.py else-branch)
        # re-invokes the LLM when the last message is plain assistant text —
        # querying a model that never emits a loop-breaker phrase spins
        # ~50 identical requests. FRIDAY (a) ends a plain-text reply immediately
        # when no tool has run yet (the first text answer IS the answer — no
        # duplicative merge), and (b) force-breaks any text-only spin that
        # persists after tool activity. Tool steps have computer:console last,
        # so they never count against the cap.
        llm_loop_guard = {"calls": 0}
        LOOP_GUARD_CAP = int(float(os.getenv("FRIDAY_LLM_LOOP_GUARD", "2")))

        def traced_completions(**params):
            trace("llm_request_start")
            # qwen3 thinking mode is ON by default in Ollama and, when allowed,
            # adds tens of seconds of silent reasoning to every trivial reply
            # (measured: ~38s vs ~2.7s on the real persona prompt). Enforce
            # think=false here at the FRIDAY-owned chokepoint so it survives
            # OI/litellm version updates (llm.py:435 only lives in some copies).
            if isinstance(params.get("model"), str) and params["model"].split("/")[-1].startswith("qwen3"):
                params["think"] = False
                trace("llm_brain_think_off")
            _last = interpreter.messages[-1] if interpreter.messages else {}
            if _last.get("role") == "user":
                llm_loop_guard["calls"] = 0
                tool_counter["n"] = 0  # per-turn tool budget (fixes lifetime accumulation)
                # Reset the runaway-script guard with the rest of the per-turn
                # budget, so one bad turn cannot poison the next one.
                turn_guard["started"] = time.monotonic()
                turn_guard["console_events"] = 0
                turn_guard["flood_stopped"] = False
            llm_loop_guard["calls"] += 1
            _assistant_text = _last.get("role") == "assistant" and _last.get("type") in (None, "message")
            if _assistant_text and llm_loop_guard["calls"] >= 2:
                # respond() wants to re-merge a plain-text assistant reply. Skip
                # the merge when NO tool has run yet AND the user's request was a
                # conversational opener: the first text reply IS the answer, so a
                # merge only dumps a duplicative greeting. Task requests keep the
                # merge so a polite first line can still rebound into a tool call.
                # Tool turns always have computer:console last, so they never hit
                # this branch.
                has_tool_activity = False
                _user_text = ""
                for m in reversed(interpreter.messages):
                    if m.get("role") == "user":
                        _user_text = m.get("content") or ""
                        break
                    if m.get("role") in ("computer", "tool"):
                        has_tool_activity = True
                        break
                if not has_tool_activity and _chat_opener(_strip_user_request_prefix(_user_text)):
                    _flog.info(
                        "llm loop guard: conversational reply, no tool ran — ending turn (calls=%d)",
                        llm_loop_guard["calls"],
                    )
                    trace("llm_loop_guard_plain_end")
                    try:
                        if isinstance(_last.get("content"), str):
                            interpreter.messages[-1]["content"] = (
                                _last["content"] + "\n\nThe task is done."
                            )
                    except Exception:
                        pass
                    return
            if (
                llm_loop_guard["calls"] > LOOP_GUARD_CAP
                and _assistant_text
            ):
                _flog.warning(
                    "llm loop guard: %d text-only llm calls this turn; forcing end",
                    llm_loop_guard["calls"],
                )
                trace("llm_loop_guard_end")
                try:
                    if isinstance(_last.get("content"), str):
                        interpreter.messages[-1]["content"] = (
                            _last["content"] + "\n\nThe task is done."
                        )
                except Exception:
                    pass
                return
            _flog.info(
                "llm call: loop=%s msgs=%d last=%s:%s model=%s tools=%s tool_choice=%s think=%s guard_calls=%d",
                getattr(interpreter, "loop", "?"),
                len(interpreter.messages),
                _last.get("role", "?"),
                _last.get("type", "?"),
                str(getattr(interpreter.llm, "model", "?")),
                bool(params.get("tools")),
                params.get("tool_choice"),
                params.get("think", "UNSET"),
                llm_loop_guard["calls"],
            )
            api_base = str(getattr(interpreter.llm, "api_base", "") or "")
            if "generativelanguage" in api_base and params.get("messages"):
                # Gemini 3 requires thought_signature round-trips on tool-call
                # history. Tool calls generated by a DIFFERENT provider
                # (Groq gpt-oss) carry no signature, so Google hard-400s the
                # whole turn ("Function call is missing a thought_signature",
                # position N). Dropping foreign tool-call/tool-result rows
                # makes Gemini replay the request fresh from the user message.
                try:
                    filtered = [
                        m for m in params["messages"]
                        if not (isinstance(m, dict) and (
                            (m.get("role") == "tool")
                            or (m.get("role") == "assistant" and m.get("tool_calls"))
                        ))
                    ]
                    if len(filtered) != len(params["messages"]) and any(
                        isinstance(m, dict) and m.get("role") == "user"
                        for m in filtered
                    ):
                        _flog.info(
                            "gemini hop sanitize: dropped %s foreign tool-history rows",
                            len(params["messages"]) - len(filtered),
                        )
                        params["messages"] = filtered
                except Exception:
                    pass
            if params.get("tools") and not params.get("tool_choice"):
                # OI sets tools but never forwards tool_choice. Providers like
                # Groq's gpt-oss default an absent tool_choice to "none" and
                # abort the stream when the model then emits a tool call.
                params["tool_choice"] = "auto"
            first_chunk = True
            thinking_logged = False
            try:
                for chunk in original_completions(**params):
                    if first_chunk:
                        first_chunk = False
                        trace("llm_first_chunk")
                    if not thinking_logged:
                        try:
                            choice = chunk["choices"][0]
                            data = choice.get("message") or choice.get("delta") or {}
                            reason = data.get("reasoning_content") if isinstance(data, dict) else None
                            if reason and isinstance(reason, str) and reason.strip():
                                _flog.info("thinking: %s", _redact_text(reason)[:600])
                                trace("llm_thinking")
                                thinking_logged = True
                        except Exception:
                            pass
                    yield chunk
            except Exception as tool_hallucination_error:
                # gpt-oss sometimes invents tools that are not in the request
                # ("silent_search<|channel|>commentary"), aborting mid-stream.
                # Retry once in PLAIN TEXT mode (markdown code blocks) so the
                # turn still completes instead of erroring on the phone.
                error_text = str(tool_hallucination_error)
                if "not in request.tools" not in error_text and "Tool call validation" not in error_text:
                    raise
                _flog.warning(
                    "tool hallucination midstream (%.80s); retrying without tools",
                    _redact_text(error_text),
                )
                retry_params = {k: v for k, v in params.items() if k not in ("tools", "tool_choice")}
                yield from original_completions(**retry_params)

        interpreter.llm.completions = traced_completions
        interpreter.llm._friday_trace_wrapped = True

    old_input = interpreter.input

    def chat_append(role, content, status="complete"):
        text = str(content or "")
        if _looks_like_tool_json(text):
            # Chunk reality check (forensics confirmed in friday.log): OI's
            # tool-calling path streams FRIDAY's spoken words AS
            # {"code": "..."} payloads — including her own confirmation
            # lines. Instead of dropping, unwrap them: the feed must show
            # exactly what TTS speaks.
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError) as probe_error:
                _flog.info(
                    "chat_append guard probe on role=%s: json.loads raised %s - keeping raw text",
                    role, type(probe_error).__name__,
                )
            else:
                if isinstance(parsed, dict):
                    code_text = parsed.get("code")
                    if isinstance(code_text, str) and code_text.strip():
                        text = code_text
                    elif any(k in parsed for k in ("tool_calls", "function")):
                        # Genuine tool metadata, nothing readable inside.
                        _flog.info("chat_append skipped tool metadata role=%s", role)
                        return
        if status_bus is None or not hasattr(status_bus, "chat"):
            _flog.warning("chat_append skipped role=%s (status_bus=%s)", role, type(status_bus).__name__ if status_bus else None)
            return
        buffer = status_bus.chat
        if role == "assistant" and status == "streaming":
            buffer.merge_stream(text)
        else:
            buffer.append(role, text, status=status)
        _flog.info(
            "chat_append role=%s chars=%s buffer_rows=%s bus=%s",
            role, len(text), len(buffer.snapshot()), id(status_bus),
        )

    def chat_upsert_tool(content, key, status="complete"):
        """Show/replace ONE tool row (e.g. the script FRIDAY just wrote).

        The conversation stream previously appended a fresh row per fragment,
        so a script streamed in many chunks was either invisible or spammed the
        feed, and Sir could not see WHAT script was actually running - only
        "OUT #1: python.exe". This rewrites a single keyed row in place so the
        newest full text is always on screen.
        """
        if status_bus is None or not hasattr(status_bus, "chat"):
            return
        try:
            status_bus.chat.upsert_tool(content, status=status, key=key)
            _flog.info(
                "chat_upsert_tool key=%s status=%s chars=%s",
                key, status, len(str(content or "")),
            )
        except Exception as upsert_error:
            _flog.info("chat_upsert_tool skipped: %s", type(upsert_error).__name__)

    def chat_finalize(snapshot_text=""):
        if status_bus is None or not hasattr(status_bus, "chat"):
            _flog.warning("chat_finalize skipped (status_bus=%s)", type(status_bus).__name__ if status_bus else None)
            return
        status_bus.chat.finalize_streaming()
        added = None
        if snapshot_text:
            # The turn snapshot is the source of truth: guarantee the feed
            # shows the full completed reply even if the streaming chunks
            # could not be classified.
            added = status_bus.chat.ensure_complete(snapshot_text)
            if added is None:
                chat_append("assistant", snapshot_text)
                added = "forced"
        _flog.info(
            "chat_finalize snapshot_chars=%s ensure=%s buffer_rows=%s roles=%s chat_id=%s",
            len(snapshot_text),
            str(added is not None).lower() if snapshot_text else "skipped",
            len(status_bus.chat.snapshot()),
            [m.get("role") for m in status_bus.chat.snapshot()][-6:],
            id(status_bus),
        )

    # Questions whose answer IS the current live state (foreground window,
    # what's open/playing right now). For these the stale `activity` journal
    # (action timestamps like "[10:36] Spotify is already open") must NOT leak
    # into the memory context — it reads like current state but is history.
    _LIVE_STATE_QUERY_RE = re.compile(
        r"\b(?:user|sir|owner|sandy|boss|bro)\s+(?:currently|right\s+now|now|just)?\s*"
        r"(?:doing|up\s+to|working\s+on|watching|playing|listening\s+to|using)\b"
        r"|\bwhat\s+am\s+i\s+(?:doing|watching|listening\s+to|looking\s+at)\b"
        r"|\bhappening\s+on\s+(?:the\s+)?(?:pc|computer|screen)\b"
        r"|\b(?:going\s+on|on\s+my\s+screen|what\s+do\s+you\s+see|"
        r"(?:app|window|program)\s+(?:is|has|am)\s+(?:focused|active))\b",
        re.IGNORECASE,
    )

    async def dispatch_text(text, *, from_telegram=False, reply_channel=""):
        """Route webview/telegram text through the same owned interpreter turn as voice."""
        request = str(text or "").strip()
        if not request:            return {"accepted": False, "status": "invalid", "message": "Enter a message first."}
        if server_state["is_paused"]:
            return {"accepted": False, "status": "paused", "message": "Voice mode is paused."}
        content = request
        # Remember where this turn's reply must go even after telegram_capture
        # is cleared by the delivery path (so a permanent failure can still
        # reach the originator). /chat sentinel -> "text"; telegram handler set
        # telegram_capture already; local text/voice -> "".
        server_state["turn_channel"] = (
            "text"
            if reply_channel == "text"
            else str((server_state.get("telegram_capture") or {}).get("chat_id") or "")
        )
        if reply_channel == "text":
            # No-voice text-command channel (/chat): deliver the final answer
            # into the text_reply_queue instead of speaking it here. Reuses the
            # telegram capture slot as the delivery hook with a sentinel chat id.
            server_state["telegram_capture"] = {"chat_id": TEXT_CHANNEL_CHAT_ID}
            server_state["telegram_turn_mute"] = True
        chat_append("user", content)
        publish_status(STATUS_THINKING, "FRIDAY is working")
        server_state["last_user_text"] = content
        log_action(actor="text", risk="read_only", operation="user_command",
                   allowed=True, text=_redact_text(content))
        _flog.info("typed: %s", _redact_text(content))

        special = handle_special_command(content, actor="text")
        if special is not None:
            if response_is_active():
                server_state["pending_router_response"] = special["response"]
            else:
                queue_direct_response(special["response"])
            return {"accepted": True, "status": "queued"}

        command_result = route_command(content, actor="text")
        if command_result is not None:
            if response_is_active():
                server_state["pending_router_response"] = command_result["response"]
            else:
                queue_direct_response(command_result["response"])
            return {"accepted": True, "status": "queued"}

        memory_command = parse_memory_command(content)
        if memory_command is not None:
            if server_state["memory_store"] is None:
                response = "Memory is unavailable right now."
            else:
                try:
                    response = format_memory_response(memory_command, server_state["memory_store"])
                except Exception:
                    response = "I couldn’t update memory right now."
            log_direct("text", "memory", "ok", response, command=memory_command)
            if response_is_active():
                server_state["pending_memory_response"] = response
            else:
                queue_direct_response(response)
            return {"accepted": True, "status": "queued"}

        if _automatic_memory_enabled(interpreter) and server_state["memory_store"] is not None:
            try:
                extract_and_remember_automatic(content, server_state["memory_store"])
            except Exception:
                pass
        if not from_telegram:
            # A LOCAL user (typed text or voice) takes the mute off; telegram
            # chains stay silent at the PC until Sir interacts locally.
            server_state["telegram_turn_mute"] = False
        memory_context = ""
        if server_state["memory_store"] is not None:
            memory_context = build_memory_context(
                content,
                server_state["memory_store"],
                top_n=3,
                max_chars=1600,
                exclude_topics=("activity",) if _LIVE_STATE_QUERY_RE.search(content) else (),
            )
        if is_screen_request(content):
            server_state["active_vision_pool"] = "active"
            try:
                response = screen_response(content)
            finally:
                server_state["active_vision_pool"] = None
            log_direct("text", "screen", "ok", "Screen captured.", chars=len(response))
            if response_is_active():
                server_state["pending_screen_response"] = response
            else:
                queue_direct_response(response)
            return {"accepted": True, "status": "queued"}
        if memory_context:
            content = memory_context + "\n\nUser request:\n" + content
        last_shot = server_state.get("last_screen_shot")
        if last_shot and _SCREENSHOT_REFERENCE_RE.search(content):
            # Follow-up ("do you see your own screenshot that you sent?") must
            # NOT plead blind: FRIDAY DID capture it and DID see it — the
            # vision read gives the brain the real content to answer from.
            content = (
                "[Context: the screenshot I just captured and sent you showed - "
                + str(last_shot.get("description") or "(the vision read is unavailable)")
                + " You DID capture it and you saw it. Answer truthfully, "
                "grounded in that description.]\n\n"
                + content
            )
        if from_telegram:
            # Make the source unmistakable: the brain must answer as a TEXT
            # bubble on Sir's phone — no local voice, no opening/showing
            # anything on the PC, no file artifacts unless he asks for one.
            content = (
                "[Sir is talking to you on Telegram — your reply goes to his "
                "phone as a chat text. Answer in one short bubble. Do NOT "
                "open, show, or play anything on the PC, do NOT activate "
                "local voice, and do not send a file/screenshot unless he "
                "explicitly asked for one. When he says 'show me', the "
                "screenshot itself is handled for you; just acknowledge.]\n\n"
                + content
            )
        if response_is_active():
            server_state["pending_user_text"] = request
            server_state["pending_input"] = {"role": "user", "type": "message", "content": content}
            return {"accepted": True, "status": "queued"}
        _begin_single_turn(interpreter, server_state, user=request, source="text")
        server_state["brain_router"].apply_for(request, interpreter)
        _mark_turn_dispatch(server_state, interpreter, user=request, content=content, source="text")
        await old_input({"role": "user", "type": "message", "start": True})
        await old_input({"role": "user", "type": "message", "content": content})
        await old_input({"role": "user", "type": "message", "end": True})
        return {"accepted": True, "status": "queued"}

    def submit_text(text):
        loop = server_state.get("event_loop")
        if loop is None or not loop.is_running():
            return {"accepted": False, "status": "unavailable", "message": "FRIDAY server is not ready yet."}
        future = asyncio.run_coroutine_threadsafe(dispatch_text(text), loop)
        try:
            return future.result(timeout=2.0)
        except TimeoutError:
            return {"accepted": True, "status": "queued"}
        except Exception as error:
            return {"accepted": False, "status": "error", "message": "Unable to submit that message."}

    interpreter.submit_text = submit_text

    # --- Telegram remote: same brain, phone transport -----------------------
    from .remote_telegram import TelegramRemoteAdapter

    server_state["telegram_capture"] = None

    async def telegram_command_handler(text, chat_id, message=None):
        """One Telegram text = one FRIDAY turn through the SAME brain/tools
        the voice mode uses: deterministic router first, then Open Interpreter.

        Phone turns are SILENT at the PC (text-only answer) unless Sir asks
        her to speak aloud ("Say \"what's good\"", "could you talk and say...").
        """
        lowered = (text or "").lower().strip()
        # Screenshot BACK to the phone only for true capture requests —
        # never when Sir merely references screenshots ("you don't need
        # to take a screenshot for my notifications"). Matches explicit
        # "screenshot" asks AND bare "show me" / "show me your screen" /
        # "show me what you see" — on the phone that ALWAYS means a photo.
        capture_phrase = re.search(
            r"\bscreenshot\b|"
            r"\bscreenshot\s+of\s+(?:the\s+|your\s+)screen\b|"
            r"\b(?:can\s+you\s+)?(?:show|send|share)\s+me\b"
            r"(?:\s+(?:your\s+|my\s+|the\s+)?"
            r"(?:screen|view|camera|desktop|display|what\s+you\s+see))?"
            r"(?:\s*,?\s*(?:please|friday|sir|bro|boss))?"
            r"\s*[.!?]*\s*$",
            lowered,
        )
        # "screenshot" as a bare WORD is only actionable when the message is a
        # request, not an interrogative ("what would a screenshot show",
        # "tell me about screenshots"). Negations and the anchored
        # show/send/share branch are already handled by the two guards below.
        interrogative_leaning = re.match(
            r"(?:what|why|who|when|how|would|does|do|is|are|tell|explain)\b",
            lowered,
        )
        if (
            capture_phrase
            and not re.search(r"don'?t|no\s+need|never|without|skip", lowered)
            and not interrogative_leaning
        ):
            from .screen_understanding import capture_screen_jpeg

            try:
                jpeg = capture_screen_jpeg()
            except Exception as error:
                # Screen capture fails while a Windows "Yes/No" elevation or
                # lock-screen dialog owns the desktop. Say so plainly instead
                # of letting the generic handler_error mask the real cause.
                _flog.warning("telegram screenshot capture failed: %s", _redact_text(str(error)))
                return (
                    "Sir, a Windows Yes/No (or lock screen) prompt is covering my "
                    "view, so I couldn't capture the screen. Answer that prompt and "
                    "tell me to take the screenshot again."
                )
            # AI-AUTHORED content: the capture is only an infrastructure
            # primitive — the vision chain READS the real image and the brain
            # (qwen3:8b) composes the caption from what it truly sees. Never a
            # canned "Fresh screenshot, Sir."; the AI decides what is shown,
            # and the vision read is persisted so follow-ups ("do you see your
            # own screenshot you sent?") are answered with real context.
            try:
                server_state["active_vision_pool"] = "active"
                caption, vision = author_screen_photo(jpeg, text)
            finally:
                server_state["active_vision_pool"] = None
            if vision:
                server_state["last_screen_shot"] = {
                    "at": time.time(),
                    "description": _redact_text(vision)[:1200],
                    "caption": _redact_text(caption or "")[:600],
                }
            try:
                await telegram_adapter.send_photo(
                    chat_id, jpeg, caption=(caption or "Fresh screen capture for you, Sir.")[:1024]
                )
            except Exception as error:
                _flog.warning("telegram screenshot send failed: %s", _redact_text(str(error)))
                return "Sir, I captured the screen but couldn't upload it to Telegram. Check the PC and ask again if needed."
            return None  # photo + AI-authored caption already delivered
        speak_requested = bool(re.search(
            # anywhere in the message: 'Say "how are you"', 'Perfect, say ...',
            # 'say it out loud', 'on the headset' — position no longer matters
            r"\bsay\b|\bspeak\b|\bwhisper\b|\bannounce\b|\bshout\b|\brepeat\b|"
            r"\bout\s+loud\b|\b(?:on|through|via)\s+the\s+(?:headset|speakers?)\b|\baloud\b",
            lowered,
        ))
        server_state["telegram_turn_mute"] = not speak_requested
        server_state["telegram_err_notified"] = False  # fresh turn cycle
        server_state["telegram_capture"] = {"chat_id": chat_id}
        result = await dispatch_text(text, from_telegram=True)
        if isinstance(result, dict) and result.get("response"):
            server_state["telegram_capture"] = None
            server_state["telegram_turn_mute"] = False
            return result["response"]
        return "On it, Sir — give me a few seconds and I'll text you back."

    async def telegram_photo_handler(jpeg_bytes, question, chat_id):
        from .screen_understanding import describe_image_bytes
        return describe_image_bytes(jpeg_bytes, question)

    telegram_adapter = TelegramRemoteAdapter(
        telegram_command_handler,
        config=load_telegram_config(),
    )
    telegram_adapter.photo_handler = telegram_photo_handler
    server_state["telegram_adapter"] = telegram_adapter

    async def _deliver_failure_notice(state, text):
        """Route the honest failure note to the turn's ORIGINATING channel.
        telegram_capture is already cleared by the delivery path before the
        permanent-failure block runs, so 'turn_channel' (set at dispatch) is
        the source of truth here."""
        channel = state.get("turn_channel") or ""
        if not channel:
            return
        state["turn_channel"] = ""
        if channel == "text":
            state.setdefault("text_reply_queue", asyncio.Queue())
            await state["text_reply_queue"].put(text)
        else:
            try:
                await telegram_adapter.send_text(channel, text)
            except Exception as error:
                _flog.warning("telegram failure notice failed: %s", error)

    async def start_telegram_remote():
        if telegram_adapter.enabled:
            started = await telegram_adapter.start()
            if started:
                await telegram_adapter.announce("its on")
        else:
            await telegram_adapter.start()  # setup mode: binds the owner on first message
            if telegram_adapter.status()["running"]:
                _flog.info("telegram setup mode: waiting for owner account to bind")

    async def mark_event_loop():
        server_state["event_loop"] = asyncio.get_running_loop()
        publish_status(STATUS_IDLE, "Voice pipeline ready")
        await start_telegram_remote()

    try:
        interpreter.server.app.add_event_handler("startup", mark_event_loop)
    except AttributeError:
        pass

    async def new_input(self, chunk):
        server_state["event_loop"] = asyncio.get_running_loop()
        server_state["last_progress"] = time.monotonic()
        await asyncio.sleep(0)
        try:
            if isinstance(chunk, bytes):
                self.stt.feed_audio(chunk)
                self.audio_chunks.append(chunk)
            elif isinstance(chunk, dict):
                if "start" in chunk:
                    trace("stt_start")
                    publish_status(STATUS_LISTENING, "Listening for a command")
                    self.stt.start()
                    self.audio_chunks = []

                    if response_is_active() and self.tts.is_playing():
                        # Genuine barge-in: user speaks while TTS is actually
                        # audible. Stop the in-flight TTS and let the rest of
                        # the response drain without speaking.
                        server_state["barge_in"] = True
                        self.tts.stop()
                    
                if "end" in chunk:
                    trace("stt_stop")
                    publish_status(STATUS_THINKING, "Transcribing and routing")
                    self.stt.stop()
                    content = ""
                    if self.audio_chunks and api_pools.cloud_stt_enabled():
                        server_state["active_stt_pool"] = "active"
                        try:
                            wav_bytes = pcm_to_wav(b"".join(self.audio_chunks))
                            content, stt_provider, stt_error = transcribe_wav(wav_bytes)
                        finally:
                            server_state["active_stt_pool"] = None
                        if content:
                            trace(f"stt_cloud provider={stt_provider}")
                            _flog.info("cloud stt ok provider=%s", stt_provider)
                        elif stt_error:
                            _flog.info("cloud stt failed: %s", stt_error)
                    if not content:
                        content = self.stt.text()
                    trace(f"stt_final text={content!r}")
                    _flog.info("heard: %s", _redact_text(content.strip()))

                    print("\r" + " " * 100 + "\r", end="", flush=True)

                    if content.strip() == "":
                        publish_status(STATUS_IDLE, "Standing by")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    text_lower = content.strip().lower()
                    text_clean_lower = text_lower.rstrip(",.?! ")

                    if server_state["is_paused"]:
                        publish_status(STATUS_IDLE, "Voice mode paused")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return
                    temp_text = text_clean_lower
                    for w in ["friday", "frida", "fry day", "fryday", "fri-day", "friyay", "fire day", "fri", "day", "fry"]:
                        temp_text = temp_text.replace(w, "")
                    temp_text = temp_text.strip(",.?! ")

                    if temp_text == "pause":
                        server_state["is_paused"] = True
                        close_hot_window("paused")
                        publish_status(STATUS_IDLE, "Voice mode paused")
                        print("Voice mode paused. Press [ENTER] in the server terminal to resume listening.")
                        self.tts.feed("Voice mode paused.")
                        self.tts.play_async(on_audio_chunk=self.on_tts_chunk, sentence_fragment_delimiters=".?!;,\n…)]}", minimum_sentence_length=1)
                        await old_input({"role": "user", "type": "message", "content": ""})
                        await old_input({"role": "user", "type": "message", "end": True})
                        self.output_queue.sync_q.put({"voice_paused": True, "end": True})
                        return

                    hallucination_reason = reject_reason(text_lower)
                    if hallucination_reason is not None:
                        trace(f"wake_rejected reason=hallucination detail={hallucination_reason}")
                        publish_status(STATUS_IDLE, "Standing by")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    hot_window = _hot_window_is_active(server_state)

                    matched_word = _find_wake_word(text_lower)
                    found_wake = bool(matched_word)
                    trace(
                        f"wake_filter final={text_lower!r} hot_window={hot_window} "
                        f"matched={matched_word or 'none'}"
                    )
                    
                    if not hot_window and not found_wake:
                        trace("wake_rejected reason=final_text_missing_wake")
                        if not _is_recent_tts_echo(text_clean_lower, server_state, self.tts.is_playing()):
                            server_state["ambient_buffer"].add(text_clean_lower)
                        publish_status(STATUS_IDLE, "Standing by")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    if hot_window:
                        clean_content = text_lower
                        if found_wake:
                            clean_content = _strip_wake_word(clean_content, matched_word)
                    else:
                        clean_content = _strip_wake_word(text_lower, matched_word)
                    clean_content = clean_content.strip(",.?! ")
                    
                    if clean_content.strip() == "":
                        publish_status(STATUS_IDLE, "Ready for a command")
                        print("Ready for command...")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    if hot_window:
                        hot_candidate = _hot_window_candidate(
                            clean_content, server_state, self.tts.is_playing()
                        )
                        if hot_candidate is None:
                            publish_status(STATUS_IDLE, "Ignored hot-window input")
                            self.output_queue.sync_q.put({"ignored": True, "end": True})
                            return
                        clean_content = hot_candidate
                        # Each accepted follow-up gets a fresh bounded window.
                        open_hot_window()

                    if _is_recent_tts_echo(clean_content, server_state, self.tts.is_playing()):
                        trace("tts_echo_suppressed")
                        publish_status(STATUS_IDLE, "Ignored assistant echo")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    now = time.time()
                    last_t = server_state["last_wake_time"]
                    last_w = server_state["last_wake_text"]
                    if clean_content == last_w and (now - last_t) < 3.0:
                        publish_status(STATUS_IDLE, "Ignored duplicate request")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    print("Wake Word Detected!")
                    print(">", clean_content)
                    _play_chirp("wake")
                    chat_append("user", clean_content)
                    server_state["last_user_text"] = clean_content
                    _flog.info("accepted voice: %s", _redact_text(clean_content))
                    log_action(actor="voice", risk="read_only", operation="user_command",
                               allowed=True, text=_redact_text(clean_content))
                    trace("wake_accepted")
                    publish_status(STATUS_THINKING, "FRIDAY is working")

                    # "...that's enough / stop" only means "be quiet" while
                    # FRIDAY is mid-response. Silence instantly, swallow the
                    # rest of the in-flight turn, stay in standby.
                    if _STOP_PHRASE_RE.match(clean_content or "") and (
                        response_is_active() or self.tts.is_playing()
                    ):
                        self.tts.stop()
                        server_state["barge_in"] = True
                        _play_chirp("tool_complete")
                        _flog.info("stop phrase honored: response silenced")
                        log_action(actor="voice", risk="read_only",
                                   operation="conversation_stop", allowed=True,
                                   spoken="Silenced response on user request")
                        publish_status(STATUS_IDLE, "Standing by")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        return

                    if judge_enabled() and not hot_window:
                        ambient = server_state["ambient_buffer"].recent()
                        if ambient:
                            judge_started = time.monotonic()
                            client = server_state.get("judge_client")
                            if client is None:
                                client = JudgeClient()
                                server_state["judge_client"] = client
                            judge_result = await asyncio.to_thread(
                                _intent_judge, clean_content, ambient,
                                client=client, enabled=True,
                            )
                            judge_latency = time.monotonic() - judge_started
                            log_action(actor="voice", risk="read_only",
                                       operation="intent_judge", allowed=True,
                                       outcome="ok" if judge_result.get("directed") else "ignored",
                                       latency_ms=round(judge_latency * 1000),
                                       model=judge_result.get("model"))
                            if not judge_result.get("directed"):
                                trace("wake_rejected reason=judge_not_directed")
                                publish_status(STATUS_IDLE, "Standing by")
                                self.output_queue.sync_q.put({"ignored": True, "end": True})
                                return
                            new_query = judge_result.get("query", "")
                            if new_query and new_query != clean_content:
                                clean_content = new_query
                                trace(f"intent_judge_rewrote query={clean_content!r}")

                    special = handle_special_command(clean_content, actor="voice")
                    if special is not None:
                        trace("special_adapter handled")
                        if hot_window:
                            close_hot_window("special handled")
                        if response_is_active():
                            server_state["pending_router_response"] = special["response"]
                        else:
                            queue_direct_response(special["response"])
                        return

                    command_result = route_command(clean_content, actor="voice")
                    if command_result is not None:
                        trace(f"command_router action={command_result['action']}")
                        if hot_window:
                            close_hot_window("router handled")
                        if response_is_active():
                            server_state["pending_router_response"] = command_result["response"]
                        else:
                            queue_direct_response(command_result["response"])
                        return

                    server_state["last_wake_time"] = now
                    server_state["last_wake_text"] = clean_content

                    memory_command = parse_memory_command(clean_content)
                    if memory_command is not None:
                        if server_state["memory_store"] is None:
                            memory_response = "Memory is unavailable right now."
                        else:
                            try:
                                memory_response = format_memory_response(
                                    memory_command, server_state["memory_store"]
                                )
                            except Exception:
                                memory_response = "I couldn’t update memory right now."
                        log_direct("voice", "memory", "ok", memory_response, command=memory_command)
                        if hot_window:
                            close_hot_window("follow-up accepted")
                        if response_is_active():
                            server_state["pending_input"] = None
                            server_state["pending_memory_response"] = memory_response
                        else:
                            queue_direct_response(memory_response)
                        return

                    if _automatic_memory_enabled(self) and server_state["memory_store"] is not None:
                        try:
                            extract_and_remember_automatic(
                                clean_content, server_state["memory_store"]
                            )
                        except Exception as error:
                            # Learning is advisory; never delay or break the response.
                            trace(f"automatic_memory_skipped error={error}")

                    content = clean_content
                    memory_context = ""
                    if server_state["memory_store"] is not None:
                        memory_context = build_memory_context(
                            clean_content,
                            server_state["memory_store"],
                            top_n=3,
                            max_chars=1600,
                            exclude_topics=("activity",) if _LIVE_STATE_QUERY_RE.search(clean_content) else (),
                        )

                    if is_screen_request(content):
                        trace("screen_request_start")
                        server_state["active_vision_pool"] = "active"
                        try:
                            screen_result = screen_response(content)
                        finally:
                            server_state["active_vision_pool"] = None
                        trace(f"screen_request_complete chars={len(screen_result)}")
                        log_direct("voice", "screen", "ok", "Screen captured.", chars=len(screen_result))
                        # Screen evidence is already the answer. Do not send it
                        # through Open Interpreter, which could delay, mutate, or
                        # invent a visual claim after the bounded vision attempt.
                        if response_is_active():
                            server_state["pending_screen_response"] = screen_result
                        else:
                            queue_direct_response(screen_result)
                        return
                    if memory_context:
                        content = memory_context + "\n\nUser request:\n" + content
                    if response_is_active():
                        # A response owns the lifecycle right now. Don't send this new
                        # message to the interpreter (it would stop the live response:
                        # "Open Interpreter stopping." / "NO CHUNKS SENT" cascade).
                        # Keep the newest request and dispatch it once the response
                        # completes (see new_output).
                        server_state["pending_user_text"] = clean_content
                        server_state["pending_input"] = {
                            "role": "user",
                            "type": "message",
                            "content": content,
                        }
                        return

                    server_state["brain_router"].apply_for(clean_content, interpreter)
                    server_state["turn_channel"] = ""  # local voice turn: no remote out-channel
                    _begin_single_turn(self, server_state, user=clean_content, source="voice")
                    _mark_turn_dispatch(server_state, interpreter, user=clean_content, content=content, source="voice")
                    await old_input({"role": "user", "type": "message", "start": True})
                    await old_input({"role": "user", "type": "message", "content": content})
                    await old_input({"role": "user", "type": "message", "end": True})
        except Exception as e:
            _flog.warning("input error: %s", e)
            server_state["brain_router"].mark_failure(reason=str(e))
            print(f"[STT/input error: {e}]")
            publish_status(STATUS_ERROR, f"Input error: {e}")
            output_queue = getattr(interpreter, "output_queue", None)
            if output_queue is not None:
                emit_input_terminal(output_queue)

    old_output = interpreter.output

    async def new_output(self):
        while True:
            try:
                output = await old_output()
                server_state["last_progress"] = time.monotonic()
                
                if isinstance(output, dict) and output.get("ignored") is True:
                    return {"ignored": True, "end": True}

                if isinstance(output, dict) and output.get("voice_paused") is not None:
                    return {"voice_paused": bool(output["voice_paused"]), "end": True}

                if isinstance(output, bytes):
                    return output

                await asyncio.sleep(0)

                if isinstance(output, dict) and output.get("type") == "error":
                    turn_error = _redact_text(str(output.get("content") or "unknown error"))[:4000]
                    _flog.warning("turn error: %s", turn_error)
                    _flog.warning("turn error: %s", turn_error)
                    server_state["turn_failed"] = True
                    server_state["turn_error"] = turn_error
                    publish_status(STATUS_ERROR, "Assistant endpoint error")
                    continue

                if (
                    isinstance(output, dict)
                    and output.get("type") == "status"
                    and output.get("content") == "complete"
                ):
                    trace("response_complete")
                    publish_status(STATUS_IDLE, "Standing by")
                    turn_failed = server_state.get("turn_failed", False)
                    if turn_failed:
                        server_state["turn_failed"] = False
                        turn_error = server_state.get("turn_error") or "Unknown provider error"
                    else:
                        server_state["brain_router"].mark_success()
                    server_state["tts_echo_turn_active"] = False
                    _flog.info("response complete, next window=%s", "hot" if not server_state["is_paused"] else "paused")
                    # The active response finished. Clear any pending barge-in so
                    # the next response is spoken normally, then dispatch any
                    # deferred user input (it MUST be dispatched from here so the
                    # lifecycle is owned by a single response at a time and no
                    # response is ever interrupted).
                    server_state["barge_in"] = False
                    _end_single_turn(self, server_state)
                    turn = _flush_turn_log(server_state)
                    chat_finalize(turn.get("assistant_snippet", "") if turn else "")

                    capture = server_state.get("telegram_capture")
                    if capture is not None and turn:
                        # The turn just said something while a Telegram chat
                        # was waiting: send the SAME text to that phone chat.
                        capture_chat_id = str(capture.get("chat_id") or "")
                        snippet_text = _redact_for_phone(turn.get("assistant_snippet") or "").strip()
                        if not snippet_text and (turn.get("actions") or []):
                            # Turn ran tools but the FINAL model call produced
                            # no spoken text (qwen3 sometimes closes an empty
                            # tool-call frame). Answer honestly instead of
                            # leaving /chat in a 504 silence / a hung phone.
                            step_count = len(turn["actions"])
                            snippet_text = (
                                f"Finished that — {step_count} step"
                                f"{'s were' if step_count != 1 else ' was'} completed, "
                                "but I produced no spoken answer this turn."
                            )
                        if capture_chat_id == TEXT_CHANNEL_CHAT_ID:
                            # No-voice /chat channel: hand the reply to the
                            # waiting text command instead of Telegram/TTS.
                            snippet_text = snippet_text or "(no reply was produced this turn)"
                            if snippet_text:
                                server_state.setdefault("text_reply_queue", asyncio.Queue())
                                await server_state["text_reply_queue"].put(snippet_text)
                            server_state["telegram_capture"] = None
                            server_state["telegram_turn_mute"] = False
                        elif capture_chat_id and snippet_text:
                            try:
                                await telegram_adapter.send_text(capture_chat_id, snippet_text)
                            except Exception as error:
                                _flog.warning("telegram reply failed: %s", error)
                            # The phone got its answer: end the muted window
                            # right here so follow-up local turns are audible.
                            server_state["telegram_capture"] = None
                            server_state["telegram_turn_mute"] = False
                    if (server_state["pending_input"] is None
                            and server_state["pending_router_response"] is None
                            and server_state["pending_screen_response"] is None
                            and server_state["pending_memory_response"] is None):
                        # Turn chain (including any queued follow-ups) finished.
                        server_state["telegram_turn_mute"] = False
                    if turn_failed:
                        # A provider/model failure ended the turn (OI swallows the
                        # exception into an "error" output chunk, then marks the
                        # turn complete).
                        #
                        # IMMEDIATE PROVIDER HOP (Sir directive: never hang, never
                        # error-and-silence): fail the exhausted candidate and
                        # instantly switch to the next one in the chain — every
                        # switch line is printed to console, captured in Logs,
                        # and the AGENTS tab reflects the live model.
                        acted = bool(turn and turn.get("actions"))
                        if not acted:
                            # No side effects yet: rewind to the dispatch
                            # snapshot, fail the exhausted candidate and hop.
                            _flog.warning("hop to next candidate after failure")
                            try:
                                from .self_improve import scratchpad_note

                                scratchpad_note(
                                    "PROVIDER SWITCHED - read scratchpad top: do NOT redo "
                                    "screenshots/surveys already logged; continue from the "
                                    "last recorded state instead.",
                                    tag="hop",
                                )
                            except Exception:
                                pass
                            hopped = server_state["brain_router"].failover_to_next(
                                interpreter, str(server_state.get("turn_user_text") or "request"),
                                reason=turn_error,
                            )
                            # FIX (2026-09-29): this branch used to `continue`
                            # unconditionally and discard the failover result, so a
                            # turn whose whole chain was dead re-dispatched forever
                            # and FRIDAY said nothing at all. Voice and typed turns
                            # never reached the retry budget below (it lived in a
                            # Telegram-only block), which is why the HUD just showed
                            # "FRIDAY is working" indefinitely. Now the budget is
                            # enforced HERE, for every channel, and when it runs out
                            # she says so plainly instead of going quiet.
                            #
                            # FIX (2026-09-30): `failover_to_next` returns False once
                            # it lands on LOCAL (local is the end of the chain), so
                            # testing that boolean alone made FRIDAY apologise
                            # "every thinking model is unavailable" WITHOUT ever
                            # running the local brain - defeating the whole point of
                            # the fallback chain. The budget is about re-dispatching,
                            # and landing on a real candidate - local included - is a
                            # legitimate hop. Only give up when the router produced
                            # no ticket at all, or we have genuinely run out of hops.
                            hop_count = server_state.get("turn_hop_count", 0) + 1
                            server_state["turn_hop_count"] = hop_count
                            hop_max = max(1, _max_turn_retries())
                            landed = getattr(server_state["brain_router"], "current", None)
                            if not landed or hop_count > hop_max:
                                _flog.error(
                                    "brain chain exhausted after %s hop(s), no candidate "
                                    "left: %s", hop_count, turn_error,
                                )
                                # FIX (2026-09-30): only penalise a candidate we
                                # actually ASKED. The router has already applied the
                                # next ticket by this point, and cooling that one
                                # punishes an untried provider (potentially a
                                # persisted 6h model ban on a candidate that never
                                # ran). The failure belongs to the candidate that
                                # was in place when the turn failed.
                                if not landed or landed[0] == "local":
                                    server_state["brain_router"].mark_failure(
                                        reason=turn_error
                                    )
                                server_state["turn_error"] = ""
                                server_state["turn_hop_count"] = 0
                                publish_status(STATUS_IDLE, "Standing by")
                                if not server_state["is_paused"]:
                                    sorry = (
                                        "Sorry, Sir - every thinking model I have is "
                                        "unavailable right now, so I could not finish that. "
                                        "Please try again in a moment."
                                    )
                                    queue_direct_response(sorry)
                                    await _deliver_failure_notice(server_state, sorry)
                                    # FIX (2026-09-30): clear the direct-text slot
                                    # after delivering. queue_direct_response() sets
                                    # last_direct_text, and the deterministic-reply
                                    # block below re-sends that value to Telegram /
                                    # the text channel, so leaving it set meant the
                                    # phone got the SAME apology twice and the text
                                    # channel kept a stale copy that answered the
                                    # next /chat request.
                                    server_state["last_direct_text"] = None
                                continue
                            messages_len = server_state.get("turn_messages_len")
                            if messages_len is not None and len(getattr(interpreter, "messages", [])) > messages_len:
                                interpreter.messages = interpreter.messages[:messages_len]
                            # FIX (2026-09-30): the hop path re-dispatched with
                            # raw old_input and never re-armed the turn log, so
                            # every RETRIED turn was invisible to _record_turn_output
                            # and always finalised as "snapshot_chars=0" even when
                            # the chat feed held the full reply. That made the one
                            # diagnostic for this class of bug report a lie. Re-arm
                            # the turn so the snapshot reflects what she said.
                            _begin_single_turn(
                                self,
                                server_state,
                                user=server_state.get("turn_user_text") or "request",
                                source=server_state.get("turn_source") or "text",
                            )
                            await asyncio.sleep(0.5)  # brief backoff before the next hop
                            await old_input({"role": "user", "type": "message", "start": True})
                            await old_input({"role": "user", "type": "message", "content": server_state.get("turn_content")})
                            await old_input({"role": "user", "type": "message", "end": True})
                            continue
                        # acted && will-not-retry: honest phone notice happens
                        # in the permanent-failure block below (dup guard).
                    capture = server_state.get("telegram_capture")
                    if turn is None or not (turn and turn.get("assistant_snippet")):
                        # Deterministic replies (launches/closes/browser/etc.)
                        # have no LLM turn: capture their handed-over text
                        # so the phone still gets the outcome.
                        direct_text = server_state.get("last_direct_text")
                        if direct_text and capture is not None:
                            chat_id = str(capture.get("chat_id") or "")
                            if chat_id == TEXT_CHANNEL_CHAT_ID:
                                server_state.setdefault("text_reply_queue", asyncio.Queue())
                                await server_state["text_reply_queue"].put(direct_text)
                            elif chat_id:
                                try:
                                    await telegram_adapter.send_text(chat_id, direct_text)
                                except Exception as error:
                                    _flog.warning("telegram direct-text failed: %s", error)
                            server_state["last_direct_text"] = None
                        server_state["telegram_capture"] = None
                        capture = None
                    if capture is not None and turn and turn.get("assistant_snippet"):
                        if capture is not None and not server_state.get("telegram_err_notified"):
                            # On the phone the silent failure was already
                            # acked with "Working on it" — make sure Sir
                            # ALWAYS gets a truthful note even when the
                            # whole chain fails.
                            server_state["telegram_err_notified"] = True
                            chat_id = str(capture.get("chat_id") or "")
                            if turn and turn.get("actions"):
                                # The tools DID run; only the wrap-up answer
                                # got rate-limited — tell Sir what happened
                                # truthfully instead of staying silent.
                                sorry = (
                                    "The actions ran, Sir — only my wrap-up message got "
                                    "rate-limited mid-run. Ask me to confirm and I will."
                                )
                            else:
                                sorry = (
                                    "Sorry, Sir — the brain providers hit rate limits mid-run. "
                                    "Say the request again in half a minute and I will take it from there."
                                )
                            try:
                                await telegram_adapter.send_text(chat_id, sorry)
                            except Exception as error:
                                _flog.warning("telegram failure notice failed: %s", error)
                            server_state["telegram_capture"] = None
                            # NOTE: telegram_turn_mute stays TRUE — any queued
                            # follow-up turns from this phone chain remain
                            # silent at the PC; a LOCAL user (voice/typed)
                            # takes the mute off, so Friday is never trapped
                            # in silence for the desk.
                        retry_count = server_state.get("turn_retry_count", 0)
                        max_retries = _max_turn_retries()
                        acted = bool(turn and turn.get("actions"))
                        can_retry = (
                            not acted
                            and retry_count < max_retries
                            and not server_state["is_paused"]
                            and server_state.get("turn_content")
                        )
                        if can_retry:
                            server_state["turn_retry_count"] = retry_count + 1
                            retry_content = server_state.get("turn_content")
                            retry_user = server_state.get("turn_user_text") or "request"
                            retry_source = server_state.get("turn_source") or "text"
                            # OI may have appended a partial/duplicated user or
                            # assistant message before failing. Rewind the message
                            # history to what existed at dispatch time so the
                            # retry replays the exact same request once.
                            messages_len = server_state.get("turn_messages_len")
                            if messages_len is not None and len(getattr(interpreter, "messages", [])) > messages_len:
                                interpreter.messages = interpreter.messages[:messages_len]
                            _flog.warning(
                                "turn failover attempt %s/%s after error: %s",
                                retry_count + 1,
                                max_retries,
                                turn_error,
                            )
                            publish_status(STATUS_THINKING, "FRIDAY is working")
                            await asyncio.sleep(1.0)  # Polite backoff delay before failover retry to prevent API exhaustion
                            _begin_single_turn(self, server_state, user=retry_user, source=retry_source)
                            server_state["brain_router"].failover_to_next(
                                interpreter, retry_user, reason=turn_error
                            )
                            await old_input({"role": "user", "type": "message", "start": True})
                            await old_input({"role": "user", "type": "message", "content": retry_content})
                            await old_input({"role": "user", "type": "message", "end": True})
                            continue
                        # No retry available (side effects already ran, or pool
                        # exhausted). Be honest and stay in standby.
                        _flog.warning(
                            "turn failed permanently: %s (acted=%s attempts=%s)",
                            turn_error, acted, retry_count,
                        )
                        server_state["brain_router"].mark_failure(reason=turn_error)
                        server_state["turn_error"] = ""
                        if not server_state["is_paused"]:
                            sorry = (
                                "Sorry, Sir — the assistant engine hit an error and couldn't finish that request. Please try again."
                            )
                            queue_direct_response(sorry)
                            # telegram_capture is already cleared here; use the
                            # turn_channel snapshot so /chat and Telegram turns
                            # still get the honest answer instead of a 504/timeout.
                            await _deliver_failure_notice(server_state, sorry)
                        if open_hot_window():
                            return {
                                "hot_window": True,
                                "active": True,
                                "seconds": server_state["hot_window_seconds"],
                            }
                        continue
                    if turn and turn.get("assistant_snippet"):
                        # The completed turn snapshot is the source of truth for
                        # what FRIDAY actually said. Emit it unconditionally so
                        # the live feed and console always show the reply even if
                        # the streaming message branch was skipped or redacted.
                        snapshot_text = turn["assistant_snippet"]
                        _flog.info("assistant: %s", _redact_text(snapshot_text))
                        try:
                            print(snapshot_text, flush=True)
                        except Exception:
                            pass
                    note_kind = maybe_record_project_note(turn)
                    if note_kind == "idea" and not server_state["is_paused"] and not response_is_active() and not social_guard.active():
                        queue_direct_response("That sounds like a fun project, Sir. I've noted it down.")
                    elif note_kind == "code":
                        _flog.info("note_taker: recorded project note silently")
                    if server_state["pending_memory_response"] is not None and not server_state["is_paused"]:
                        pending_memory_response = server_state["pending_memory_response"]
                        server_state["pending_memory_response"] = None
                        queue_direct_response(pending_memory_response)
                    elif server_state["pending_screen_response"] is not None and not server_state["is_paused"]:
                        pending_screen_response = server_state["pending_screen_response"]
                        server_state["pending_screen_response"] = None
                        queue_direct_response(pending_screen_response)
                    elif server_state["pending_router_response"] is not None and not server_state["is_paused"]:
                        pending_router_response = server_state["pending_router_response"]
                        server_state["pending_router_response"] = None
                        queue_direct_response(pending_router_response)
                    elif server_state["pending_input"] is not None and not server_state["is_paused"]:
                        pending = server_state["pending_input"]
                        server_state["pending_input"] = None
                        publish_status(STATUS_THINKING, "FRIDAY is working")
                        _begin_single_turn(
                            self, server_state,
                            user=server_state.get("pending_user_text") or "follow-up",
                            source="voice",
                        )
                        follow_up_text = server_state.get("pending_user_text") or pending["content"]
                        server_state["pending_user_text"] = None
                        server_state["brain_router"].apply_for(follow_up_text, interpreter)
                        _mark_turn_dispatch(server_state, interpreter, user=follow_up_text, content=pending["content"], source="voice")
                        await old_input({"role": "user", "type": "message", "start": True})
                        await old_input({"role": "user", "type": "message", "content": pending["content"]})
                        await old_input({"role": "user", "type": "message", "end": True})
                    elif not server_state["is_paused"]:
                        # Only a completed, owned response opens the one-turn
                        # follow-up window. Pending input takes precedence.
                        if open_hot_window():
                            return {
                                "hot_window": True,
                                "active": True,
                                "seconds": server_state["hot_window_seconds"],
                            }
                    maybe_run_digest(server_state, status_bus)
                    continue

                delimiters = ".?!;,\n…)]}"

                # ---- WHERE THE SCRIPT ACTUALLY ARRIVES (2026-09-30) -----------
                # Open Interpreter sends the code as a `code` event whose
                # payload key is `content`, NOT `code`. Verified live by
                # logging every output type:
                #   type='code' format='python' keys=['content','format','role','type']
                # FRIDAY only ever read `output.get("code")`, which never
                # exists, so `tool_code_parts` stayed EMPTY for the life of the
                # project and the "TOOL #n result" row was unreachable dead
                # code. That is exactly why Sir only ever saw
                # "OUT #1: python.exe" and never the script itself.
                if isinstance(output, dict) and output.get("type") == "code":
                    _script_payload = output.get("code") or output.get("content")
                    if _script_payload and not output.get("end"):
                        fragment = str(_script_payload)
                        tool_code_parts.clear()
                        tool_code_parts.append(fragment)
                        _flog.info(
                            "tool script captured: format=%s chars=%s",
                            output.get("format"), len(fragment),
                        )
                        chat_upsert_tool(
                            f"TOOL #{tool_counter['n']} — script "
                            f"({output.get('format') or 'code'}, {len(fragment)} chars):\n"
                            f"{fragment[:2000]}",
                            key=f"tool-{tool_counter['n']}-code",
                            status="streaming",
                        )

                if isinstance(output, dict) and output.get("type") == "code":
                    # Accumulate streamed code fragments so the console and
                    # log show WHAT tool is actually running instead of the
                    # useless "[Running tool: ...]" placeholder (code arrives
                    # across multiple chunks after the start flag).
                    if output.get("code"):
                        fragment = str(output.get("code"))
                        joined = "".join(tool_code_parts)
                        if fragment not in joined and joined not in fragment:
                            tool_code_parts.append(fragment)
                        elif joined and fragment.startswith(joined):
                            tool_code_parts.clear()
                            tool_code_parts.append(fragment)
                        joined_preview = "".join(tool_code_parts).strip()
                        if joined_preview:
                            _flog.info("tool code so far: %s", _redact_text(joined_preview)[:600])
                            # Show the ACTUAL script as it streams into a single
                            # live row, so Sir can always see which code FRIDAY
                            # is running instead of an opaque "python.exe".
                            chat_upsert_tool(
                                f"TOOL #{tool_counter['n']} — {str(output.get('language') or 'code')} "
                                f"(streaming, {len(joined_preview)} chars):\n{joined_preview[:1500]}",
                                key=f"tool-{tool_counter['n']}-code",
                                status="streaming",
                            )
                    if output.get("start"):
                        tool_code_parts.clear()
                        tool_counter["n"] += 1
                        if tool_counter["n"] > HARD_TOOL_CAP:
                            # Hard stop: the soft nudge was ignored; kill the
                            # agent turn so it cannot keep dry-running tools
                            # (async_core checks stop_event before each chunk).
                            _flog.warning(
                                "HARD tool stop at #%s (cap %s) — terminating victim turn",
                                tool_counter["n"], HARD_TOOL_CAP,
                            )
                            try:
                                interpreter.stop_event.set()
                            except Exception:
                                pass
                            publish_status(
                                STATUS_ERROR,
                                f"Stopped after {HARD_TOOL_CAP} tools — looping turn killed",
                            )
                            last_finding = ""
                            try:
                                from .self_improve import scratchpad_path
                                _sp_path = scratchpad_path()
                                if os.path.exists(_sp_path):
                                    _recent = open(
                                        _sp_path, "r", encoding="utf-8", errors="replace"
                                    ).read().splitlines()
                                    for _line in reversed(_recent):
                                        if "OUTCOME" in _line or "assistant:" in _line:
                                            last_finding = _line.split("|", 1)[-1].strip()[:280]
                                            break
                            except Exception:
                                pass
                            wrap = (
                                "I hit my tool budget this turn and stopped the loop. "
                                "What I had already confirmed: " + last_finding
                                if last_finding
                                else "I hit my tool budget this turn and stopped myself before looping further — ask me again and I'll keep it direct."
                            )
                            chat_append("tool", f"HARD STOP at tool #{tool_counter['n']} — loop terminated", status="complete")
                            _capture = server_state.get("telegram_capture")
                            if _capture is None:
                                # FIX (2026-09-30): a LOCAL voice/desk turn that
                                # hit the hard tool cap used to get NOTHING - the
                                # wrap only went to Telegram or the text queue, so
                                # FRIDAY killed the turn and went silent. She now
                                # says it out loud locally too. Speech-only: the
                                # turn is still live and OI will emit its own
                                # completion, so sending `complete` here too would
                                # finalise the turn twice and clear the ban on the
                                # candidate we just deliberately stopped.
                                queue_speech_only(wrap)
                            if _capture is not None and str(_capture.get("chat_id") or ""):
                                _c_chat_id = str(_capture.get("chat_id"))
                                if _c_chat_id == TEXT_CHANNEL_CHAT_ID:
                                    # No-voice /chat channel: deliver the honest
                                    # wrap to the waiting text command instead
                                    # of Telegram/TTS.
                                    server_state.setdefault("text_reply_queue", asyncio.Queue())
                                    await server_state["text_reply_queue"].put(
                                        _redact_for_phone(wrap).strip()
                                    )
                                    server_state["telegram_capture"] = None
                                    server_state["telegram_turn_mute"] = False
                                else:
                                    try:
                                        await telegram_adapter.send_text(_c_chat_id, wrap)
                                    except Exception as _e:
                                        _flog.warning("telegram hard-stop notice failed: %s", _e)
                                    # The phone got a real answer: end the muted
                                    # window so the late phase doesn't double-ping.
                                    server_state["telegram_capture"] = None
                                    server_state["telegram_turn_mute"] = False
                        trace("tool_start")
                        publish_status(STATUS_THINKING, f"Running tool #{tool_counter['n']}")
                        _play_chirp("tool_start")
                        language = str(output.get("language") or "code").strip()
                        tool_label = f"Tool #{tool_counter['n']} ({language})"
                        _flog.info("tool start: %s", tool_label)
                        chat_upsert_tool(
                            f"TOOL #{tool_counter['n']} — writing {language}...",
                            key=f"tool-{tool_counter['n']}-code",
                            status="streaming",
                        )
                        try:
                            print(f"[Tool #{tool_counter['n']} starting: writing {language}]", flush=True)
                        except Exception:
                            pass
                    if output.get("end"):
                        trace("tool_complete")
                        publish_status(STATUS_THINKING, "Tool finished")
                        _play_chirp("tool_complete")
                        tool_code = "".join(tool_code_parts).strip()
                        success = output.get("success", True)
                        if tool_code:
                            _flog.info("tool complete: success=%s code=%s", success, _redact_text(tool_code)[:800])
                            global LAST_TOOL_SCRIPT
                            LAST_TOOL_SCRIPT = {
                                "tool_number": tool_counter["n"],
                                "language": language,
                                "success": bool(success),
                                "code": _redact_text(tool_code)[:4000],
                                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                            chat_upsert_tool(
                                f"TOOL #{tool_counter['n']} — script ({len(tool_code)} chars, "
                                f"{'ok' if success else 'FAILED'}):\n{tool_code[:2000]}",
                                key=f"tool-{tool_counter['n']}-code",
                                status="complete",
                            )
                            try:
                                print(f"[Tool #{tool_counter['n']} {'OK' if success else 'FAILED'} code]\n{tool_code[:2000]}", flush=True)
                            except Exception:
                                pass
                            try:
                                from .self_improve import scratchpad_note

                                scratchpad_note(
                                    f"tool #{tool_counter['n']} ({success and 'ok' or 'failed'}): "
                                    f"{tool_code[:160]}",
                                    tag="tool",
                                )
                            except Exception:
                                pass
                        else:
                            _flog.info("tool complete: success=%s", success)
                        tool_cap = SERVER_TOOL_CAP[0]
                        if (
                            tool_counter["n"] >= tool_cap
                            and not server_state.get("tool_cap_nudged")
                        ):
                            # Loop guard: too many tool runs in one turn. The
                            # model rebuilds its context from interpreter.messages
                            # every agent iteration (respond.py:62), so this
                            # user-role nudge reaches the model and forces a
                            # pivot / truthful wrap-up instead of retrying a
                            # dead strategy 20+ times (Groq TPM killer).
                            server_state["tool_cap_nudged"] = True
                            guard_text = (
                                f"[System loop-guard] This turn has now run {tool_counter['n']} "
                                "tool executions without completing the task. READ "
"~/.friday/scratchpad.md FIRST (screenshots already captured in "
                                 "the screenshot folder (FRIDAY_SCRATCH_DIR / repo `screenshots`), locates already tried with results), then "
                                "STOP retrying the same strategy. Either pivot to a materially "
                                "DIFFERENT mechanism (e.g. launch via Start menu / os.startfile / "
                                "resolved exe path instead of hunting an icon, or keyboard "
                                "navigation instead of clicking), or wrap up NOW and tell Sir the "
                                "truth about what worked and what not. No more than ONE further "
                                "attempt allowed."
                            )
                            try:
                                interpreter.messages.append(
                                    {"role": "user", "type": "message", "content": guard_text}
                                )
                            except Exception:
                                pass
                            _flog.warning("tool loop-guard nudge injected at #%s", tool_counter["n"])
                            chat_append("tool", f"LOOP GUARD at tool #{tool_counter['n']} — pivot or wrap up", status="complete")
                            try:
                                print(f"[loop guard] tool #{tool_counter['n']} reached cap — nudge injected", flush=True)
                            except Exception:
                                pass
                    _record_turn_output(server_state, output)

                if isinstance(output, dict) and output.get("type") == "console" and output.get("content"):
                    # Show what the executed code printed/returned.
                    console_text = str(output.get("content") or "").strip()
                    # ---- INFINITE-LOOP / FLOOD GUARD (2026-09-30) -------------
                    # Observed live: after a Groq 429 failover, the brain wrote
                    # python containing an unbounded loop that printed "Task is
                    # not complete. Continuing..." forever. Because no `end`
                    # event ever arrived, the tool counter NEVER advanced, so
                    # the tool-cap loop guard could not fire and the turn hung
                    # indefinitely - FRIDAY simply never answered. The counter
                    # is the wrong place to catch this, so bound BOTH the
                    # number of console events and the total time in the turn.
                    turn_guard["console_events"] = turn_guard.get("console_events", 0) + 1
                    elapsed = time.monotonic() - turn_guard["started"]
                    over_events = turn_guard["console_events"] > TOOL_FLOOD_EVENT_CAP
                    over_time = elapsed > TOOL_WALL_CLOCK_LIMIT
                    if over_events or over_time:
                        if not turn_guard.get("flood_stopped"):
                            turn_guard["flood_stopped"] = True
                            reason = (
                                f"{turn_guard['console_events']} console events"
                                if over_events
                                else f"{int(elapsed)}s wall clock"
                            )
                            _flog.warning(
                                "tool flood/stall guard tripped (%s) — killing turn", reason
                            )
                            publish_status(
                                STATUS_ERROR,
                                "Stopped a runaway script so I could answer you",
                            )
                            chat_append(
                                "tool",
                                f"RUNAWAY SCRIPT STOPPED ({reason}) — the code was "
                                f"looping instead of finishing",
                                status="complete",
                            )
                            try:
                                interpreter.stop_event.set()
                            except Exception:
                                pass
                            # Tell the truth rather than go silent: this turn is
                            # being killed, so nothing downstream will speak for
                            # her. Mirrors the HARD tool-cap path.
                            flood_wrap = (
                                "I stopped that myself - the code I wrote started "
                                "looping instead of finishing, so I killed it rather "
                                "than hang. Ask me again and I'll take a direct route."
                            )
                            try:
                                queue_speech_only(flood_wrap)
                            except Exception:
                                pass
                            _flood_capture = server_state.get("telegram_capture")
                            if _flood_capture is not None and str(
                                _flood_capture.get("chat_id") or ""
                            ):
                                _f_chat_id = str(_flood_capture.get("chat_id"))
                                if _f_chat_id == TEXT_CHANNEL_CHAT_ID:
                                    server_state.setdefault("text_reply_queue", asyncio.Queue())
                                    await server_state["text_reply_queue"].put(flood_wrap)
                                    server_state["telegram_capture"] = None
                                    server_state["telegram_turn_mute"] = False
                                else:
                                    try:
                                        await telegram_adapter.send_text(_f_chat_id, flood_wrap)
                                    except Exception as flood_send_error:
                                        _flog.warning(
                                            "flood-stop notice failed: %s", flood_send_error
                                        )
                                    server_state["telegram_capture"] = None
                                    server_state["telegram_turn_mute"] = False
                        # Keep the log, but stop flooding it and the feed.
                        if turn_guard["console_events"] % 50 == 0:
                            _flog.info(
                                "tool console suppressed after runaway stop (%s total)",
                                turn_guard["console_events"],
                            )
                        _record_turn_output(server_state, output)
                        continue
                    _showable = bool(console_text) and not _console_noise(console_text)
                    if console_text and not _showable:
                        _flog.info("tool console(noise): %s", _redact_text(console_text)[:200])
                    elif _showable:
                        _flog.info("tool console: %s", _redact_text(console_text)[:600])
                        chat_append("tool", f"OUT #{tool_counter['n']}: {console_text[:600]}", status="complete")
                        try:
                            print(f"[Tool #{tool_counter['n']} console]\n{console_text[:1500]}", flush=True)
                        except Exception:
                            pass
                        try:
                            from .self_improve import scratchpad_note

                            scratchpad_note(
                                f"tool #{tool_counter['n']} printed: {console_text[:200]}",
                                tag="out",
                            )
                        except Exception:
                            pass

                if output.get("type") == "message" and len(output.get("content", "")) > 0:

                    if server_state["barge_in"]:
                        # Barge-in: user interrupted the current response. Do not
                        # feed or play its remaining text; let it drain silently.
                        await asyncio.sleep(0)
                        continue

                    content = output.get("content")
                    # Capture every assistant message for the UI feed
                    chat_append("assistant", content, status="streaming")
                    _record_turn_output(server_state, output)
                    if isinstance(content, str) and content and not _looks_like_tool_json(content):
                        _flog.info("assistant: %s", _redact_text(content))
                        try:
                            print(content, flush=True)
                        except Exception:
                            pass
                    if not _is_tts_text_speakable(content, self.tts.text()):
                        continue
                    if content.strip() in (".", "!", "?", ","):
                        continue
                    if server_state.get("telegram_turn_mute"):
                        # Telegram turn: the answer goes to the phone chat,
                        # nothing is spoken through the local head unit.
                        if server_state["tts_echo_turn_active"]:
                            server_state["tts_echo_turn_active"] = False
                        continue
                    if not server_state["tts_echo_turn_active"]:
                        server_state["recent_tts_texts"] = []
                        server_state["tts_echo_buffer"] = ""
                        server_state["tts_echo_turn_active"] = True
                    self.tts.feed(content)
                    _remember_tts_text(server_state, content)

                    if not self.tts.is_playing() and any([c in delimiters for c in content]): 
                        publish_status(STATUS_SPEAKING, "FRIDAY is speaking")
                        self.tts.play_async(on_audio_chunk=self.on_tts_chunk, muted=not self.play_audio, sentence_fragment_delimiters=delimiters, minimum_sentence_length=5)
                        trace("tts_start")
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "start": True}

                if output == {"role": "assistant", "type": "message", "end": True}:
                    trace("final_response_end")
                    if server_state["barge_in"]:
                        # Interrupted response segment ended. Stay muted for the
                        # rest of the response; the flag clears at "complete".
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
                    if server_state.get("telegram_turn_mute"):
                        # Telegram turn: no local playback; audio already
                        # directed at the phone chat via text.
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
                    if not self.tts.is_playing():
                        publish_status(STATUS_SPEAKING, "FRIDAY is speaking")
                        self.tts.play_async(on_audio_chunk=self.on_tts_chunk, muted=not self.play_audio, sentence_fragment_delimiters=delimiters, minimum_sentence_length=5)
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "start": True}
                    return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
            except Exception as e:
                _flog.warning("output error: %s", e)
                print(f"[TTS/output error: {e}]")
                publish_status(STATUS_ERROR, f"Output error: {e}")
                await asyncio.sleep(0.5)

    def on_tts_chunk(self, chunk):
        nonlocal tts_audio_seen
        if not tts_audio_seen:
            tts_audio_seen = True
            trace("tts_first_audio")
        self.output_queue.sync_q.put(chunk)

    if _reminders_enabled(interpreter):
        try:
            reminder_store = ReminderStore()
            server_state["reminder_store"] = reminder_store

            def reminder_is_quiet():
                return (
                    not server_state["is_paused"]
                    and not response_is_active()
                    and not interpreter.tts.is_playing()
                    and not social_guard.active()
                )

            def enqueue_reminder(message):
                queue_direct_response(message)

            reminder_worker = ReminderWorker(
                reminder_store,
                status_bus=status_bus,
                notification_hook=enqueue_reminder,
                quiet_predicate=reminder_is_quiet,
            ).start()
            interpreter.reminder_worker = reminder_worker
            interpreter.stop_reminders = reminder_worker.stop
            print("[reminders enabled]")
        except Exception as error:
            print(f"[reminders disabled: {error}]")

    interpreter.input = types.MethodType(new_input, interpreter)
    interpreter.output = types.MethodType(new_output, interpreter)
    interpreter.on_tts_chunk = types.MethodType(on_tts_chunk, interpreter)

    @interpreter.server.app.get("/ping")
    async def ping():
        return PlainTextResponse("pong")

    @interpreter.server.app.get("/open_app")
    async def open_app():
        """Second-launch handler: raise the HUD window of THIS instance
        (the tray-boot instance keeps it hidden until Sir opens it)."""
        import webview as _webview

        try:
            for window in _webview.windows:
                window.show()
                window.restore()
        except Exception as error:
            return PlainTextResponse("no window")
        return PlainTextResponse("FRIDAY window raised")

    @interpreter.server.app.post("/chat")
    async def chat(request: Request):
        """No-voice text command: POST the raw text (or {"text": "..."}) and
        receive FRIDAY's final reply. Runs the SAME dispatch_text chain as the
        HUD chat input and Telegram, so routers/memory/screen/brain all apply.
        The reply is delivered to the text_reply_queue instead of being spoken."""
        raw = (await request.body()).decode("utf-8", errors="replace").strip()
        text_in = raw
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
                    text_in = parsed["text"].strip() or text_in
            except Exception:
                pass
        if not text_in:
            return PlainTextResponse("empty request", status_code=400)
        timeout = float(os.environ.get("FRIDAY_TEXT_TIMEOUT", "240"))
        result = await dispatch_text(text_in, reply_channel="text")
        if not result.get("accepted"):
            return PlainTextResponse(f"NOT ACCEPTED: {result}", status_code=500)
        queue = server_state.setdefault("text_reply_queue", asyncio.Queue())
        try:
            reply = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return PlainTextResponse("(no reply within timeout)", status_code=504)
        return PlainTextResponse(reply)

    def terminal_input_thread():
        while True:
            try:
                input() 
                if server_state["is_paused"]:
                    server_state["is_paused"] = False
                    print("\r🟢 Voice mode activated! Listening...")
                    
                    interpreter.tts.feed("Voice mode activated.")
                    interpreter.tts.play_async(on_audio_chunk=interpreter.on_tts_chunk, sentence_fragment_delimiters=".?!;,\n…)]}", minimum_sentence_length=1)
                    
                    interpreter.output_queue.sync_q.put({"voice_paused": False, "end": True})
            except Exception as e:
                pass

    # --- Pipeline watchdog ---
    # If a response is in flight and nothing progresses for a long time (STT
    # waiting forever, a wedged model request, etc.), stop the in-flight turn,
    # reset status, and emit an end-of-stream so the mic keeps working.
    _watchdog_seconds = max(10.0, float(os.environ.get("FRIDAY_WATCHDOG_TIMEOUT", "180")))

    def _watchdog_skip_idle():
        if server_state["is_paused"]:
            return True
        if not (
            response_is_active()
            or server_state["pending_input"] is not None
            or server_state["pending_router_response"] is not None
            or server_state["pending_memory_response"] is not None
            or server_state["pending_screen_response"] is not None
        ):
            if not server_state.get("pulse_active"):
                return True
        try:
            player = getattr(interpreter, "tts", None)
            if player is not None and getattr(player, "is_playing", None):
                return bool(player.is_playing())
        except Exception:
            pass
        return False

    def watchdog_loop():
        while True:
            time.sleep(1.0)
            try:
                if os.environ.get("FRIDAY_WATCHDOG") == "0":
                    return
                if _watchdog_skip_idle():
                    continue
                stalled = time.monotonic() - server_state.get("last_progress", time.monotonic())
                if stalled < _watchdog_seconds:
                    continue
                _flog.warning("watchdog reset: no progress for %.0fs, pipeline wedged", stalled)
                print(f"[watchdog] no progress for {stalled:.0f}s, resetting pipeline", flush=True)
                stop_fn = getattr(interpreter, "stop", None)
                if callable(stop_fn):
                    try:
                        stop_fn()
                    except Exception as error:
                        _flog.warning("watchdog stop() raised: %s", error)
                server_state["barge_in"] = False
                server_state["last_progress"] = time.monotonic()
                publish_status(STATUS_IDLE, "Pipeline reset by watchdog")
                try:
                    interpreter.output_queue.sync_q.put({"ignored": True, "end": True})
                except Exception:
                    pass
            except Exception as error:
                _flog.warning("watchdog error: %s", error)
                time.sleep(5.0)

    _watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True, name="friday-watchdog")
    _watchdog_thread.start()

    def working_heartbeat():
        """While a response is in flight, publish every 5s so the UI never looks
        frozen (shows live elapsed time) and long model waits are visible."""
        while True:
            time.sleep(2.0)
            try:
                if not server_state.get("pulse_active"):
                    continue
                elapsed = int(time.monotonic() - server_state.get("pulse_started_at", time.monotonic()))
                if elapsed >= 4 and elapsed % 5 == 0:
                    publish_status(STATUS_THINKING, f"FRIDAY is working ({elapsed}s)")
            except Exception as error:
                _flog.debug("heartbeat error: %s", error)

    threading.Thread(target=working_heartbeat, daemon=True, name="friday-working-heartbeat").start()

    t = threading.Thread(target=terminal_input_thread, daemon=True)
    t.start()

    try:
        interpreter.server.run()
    finally:
        if reminder_worker is not None:
            reminder_worker.stop()
        for system in (desktop_ear, social_guard, focus_tracker, welcome_back):
            try:
                system.stop()
            except Exception:
                pass
