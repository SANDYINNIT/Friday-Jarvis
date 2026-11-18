from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from RealtimeSTT import AudioToTextRecorder
from RealtimeTTS import TextToAudioStream
import importlib
import asyncio
import collections
import contextvars
import dataclasses
from .chat_lifecycle import ChatRequest, LifecycleRegistry
from .speech_playback import emit_speech_frame, notify_speech_stop, record_playback, speech_snapshot
from .audio_link import AudioLink, AudioWebSocket
from .light_archive import ConversationArchive, archive_path
from .local_knowledge import KnowledgeStore, KnowledgeTurn
from .voice_trace import get_trace as _voice_trace_get
import hashlib
import types
import uuid
import time
import tempfile
import wave
import os
import threading
from threading import RLock
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
from . import identity_integration as _identity

os.environ["INTERPRETER_REQUIRE_ACKNOWLEDGE"] = "False"
os.environ["INTERPRETER_REQUIRE_AUTH"] = "False"

TEXT_CHANNEL_CHAT_ID = "@text-channel"

_TELEGRAM_DIRECT_SCREEN_RE = re.compile(
    r"^\s*(?:(?:hey|hi|okay|ok|please|friday)[,\s]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:"
    r"(?:take|send|share)\s+(?:me\s+)?(?:a\s+)?screenshot"
    r"(?:\s+of\s+(?:(?:my|the|your)\s+)?(?:screen|desktop|display))?"
    r"(?:\s+and\s+send\s+it(?:\s+to\s+me)?)?"
    r"|show\s+me(?:\s+(?:(?:my|the|your)\s+)?"
    r"(?:screen|desktop|display|view|what\s+you\s+see))"
    r"|screenshot"
    r")\s*(?:please|friday|sir|bro|boss)?\s*[.!?]*\s*$",
    re.IGNORECASE,
)


def _telegram_screen_request_kind(text):
    """Classify only screen requests that Telegram may safely auto-deliver."""
    value = " ".join(str(text or "").split())
    if not re.search(
        r"\bscreenshot\b|\bshow\s+me\b.{0,24}\b(?:screen|desktop|display|view)\b",
        value,
        re.IGNORECASE,
    ):
        return ""
    screenshot = re.search(r"\bscreenshot\b", value, re.IGNORECASE)
    if screenshot:
        before = value[:screenshot.start()]
        ordered = re.search(
            r"\b(?:then|after(?:ward|wards)?|once|when\s+.{0,24}\bdone|and\s+(?:then\s+)?)\b",
            before,
            re.IGNORECASE,
        )
        action = re.search(
            r"\b(?:open|close|find|search|type|write|click|change|create|run|move|"
            r"rename|delete|install|start|stop|play|pause|send|do|finish)\b",
            before,
            re.IGNORECASE,
        )
        if ordered and action:
            return "after_action"
    if _TELEGRAM_DIRECT_SCREEN_RE.fullmatch(value):
        return "direct"
    return ""

# 2026-10-02 (audit): module-level logger.
#
# `_flog` and `trace` are defined INSIDE start_server(), so they are closure
# names that a module-level function such as _mark_turn_dispatch cannot see.
# The history-trim logging used `_flog`/`trace`, which raised NameError the
# first time the cap actually fired - i.e. on the first turn after 200
# messages - turning a working history trim into an HTTP 500. That bug sat
# latent from the moment the trim was added, and only a real end-to-end run
# past 200 messages exposed it.
#
# It must be the PROJECT's logger (`friday`), not a fresh `logging.getLogger`:
# only that one is given a handler and DEBUG level by setup_file_logging(), so
# a bare logger's INFO lines are silently dropped by the last-resort handler and
# the trim becomes unobservable again - the same class of silent failure.
from .file_logger import friday_logger as _friday_logger

_history_log = _friday_logger()
# 2026-10-02: _log_empty_turn is a MODULE-level function, so it cannot use the
# `trace`/`_flog` closures inside start_server() - calling them raised NameError,
# which its own `except Exception: pass` then swallowed. That is the same latent
# bug that broke the history trim, in a second place.
_turn_log = _friday_logger()

# The most recent script FRIDAY wrote and ran this process, so she can be asked
# "which script did you just use?" and answer with the real code instead of a
# vague claim. Read by self_awareness.last_script(). No secrets: redacted.
LAST_TOOL_SCRIPT = {}

def emit_input_terminal(output_queue):
    """Release a light client whose audio request failed before dispatch."""
    output_queue.sync_q.put({"ignored": True, "end": True})


# PROCESS INVOKED vs TASK COMPLETED (2026-11-06).
# A tool's console output is the only evidence FRIDAY ever gets about whether
# the run itself worked: OI's code-end frame carries no status at all. Without
# this, `success=True` was reported next to a FileNotFoundError traceback
# (friday.log 2026-11-06 13:57:25) because the flag defaulted to True.
_TOOL_FAILURE_MARKERS = (
    "Traceback (most recent call last)",
    "Exception in thread",
)
_TOOL_FAILURE_LINE_PREFIXES = (
    "SyntaxError:",
    "IndentationError:",
    "MemoryError:",
)


def _console_shows_failure(text):
    """True when a tool's own console output shows the run FAILED.

    Heuristic on purpose - FRIDAY never receives an exit code for these runs,
    so the observable evidence is the output text itself. Deliberately module
    level and closure-free (test_audit_regressions) so it can be unit-tested
    directly. Never raises.
    """
    try:
        body = str(text or "")
        if not body.strip():
            return False
        if any(marker in body for marker in _TOOL_FAILURE_MARKERS):
            return True
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(_TOOL_FAILURE_LINE_PREFIXES):
                return True
        return False
    except Exception:
        return False


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


def tts_is_degraded(server_state):
    """True when every TTS engine is known dead, so audio must not be fed.

    Only CloudFirstTTSEngine implements the health API; any other engine (or a
    missing one) reports healthy, so this can never block normal speech.
    """
    engine = server_state.get("tts_engine") if isinstance(server_state, dict) else None
    checker = getattr(engine, "is_degraded", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def tts_degraded_status(server_state):
    """Truthful degradation status; safe when no engine/health API exists."""
    engine = server_state.get("tts_engine") if isinstance(server_state, dict) else None
    reader = getattr(engine, "degraded_status", None)
    if callable(reader):
        try:
            return dict(reader())
        except Exception:
            pass
    return {"degraded": False, "reason": "", "fail_streak": 0,
            "retry_in_seconds": 0.0, "last_stage": None}


def _prompt_render_stats():
    """What the system-prompt render cost - exposed on /voice/trace.

    ROUND ZB-LAT: `render_message` runs inside Open Interpreter's respond loop
    (one `{{}}` kernel block each, 11 of them in the default profile) between
    `brain select` and `llm_request_start`. It has no trace STAGE of its own, so
    before this it was invisible - it showed up only as a silent gap in
    friday.log. Measured on this machine: median **7.0s** over n=177 real turns
    (p90 9.0s), of which ~2s was the `localhost` IPv6 connect timeout in
    `self_awareness._ollama_models`.

    Never raises: a trace endpoint must not 500 because a counter moved.
    """
    try:
        from . import prompt_render_cache as _prc

        stats = dict(_prc.stats())
    except Exception as error:
        return {"available": False, "error": type(error).__name__}
    stats["available"] = True
    stats["note"] = (
        "seconds is ONE full render of the system prompt (kernel blocks + "
        "convert + trim), stamped when that render actually happened; `at` is "
        "wall clock so it lines up with friday.log"
    )
    return stats


def note_tts_degraded(server_state):
    """Record a non-audible TTS-degradation signal in server_state.

    Deliberately NOT audible: when every engine is dead there is nothing to
    speak with, so promising a spoken "voice is down" would be a lie. This
    writes a status record that logs/trace/HUD can read instead.
    Returns the status dict. Emits a log line only on the entry transition,
    so a degraded stretch does not flood the log.
    """
    status = tts_degraded_status(server_state)
    if not isinstance(server_state, dict):
        return status
    already = server_state.get("tts_degraded") or {}
    entry = {
        "at": time.time(),
        "reason": status.get("reason") or "all TTS engines failed",
        "retry_in_seconds": status.get("retry_in_seconds", 0.0),
        "fail_streak": status.get("fail_streak", 0),
        "last_stage": status.get("last_stage"),
    }
    server_state["tts_degraded"] = entry
    if not already:
        print("[tts degraded] no audio will be produced: %s" % entry["reason"],
              flush=True)
    return status


def _tts_recover_latch(tts):
    """Clear a stale RealtimeTTS playback latch left by a crash inside play().

    RealtimeTTS `text_to_stream.py:451` re-enters `play()` recursively when new
    text arrives while nothing was iterated. That recursion can die with
    RecursionError OUTSIDE play()'s try/finally, so `is_playing_flag` and
    `play_lock` are never released: every later `play_async()` is then refused
    with "already playing audio, skipping", which is why `tts_first_audio` and
    `tts_end` stopped firing and the 180s watchdog never repaired it.

    We cannot patch the wheel, so FRIDAY detects and repairs the latch itself.
    It only clears when the stream is demonstrably NOT playing (no live playback
    thread and `is_playing()` False), so a genuinely speaking stream is never
    disturbed. Returns True when a stale latch was cleared.
    """
    if tts is None:
        return False
    try:
        if not getattr(tts, "is_playing_flag", False):
            return False                       # not latched - nothing to do
        playing = getattr(tts, "is_playing", None)
        if callable(playing) and playing():
            return False                       # really playing
        thread = getattr(tts, "play_thread", None)
        if thread is not None and getattr(thread, "is_alive", lambda: False)():
            return False                       # playback thread still running
        try:
            tts.is_playing_flag = False
        except Exception:
            return False
        lock = getattr(tts, "play_lock", None)
        if lock is not None:
            try:
                if lock.locked():
                    lock.release()
            except Exception:
                pass
        return True
    except Exception:
        return False


def _begin_single_turn(interpreter, server_state, *, user="", source=None):
    if server_state.get("single_turn_loop") is None:
        server_state["single_turn_loop"] = getattr(interpreter, "loop", True)
        interpreter.loop = server_state["single_turn_loop"]
    # The HARD-tool-cap termination latch is per TURN: a new turn starts with
    # the drain off, or the residue of a killed turn would silence this one.
    server_state["tool_cap_terminated"] = False
    # Multi-step chaining runs with the loop engine ON but SAFE: server.py's
    # per-turn llm-call loop guard (traced_completions, FRIDAY_LLM_LOOP_GUARD)
    # force-breaks any text-only repeat loop instead of letting it spin.
    _begin_turn_log(server_state, user=user, source=source)
    # A new turn must not reuse the previous turn's rendered system prompt.
    # (The cache key also counts user messages, so this is belt-and-braces for
    # entry points that never reach this function.)
    try:
        from . import prompt_render_cache as _prompt_cache

        _prompt_cache.begin_turn()
    except Exception:
        pass


_TURN_TEXT_CHAR_LIMIT = 12000
_TURN_SNIPPET_LIMIT = 12000
_TURN_THINKING_LIMIT = 200
_TURN_CHUNK_LIMIT = 200
_TURN_ACTION_LIMIT = 50

# PHASE 3 (2026-10-03) reply-concurrency bounds.
# _TEXT_REPLY_QUEUE_MAX caps the legacy shared reply sink, which nothing reads.
_TEXT_REPLY_QUEUE_MAX = 32
# _PENDING_QUEUE_MAX caps how many requests may wait behind a running turn.
# Overflow evicts the OLDEST waiter and says so in the log - a bounded, visible
# loss instead of the previous unbounded, silent one.
_PENDING_QUEUE_MAX = 8
# ROUND ZB-P1: per-TOOL wall-clock bound. The turn budgets (600s wall / 300s
# silent) only fire when NOTHING progresses, and a chatty tool makes progress
# continuously - so on 2026-11-02 one script held a turn for ~9.4 minutes while
# console output kept arriving and no guard existed at all. This bounds a
# single tool's runtime; exceeding it cancels the turn exactly as the watchdog
# does. Overridable with FRIDAY_TOOL_TIMEOUT.
TOOL_TIMEOUT_SECONDS = max(10.0, float(os.environ.get("FRIDAY_TOOL_TIMEOUT", "120")))


def enforce_tool_timeout(tool_timing, interpreter, *, limit=None):
    """Bound ONE tool's wall clock and cancel the turn when it fires.

    Returns the elapsed seconds when the bound was exceeded (and cancels), or
    None when there is no running tool or it is still inside its budget.

    Why this exists: TURN_WALL_CLOCK_LIMIT (600s) and SILENT_HANG_LIMIT (300s)
    only fire when NOTHING progresses, and a tool that keeps printing makes
    progress forever. Observed 2026-11-02: one script held a turn for ~9.4
    minutes while console output kept arriving, with no guard at all. The
    cancellation uses `interpreter.stop_event` - the same mechanism as the
    watchdog and the HARD tool cap, and the only one that genuinely ends a
    running Open Interpreter turn.
    """
    started = (tool_timing or {}).get("started")
    if started is None:
        return None
    if limit is None:
        limit = TOOL_TIMEOUT_SECONDS
    try:
        elapsed = time.monotonic() - float(started)
    except Exception:
        return None
    if elapsed < limit:
        return None
    tool_timing["started"] = None
    _turn_log.warning(
        "tool timeout: one tool ran %.0fs (limit %.0fs) - cancelling turn",
        elapsed, limit)
    try:
        event = getattr(interpreter, "stop_event", None)
        if event is not None and hasattr(event, "set"):
            event.set()
    except Exception:
        pass
    return elapsed


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


# ---------------------------------------------------------------------------
# Internal control text must never reach the user (2026-09-30).
#
# Observed on Telegram: after a vision turn the bot sent, verbatim,
#   "I understand. I will continue only when the user's task is unfinished and
#    stop immediately if the task is complete. I will not repeat
#    acknowledgements or ask what to do next."
# That is Open Interpreter's `interpreter.loop_message` (default.py:414) -
# a control instruction FRIDAY feeds HERSELF to drive the agent loop. The
# model echoed it as if it were an answer, and the echo was delivered to the
# phone and spoken. Same family as the loop-breaker suffix ("The task is
# done.") leaking into replies.
#
# A model can always parrot its own instructions, so the durable fix is a
# delivery-layer filter, not a prompt change.
# ---------------------------------------------------------------------------
_INTERNAL_CONTROL_PATTERNS = (
    re.compile(r"\bcontinue only when the user'?s? task is\b", re.I),
    re.compile(r"\bif the task is complete,? stop immediately\b", re.I),
    re.compile(r"\bi will not repeat acknowledgements\b", re.I),
    re.compile(r"\bask what to do next\b", re.I),
    re.compile(r"^\s*the task is (?:done|impossible)\.?\s*$", re.I),
    re.compile(r"\[system loop-guard\]", re.I),
    re.compile(r"^\s*i have stopped\.?\s*$", re.I),
)


def _is_internal_control_text(value):
    """True when `value` is FRIDAY's own control text, not an answer."""
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _INTERNAL_CONTROL_PATTERNS)


def _request_words_only(value):
    """Extract Sir's ACTUAL words from a possibly wrapped request.

    Telegram turns prepend a long bracketed channel instruction
    ("[Sir is talking to you on Telegram - your reply goes to his phone...]")
    and memory context is prepended as "<ctx>\\n\\nUser request:\\n<text>".
    Logging the raw string wasted the entire 160-char budget on the wrapper, so
    the scratchpad recorded a garbled fragment of FRIDAY's own instructions
    instead of what Sir actually asked. Observed in the log:
    "- [18:07:06] request: Sir asked: [Sir is talking to you on Telegram -]
    your reply goes to his phone as a chat text. Answer in one short bubble."

    Module-level on purpose (see _clean_user_text): it must not touch anything
    that only exists inside start_server().
    """
    text = str(value or "")
    # The /chat endpoint dispatches the RAW request body, so the text can still
    # be JSON. Unwrap it before anything else or the journal/scratchpad store
    # '{"message": "..."}' instead of what Sir actually said.
    stripped_json = text.strip()
    if stripped_json.startswith("{"):
        try:
            import json as _json

            parsed = _json.loads(stripped_json)
            if isinstance(parsed, dict):
                for key in ("message", "text", "content", "prompt"):
                    candidate = parsed.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        text = candidate
                        break
        except Exception:
            pass
    marker = "\n\nUser request:\n"
    if marker in text:
        text = text.split(marker, 1)[1]
    # Round M (2026-10-03): a screen turn is prefixed with a
    # `[FRESH SCREEN EVIDENCE ...] ... [/FRESH SCREEN EVIDENCE]` block so the
    # brain is grounded in the capture. Without this strip, the durable "what
    # did Sir ask" journal recorded FRIDAY's own context block instead - observed
    # live: `ASKED: I captured and read the RIGHT screen ... (1 seconds ago)`.
    # The generic bracketed-wrapper loop below cannot handle it, because the
    # evidence text contains its own brackets and the loop only strips to the
    # FIRST `]`. Stripped here by explicit sentinel, before that loop.
    screen_open = "[FRESH SCREEN EVIDENCE"
    screen_close = "[/FRESH SCREEN EVIDENCE]"
    head = text.lstrip()
    if head.startswith(screen_open):
        end = head.find(screen_close)
        if end != -1:
            text = head[end + len(screen_close):].lstrip()
    # Drop a leading bracketed channel wrapper, including its closing "]\n\n".
    stripped = text.lstrip()
    while stripped.startswith("["):
        end = stripped.find("]")
        if end == -1:
            break
        stripped = stripped[end + 1:].lstrip()
    if stripped:
        text = stripped
    return " ".join(text.split())[:160]


def _clean_user_text(value):
    """Drop internal control text from anything bound for a human.

    Returns "" when the whole message was internal, so callers naturally skip
    sending it rather than delivering a blank bubble.

    NOTE: this is a MODULE-level function but `_flog` is a local of
    start_server(), so it must never touch `_flog` directly - doing so raised
    NameError inside the Telegram delivery path. A test caught this.
    """
    if _is_internal_control_text(value):
        try:
            import logging

            logging.getLogger("friday.control_text").info(
                "suppressed internal control text from user-facing output"
            )
        except Exception:
            pass
        return ""
    return str(value or "")


# Follow-up turns that reference a screenshot FRIDAY just sent get the saved
# vision read injected into brain context so she never pleads blind.
#
# WIDENED 2026-09-30 after the real failure: the original pattern only matched
# literal words like "screenshot"/"photo"/"do you see", so these follow-ups
# matched NOTHING and FRIDAY answered with no referent at all:
#   "What is the left one about tho? Like what's being run? What does it do."
#   "But what does the application do that is open?"
#   "what does the application do that is open on the left screen????"
# The symptom was "It seems there might be a misunderstanding or a typo in
# your message" to a perfectly clear question. Demonstratives ("the left
# one", "that app", "the window") and positional screen references now count.
_SCREENSHOT_REFERENCE_RE = re.compile(
    r"\bscreenshot\b|\b(?:the\s+)photo\b|\bthe\s+(?:picture|shot)\b|"
    r"\b(?:did\s+you|do\s+you)\s+(?:see|catch|lose|get)\b|"
    r"\bwhat\s+did\s+you\s+(?:just\s+)?(?:send)\b|\byou\s+(?:just\s+)?sent\b|"
    # demonstrative / positional references to what is currently on screen
    r"\bthe\s+(?:left|right|other|top|bottom)\s+(?:one|side|screen|monitor|window|app|application|tab)\b|"
    r"\b(?:left|right)\s+(?:screen|monitor|window|one|side)\b|"
    r"\b(?:this|that|these|those)\s+(?:one|app|application|window|program|tab|page)\b|"
    r"\bthe\s+(?:open|current|currently open|running)\s+(?:app|application|window|program|one)\b|"
    # Relative-clause forms, which are how people actually phrase it:
    # "the application THAT IS OPEN", "the app THAT'S running", "the window
    # you have open". Missing these is why a clear question like "what does
    # the application that is open do" was answered with a generic pixel
    # description instead of from the screen read.
    r"\b(?:app|application|window|program|tool|thing)\s+(?:that\s+(?:is|are)\s+|"
    r"that'\s*s\s+|which\s+is\s+)(?:open|running|current|active|on\s+screen)\b|"
    r"\b(?:app|application|window|program)\s+you\s+(?:have|got)\s+open\b|"
    r"\bwhat(?:'s|\s+is)\s+(?:being\s+run|running|open|on)\b|"
    r"\bwhat\s+does\s+the\s+(?:app|application|window|program|thing)\b|"
    r"\bon\s+(?:my\s+)?(?:left|right)\b",
    re.IGNORECASE,
)

# A screen read stays useful as context for a few minutes; a fresh capture is
# always better, but the brain must still know what she last SAW.
_SCREEN_CONTEXT_TTL_SECONDS = float(os.getenv("FRIDAY_SCREEN_CONTEXT_TTL", "900"))
# Round M: how much of the fresh screen report is injected into the brain's
# prompt. The report itself is already capped by
# screen_understanding.SCREEN_RESPONSE_LIMIT (1800 chars); this is a second,
# explicit bound on what is injected so a future larger report cannot silently
# inflate every screen turn's prompt.
_SCREEN_EVIDENCE_CHARS = int(os.getenv("FRIDAY_SCREEN_EVIDENCE_CHARS", "1800"))


# ROUND ZB-FS (2026-11-03): a screen search that came back EMPTY.
#
# Observed live: asked to find text that is not on the screen, the model kept
# re-shooting and re-OCRing for 330s+ (17 tools, ~7 minutes) because every call
# honestly answered "not found" and nothing in the loop treated a REPEATED empty
# result as the end of the answer. The existing guards could not see it:
#   * TOOL_TIMEOUT_SECONDS bounds ONE tool - all 17 finished in under 2 s;
#   * the console FLOOD guard needs the SAME bytes in a row, and the model
#     varied the wording each time ("test marker" / "marker" / "TEST MARKER");
#   * SERVER_TOOL_CAP only SUGGESTS wrapping up, and nothing enforces it;
#   * HARD_TOOL_CAP and the turn wall-clock are both ~10 minutes away.
# Upstream has no give-up mechanism either: open-interpreter issues #221 and
# #1236 (the latter closed "not planned"). Authoritative practice is to feed a
# structured not-found result back and then FORCE a final answer - OpenAI Agents
# SDK `tool_not_found_behavior="return_error_to_model"`, LangChain
# `early_stopping_method="generate"`, smolagents `provide_final_answer()`.
# This is the bounded half of that: N empty searches in ONE turn, then stop and
# tell Sir the truth. It matches only lines OUR tooling and the model print for
# a nil result, so a normal turn that simply finds nothing once is untouched.
_SEARCH_MISS_RE = re.compile(
    r"(?:find_text\b[^\r\n]*?\bnot\s+visible"
    r"|not\s+visible\s+on\s+screen"
    r"|(?:find_text|hits)\b[^\r\n]*?(?:->|:)\s*\[\s*\])",
    re.IGNORECASE,
)


def search_miss_target(text):
    """The absent target named by a nil-screen-search console line, else None.

    `None` means "this line is not a search miss at all", which is deliberately
    different from `""` ("a miss whose target we could not read") so a caller
    can count misses without having to parse anything itself.

    Module-level and free of `start_server` closure names on purpose:
    `test_audit_regressions.test_module_level_functions_never_use_start_server_closure_names`
    fails any module-level function that reaches for `_flog`, `trace`,
    `server_state`, `interpreter` and friends.
    """
    line = str(text or "")
    if not line.strip() or not _SEARCH_MISS_RE.search(line):
        return None
    quoted = re.search(r"['\"]([^'\"]{1,80})['\"]", line)
    return quoted.group(1) if quoted else ""


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


def _new_reply_token(server_state):
    # 2026-10-01: ONE shared asyncio.Queue with N waiters meant two simultaneous
    # /chat callers could receive each other's reply - or both hang while one
    # answer was consumed by the other. Give every request its OWN reply box and
    # route the turn's output to the box belonging to that request.
    token = uuid.uuid4().hex[:16]
    server_state.setdefault("reply_waiters", {})[token] = asyncio.Queue(maxsize=8)
    return token


def _post_reply(server_state, text, *, ownership=None):
    # Deliver one chunk of a turn's reply to THIS turn's request box. Falls back
    # to the legacy shared queue only when no request is waiting on it (the
    # voice/telegram paths), so existing behaviour is unchanged.
    token = ownership.reply_token if ownership is not None else server_state.get("turn_reply_token")
    waiter = (server_state.get("reply_waiters") or {}).get(token) if token else None
    if waiter is not None:
        try:
            waiter.put_nowait(text)
            return True
        except asyncio.QueueFull:
            # The caller is not draining fast enough. Say so instead of
            # returning a truncated answer under HTTP 200.
            _turn_log.warning("reply box full, chunk dropped: token=%s len=%d",
                              _redact_text(str(token))[:16], len(str(text)))
            return False
        except Exception:
            return False
    if ownership is not None:
        # An expired owner's box must never become another request's reply.
        return False
    # No request owns this reply. Historically this was an UNBOUNDED
    # asyncio.Queue that nothing ever read: text accumulated for the life of
    # the process and was silently lost. It is now bounded, and an overflow is
    # logged rather than swallowed.
    try:
        queue = server_state.setdefault("text_reply_queue", asyncio.Queue(maxsize=_TEXT_REPLY_QUEUE_MAX))
        queue.put_nowait(text)
        return True
    except asyncio.QueueFull:
        _turn_log.warning("text_reply_queue overflow (%d chunks), reply dropped: len=%d",
                          _TEXT_REPLY_QUEUE_MAX, len(str(text)))
        return False
    except Exception:
        return False


def _close_reply_box(server_state, token):
    # Drop the box when the request is done, so the dict cannot grow forever.
    waiters = server_state.get("reply_waiters") or {}
    waiters.pop(token, None)


# ---------------------------------------------------------------------------
# PHASE 3 (2026-10-03): the pending queue.
#
# This replaces SIX independent scalar slots (pending_input, pending_user_text,
# pending_reply_token, pending_router_response, pending_memory_response,
# pending_screen_response) which were written as three separate dict
# assignments and therefore behaved as LAST-WRITER-WINS:
#
#   * two requests queued behind one turn -> the first was silently discarded
#     and its caller waited out the full 240 s /chat timeout;
#   * a voice follow-up wrote pending_reply_token = None, so a parked /chat
#     request kept running but could never be handed its answer (guaranteed
#     504, and the answer was spoken on the desk as well);
#   * the drain read pending_input, pending_user_text and pending_reply_token
#     as three separate reads, which could disagree.
#
# One record now carries everything ONE request needs, so records cannot
# interleave and the drain always sees a self-consistent request.
# ---------------------------------------------------------------------------


def _pending_queue(server_state):
    """The live pending deque, created on demand so a partially-built state
    dict (several tests build one by hand) still works."""
    queue = server_state.get("pending_turns")
    if queue is None:
        queue = collections.deque()
        server_state["pending_turns"] = queue
    return queue


def _pending_count(server_state):
    return len(_pending_queue(server_state))


def _pending_push(server_state, *, user_text, content=None, reply_token=None,
                  source="text", direct_response=None, ownership=None):
    """Queue ONE waiting request. Returns the record that was evicted to make
    room, or None.

    `ownership` (a TurnOwnership, or None) is the reply destination of the
    request being deferred. It travels WITH the record because the running
    turn releases the shared ownership slot when it finishes: without it the
    drain dispatched a queued Telegram turn as a LOCAL one and its answer was
    spoken on the PC instead of texted (the drain only ever restored
    /chat reply tokens).
    """
    queue = _pending_queue(server_state)
    evicted = None
    if len(queue) >= _PENDING_QUEUE_MAX:
        evicted = queue.popleft()
        _turn_log.warning(
            "pending queue full (%d), evicted oldest waiter: source=%s token=%s",
            _PENDING_QUEUE_MAX,
            _redact_text(str(evicted.get("source") or "?"))[:16],
            _redact_text(str(evicted.get("reply_token") or ""))[:16],
        )
    queue.append({
        "user_text": str(user_text or ""),
        "content": content,
        "reply_token": reply_token,
        "source": source,
        "direct_response": direct_response,
        "ownership": ownership if ownership is not None else _request_owner(reply_token),
        "queued_at": time.monotonic(),
    })
    # ROUND ZB-P1: this admission gate used to be SILENT. On 2026-11-02 an
    # accepted stop request vanished here with no log line at all - no
    # `brain select`, no `llm_request_start` - and only the 180s watchdog
    # revealed it. Every channel (voice / text / Telegram) funnels through this
    # one function, so a single line makes deferral observable everywhere.
    _turn_log.info(
        "admission deferred: queued %s turn (pending=%d): %s",
        source, len(queue), _redact_text(str(user_text or ""))[:80])
    return evicted


def _pending_peek(server_state):
    queue = _pending_queue(server_state)
    return queue[0] if queue else None


def _pending_pop(server_state):
    queue = _pending_queue(server_state)
    return queue.popleft() if queue else None


def _pending_clear(server_state):
    _pending_queue(server_state).clear()


def _pending_has(server_state):
    return bool(_pending_queue(server_state))


def _external_reply_pending(server_state):
    """True while a Telegram or `/chat` turn still owes its reply elsewhere.

    Option 2 (2026-10-18): reads the IMMUTABLE ownership record rather than the
    legacy mirrored dict, so there is one source of truth. `is_external` is true
    exactly when the answer is owed to somewhere other than the PC.

    Used to stop a LOCAL turn from taking the mute off underneath a phone turn
    that is still running - the 2026-10-18 bug where Sir's Telegram replies were
    also spoken through the PC speakers.
    """
    return _ownership(server_state).is_external


@dataclasses.dataclass(frozen=True)
class TurnOwnership:
    """Immutable record of WHO a single turn belongs to.

    Replaces the pair `telegram_capture` (mutable dict) + `telegram_turn_mute`
    (mutable bool), which every interleaving turn could overwrite: a local
    voice turn landing during a phone turn silently changed the phone turn's
    mute, and its reply was then spoken through the PC as well as texted.

    Frozen on purpose. Ownership is INSTALLED once at admission and REPLACED at
    completion - never mutated in place - so a turn can no longer be quietly
    re-owned halfway through producing its answer.
    """

    # "" = local (voice/desktop), "telegram" = phone chat, "text" = /chat box.
    channel: str = ""
    chat_id: str = ""
    reply_token: str = None
    # Only a turn Sir explicitly asked to speak may use the PC speakers.
    speaks_locally: bool = True
    post_action_screenshot: bool = False
    screen_request: str = ""
    private_text: bool = False

    @property
    def is_external(self):
        """True when the answer is owed to somewhere other than the PC."""
        return bool(self.channel)

    @property
    def muted(self):
        """True when this turn's answer must NOT be spoken on the PC."""
        return self.private_text or (self.is_external and not self.speaks_locally)


# The ownership of a purely local turn. A module constant because it is
# immutable - callers can never accidentally mutate the default.
LOCAL_OWNERSHIP = TurnOwnership()


def _request_owner(reply_token=None):
    if reply_token is not None:
        return TurnOwnership("text", TEXT_CHANNEL_CHAT_ID, reply_token, speaks_locally=False)
    return TurnOwnership()


def _ownership(server_state):
    """The current turn's ownership record, never None."""
    own = server_state.get("turn_ownership")
    return own if isinstance(own, TurnOwnership) else LOCAL_OWNERSHIP


def _install_ownership(server_state, ownership):
    """Point the turn at exactly ONE request's ownership. Single writer.

    Option 2 is now COMPLETE: the legacy `telegram_capture` dict and
    `telegram_turn_mute` bool are gone. Every reader in the server reads the
    record, so there is no mirror left to drift out of step with it - which was
    the original failure mode (a mutable dict and a mutable bool, written from
    13 different places, disagreeing mid-turn).

    ROUND LAB-2B: ownership install is also the turn-admission boundary for
    the lab's coordinated serving projection. Pure bookkeeping - never a
    router, never gating - and best-effort: a lab fault can never break the
    turn path that the user actually uses.
    """
    if not isinstance(ownership, TurnOwnership):
        raise TypeError("ownership must be a TurnOwnership, got %r" % (ownership,))
    server_state["turn_ownership"] = ownership
    server_state["turn_reply_token"] = ownership.reply_token
    if ownership is not LOCAL_OWNERSHIP:
        try:
            from . import lab_authority as _lab_auth
            authority = _lab_auth.shared()
            if authority is not None:
                authority.turn_admitted(str(getattr(ownership, "channel", "") or ""))
        except Exception:
            pass
    return ownership


def _clear_ownership(server_state, expected=None):
    """The turn is over: hand the PC back to a local, audible turn.

    ROUND LAB-2B: also the turn-completion boundary for the lab projection
    (duty -> idle, serving stays as last-known-served). Best-effort only.
    """
    if expected is not None and _ownership(server_state) is not expected:
        return False
    if server_state.get("identity_integration") is not None:
        _identity.close(server_state, _ownership(server_state))
    server_state["turn_attempt"] = None
    try:
        from . import lab_authority as _lab_auth
        authority = _lab_auth.shared()
        if authority is not None:
            authority.turn_cleared("ownership cleared")
    except Exception:
        pass
    return _install_ownership(server_state, LOCAL_OWNERSHIP)


def _install_text_reply_route(server_state, reply_token):
    """Point the shared reply slots at ONE /chat caller's box.

    Only ever call this when the caller has been proven to OWN the turn (or to
    own the whole request, for a deterministic reply) - it mutates global
    state. A queued request must not call it until the drain runs it, which is
    exactly the bug the 2026-10-03 REPLY-ROUTING fix corrected."""
    if reply_token is None:
        return False
    _install_ownership(server_state, _request_owner(reply_token))
    return True


def _log_screen_call_ledger():
    """PHASE 5 (2026-10-03) MEASUREMENT ONLY - no behaviour change.

    Emits ONE ordered line naming every provider call the screen path made, with
    per-stage counts. Before this the only screen-path evidence in the log was
    `screen_request_start` / `screen_request_complete`, so the number of vision
    calls (one PER MONITOR) and whether the composed answer was ever used had to
    be inferred from the code rather than measured.

    Read `screen_turn.source=composer|canned_builder` and `composer_used` to see
    whether the composer's text became the answer; `brain` calls are counted
    separately by `traced_completions`' existing `llm call:` lines.
    """
    try:
        from .screen_understanding import take_call_ledger

        calls = take_call_ledger()
    except Exception as error:
        _turn_log.warning("screen call ledger unavailable: %s", type(error).__name__)
        return
    if not calls:
        return
    counts = {}
    for entry in calls:
        counts[entry.get("stage") or "?"] = counts.get(entry.get("stage") or "?", 0) + 1
    _turn_log.info(
        "screen calls: total=%d %s | %s",
        len(calls),
        " ".join("%s=%d" % kv for kv in sorted(counts.items())),
        " -> ".join(
            "%s(%s,%sms)" % (c.get("stage"), c.get("provider"),
                             c.get("elapsed_ms"))
            for c in calls
        ),
    )


def _record_fresh_screen_evidence(server_state):
    """Round M (2026-10-03). Publish the capture just made into the EXISTING
    `server_state["last_screen_shot"]` structure, so the brain is handed the
    screen evidence FRIDAY already paid for.

    Why this exists: `last_screen_shot` was written in exactly ONE place - the
    Telegram photo path - so on the voice and /chat screen paths the freshness
    check below could never see a fresh read. It therefore always took the "my
    last screen read is N minutes old ... capture a FRESH screenshot" branch,
    telling the brain to ignore a vision read it had obtained seconds earlier.
    Measured consequence (2026-10-03, one real screen turn): the brain made 13
    LLM calls and 12 tool calls over 172s re-deriving what the capture already
    contained, and answered "I produced no spoken answer this turn".

    Deliberately reuses `last_screen_shot` rather than adding a second screen
    memory, and never touches the composer's text: the brain still authors the
    user's answer.
    """
    try:
        from .screen_understanding import take_screen_evidence

        evidence = take_screen_evidence()
    except Exception as error:
        _turn_log.warning("screen evidence unavailable: %s", type(error).__name__)
        return None
    if not evidence or not evidence.get("report"):
        return None
    server_state["last_screen_shot"] = {
        "at": evidence.get("captured_at") or time.time(),
        "description": evidence.get("report") or "",
        "regions": evidence.get("regions") or [],
        "region_count": evidence.get("region_count") or 0,
        "ocr": bool(evidence.get("ocr")),
        "ocr_chars": int(evidence.get("ocr_chars") or 0),
        "vision_calls": int(evidence.get("vision_calls") or 0),
        "vision_chars": int(evidence.get("vision_chars") or 0),
        "vision_ok": bool(evidence.get("vision_ok")),
        "window": evidence.get("window") or {},
        "source": "screen_capture",
    }
    _turn_log.info(
        "screen evidence stored: regions=%s ocr=%s ocr_chars=%d vision_calls=%d "
        "vision_chars=%d report_chars=%d",
        evidence.get("region_count"), evidence.get("ocr"),
        int(evidence.get("ocr_chars") or 0), int(evidence.get("vision_calls") or 0),
        int(evidence.get("vision_chars") or 0), len(evidence.get("report") or ""),
    )
    return server_state["last_screen_shot"]


def _screen_context_prefix(server_state):
    """Build the brain-facing context block for a screen turn.

    Round M. The old wording claimed "the screenshot I just captured AND SENT
    you", which is false on the voice and /chat paths - no screenshot is sent
    there. It also gave the brain no reason not to re-capture, which is exactly
    what it did (12 tool calls). This says what was actually read, when, and
    that the evidence is already in hand.
    """
    shot = server_state.get("last_screen_shot") or {}
    shot_age = time.time() - float(shot.get("at") or 0)
    if shot.get("description") and shot_age <= _SCREEN_CONTEXT_TTL_SECONDS:
        regions = shot.get("regions") or []
        where = ", ".join(str(r) for r in regions if r) or "the screen"
        captured = time.strftime("%H:%M:%S", time.localtime(float(shot.get("at"))))
        facts = []
        if shot.get("vision_ok"):
            facts.append(
                "a vision model read %d characters of it"
                % int(shot.get("vision_chars") or 0))
        if shot.get("ocr") and int(shot.get("ocr_chars") or 0):
            facts.append("OCR read %d characters of text" % int(shot.get("ocr_chars") or 0))
        window = shot.get("window") or {}
        if isinstance(window, dict) and (window.get("title") or window.get("process")):
            facts.append("active window: %r" % (
                window.get("title") or window.get("process"),))
        how = "; ".join(facts) if facts else "no readable text or vision was returned"
        return (
            "[FRESH SCREEN EVIDENCE - already collected, do not re-capture]\n"
            f"I captured and read {where} at {captured} "
            f"({int(shot_age)} seconds ago), before writing this message. "
            f"From that capture: {how}.\n"
            "Here is what was actually on screen:\n"
            + str(shot.get("description") or "")[:_SCREEN_EVIDENCE_CHARS]
            + "\nAnswer Sir from THIS evidence, in your own voice. It is newer "
            "than anything you remember. Do NOT call computer.vision.query(), "
            "computer.display.find() or take another screenshot to answer a "
            "question this already covers - only reach for a tool if Sir asks "
            "for something this evidence does not contain, and say plainly if it "
            "is not there. If he says 'the left one' / 'the app' / 'the window', "
            "he means something in the evidence above; resolve it from there.\n"
            "[/FRESH SCREEN EVIDENCE]\n\n"
        )
    return (
        "[Context: my last screen read is "
        + f"{int(shot_age / 60)} minutes old, so it may be stale. If the "
        "user is asking about what was on screen, capture a FRESH "
        "screenshot rather than answering from the old one.]\n\n"
    )


_producing_attempt = contextvars.ContextVar("friday_producing_attempt", default=None)


@dataclasses.dataclass(frozen=True)
class _AttemptOutput:
    attempt: object
    payload: object
    playback: bool = False
    event_id: object = dataclasses.field(default_factory=object, compare=False)


class _OwnedOutputQueue:
    """Tag interpreter output at enqueue time, on its producer thread."""

    def __init__(self, queue):
        self._queue = queue
        self.sync_q = self

    def __getattr__(self, name):
        return getattr(self._queue, name)

    @staticmethod
    def _tag(item):
        attempt = _producing_attempt.get()
        if attempt is not None and not isinstance(item, _AttemptOutput):
            return _AttemptOutput(attempt, item)
        return item

    def put(self, item, *args, **kwargs):
        return self._queue.sync_q.put(self._tag(item), *args, **kwargs)

    def put_nowait(self, item):
        return self._queue.sync_q.put_nowait(self._tag(item))


def _current_output(server_state, output):
    if isinstance(output, _AttemptOutput):
        key = "playback_attempt" if output.playback else "turn_attempt"
        return output.attempt is server_state.get(key), output.payload
    # Keep unowned transport controls, but never attribute an untagged
    # assistant or completion frame to the turn that happens to be active.
    if isinstance(output, bytes) or (isinstance(output, dict) and (
        output.get("role") == "assistant" or output.get("type") in ("status", "error", "code", "console")
    )):
        return False, output
    return True, output


async def _dispatch_provider_end(old_input, server_state, interpreter):
    """Observe the selected route only when its admitted generation is sent."""
    generation = server_state.get("turn_attempt")
    if server_state.get("identity_integration") is not None:
        _identity.brain_dispatched(server_state, interpreter, generation)
    await old_input({"role": "user", "type": "message", "end": True})


def _observe_owned_brain_result(server_state, output, outcome):
    if (server_state.get("identity_integration") is not None
            and isinstance(output, _AttemptOutput) and not output.playback
            and _current_output(server_state, output)[0]):
        _identity.brain_result(server_state, output.attempt, outcome)


def _blank_llm_response_shape():
    """The empty per-turn provider-shape accumulator (ROUND ZA, 2026-10-05).

    Diagnostics only: sizes, names and a prompt hash. No key, no header, no
    payload, no message content.
    """
    return {"calls": [], "turn_valid": False}


# ---------------------------------------------------------------------------
# ROUND ZB-L3 L3-2: a turn that hits the hard tool cap must still SPEAK.
#
# THE FAILURE THIS FIXES. On 2026-10-07 a real turn did 25 tools against a
# healthy Modal provider: the viewport genuinely moved (the model's own digest
# went 6571c867 -> 9ed7f552), and then `HARD_TOOL_CAP` killed the turn. The
# journal recorded `shape=reasoning_only actions=25 chunks=0 len=0` and the
# `/chat` caller got a 504 after 240 s. Mechanical work succeeded and the owner
# was told NOTHING. Round Z saw the same family at actions=12 and it was filed
# as "the model made tools and emitted no text" - but the cap is a FRIDAY
# decision, and FRIDAY chose to end the turn with no words in it.
#
# Three rules, in priority order:
#   1. Try ONE more completion, with NO tools available, to narrate what
#      actually happened.
#   2. If that yields nothing usable, emit a deterministic fallback built from
#      what was genuinely verified - never a fabricated success.
#   3. Never let another tool execute after the cap.
#
# This deliberately does NOT raise the cap, add a model tier, change routing or
# touch the provider chain. The final completion goes to the provider that is
# ALREADY configured for this turn (`interpreter.llm`), so it is the same chain
# position - it is one more call, not a different ladder.
# ---------------------------------------------------------------------------
FINAL_NARRATION_TIMEOUT = float(os.getenv("FRIDAY_FINAL_NARRATION_TIMEOUT", "45"))
FINAL_NARRATION_MAX_TOKENS = 800


def _verified_work_summary():
    """What this turn can HONESTLY claim it proved.

    Reads the real UI action ledger, which only records a scroll once its
    viewport demonstrably changed. Nothing here is inferred from intent.
    """
    verified = []
    unverified = []
    try:
        from . import ui_scroll

        for record in ui_scroll.action_history():
            if not isinstance(record, dict):
                continue
            label = "%s %s" % (record.get("action") or "action",
                               record.get("window") or "")
            if record.get("verified"):
                verified.append("%s (%s)" % (label.strip(),
                                             record.get("mechanism") or "?"))
            else:
                unverified.append(label.strip())
    except Exception:
        pass
    return {"verified": verified, "unverified": unverified}


def _tool_cap_fallback_text(tools_run, summary=None):
    """The deterministic, truthful sentence when narration cannot be obtained.

    Says what was verified, says what was not, and does not apologise into a
    claim. It is deliberately boring: this is the branch where the model could
    not be trusted to describe its own work, so the code describes it instead.
    """
    summary = summary or {"verified": [], "unverified": []}
    verified = summary.get("verified") or []
    unverified = summary.get("unverified") or []
    parts = ["I hit my tool budget after %d tools, so I stopped there." % tools_run]
    if verified:
        parts.append("What I did verify: %s."
                     % "; ".join(verified[-3:]))
    else:
        parts.append("I did not verify anything before I ran out of tools, "
                     "so I am not claiming any of it worked.")
    if unverified:
        parts.append("Still unfinished: %s."
                     % "; ".join(unverified[-3:]))
    parts.append("Ask me again and I will take it in smaller steps.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# ROUND ZB-L4 L4-1: recover a turn the PROVIDER truncated mid-reasoning.
#
# THE PROVEN FAILURE (2026-10-07, live, healthy Modal, journal evidence)
# ----------------------------------------------------------------------
# A 12-tool turn hit the existing SERVER_TOOL_CAP nudge at tool #12. That nudge
# was delivered on call #13, and call #13 came back:
#
#     max_tokens=4096  reasoning_effort=None  think=UNSET
#     reasoning_len=17426  content_len=0  finish_reason="length"
#
# The model spent its whole budget on reasoning and was cut off before emitting
# any content or any tool call. Open Interpreter then ended the loop NORMALLY
# (respond.py:448 "Doesn't want to run code. We're done!") because there was
# nothing left to run. FRIDAY recorded it truthfully as `shape=reasoning_only`
# and said nothing to Sir.
#
# It was NOT: HARD_TOOL_CAP (12 < 24), stop_event, the wall-clock budget, the
# silent-hang budget, the flood cap, a provider outage, a lost delivery, or
# corrupt telemetry. Reproduced deterministically at the provider: at
# max_tokens=4096 the same endpoint returns finish=length with zero content.
#
# WHY THIS IS DETECTION AND NOT A GUARD. The signal is already recorded, per
# call, by _merge_chunk_shape - `finish_reason` and `content_len`. Nothing new
# is timed, nothing is counted, and no new budget is introduced. This fires on
# one exact proven signature and on nothing else.
#
# All four conditions are required:
#   1. the turn produced no assistant text   (nothing to duplicate)
#   2. the turn DID do work                  (actions > 0)
#   3. the final call was cut short          (finish_reason == "length")
#   4. the final call had no content at all  (content_len == 0)
#
# Condition 4 matters: a LONG ANSWER can also hit `length`, and that is a
# normal completion, not a failure. Without it this would overwrite a real
# answer with a narration.
# ---------------------------------------------------------------------------
def _budget_truncated_turn(server_state, turn):
    """True only for the proven signature: length + no content + work done.

    Returns (matched, last_call) so the caller can log the evidence that made
    it match. Never raises: a turn must not die because a diagnostic raised.
    """
    try:
        if not turn:
            return False, None
        if turn.get("assistant_snippet"):
            return False, None
        if not (turn.get("actions") or []):
            return False, None
        shape = (server_state or {}).get("llm_response_shape") or {}
        calls = [c for c in (shape.get("calls") or []) if isinstance(c, dict)]
        if not calls:
            return False, None
        last = calls[-1]
        if str(last.get("finish_reason") or "") != "length":
            return False, None
        if int(last.get("content_len") or 0) != 0:
            return False, None
        return True, last
    except Exception:
        return False, None


async def _recover_truncated_turn(interpreter, server_state, turn):
    """Narrate a provider-truncated turn. Returns (text, source) or (None, "").

    Reuses the L3-2 machinery verbatim: one tools-free completion, then the
    deterministic truthful sentence. It never fabricates success - if neither
    source produces text, the fallback reports what was and was not verified.

    Deliberately returns its source instead of logging. `_flog` and `trace` are
    CLOSURE locals of `start_server`, so a module-level helper that referenced
    them raised NameError at the exact moment it was trying to save a turn. The
    caller - which is inside start_server and has both - does the logging.
    """
    tools_run = len((turn or {}).get("actions") or [])
    summary = _verified_work_summary()
    try:
        narration = await _final_narration_completion(
            interpreter,
            server_state.get("last_user_text") or "",
            tools_run,
            summary,
        )
    except Exception:
        narration = None
    if narration:
        return narration, "narration"
    return _tool_cap_fallback_text(tools_run, summary), "truthful_fallback"


def _final_narration_prompt(request_text, tools_run, summary):
    verified = summary.get("verified") or []
    unverified = summary.get("unverified") or []
    evidence = []
    if verified:
        evidence.append("VERIFIED: " + "; ".join(verified[-5:]))
    if unverified:
        evidence.append("NOT VERIFIED: " + "; ".join(unverified[-5:]))
    if not evidence:
        evidence.append("VERIFIED: nothing.")
    return (
        "You have used your entire tool budget for this turn (%d tools) and you "
        "may not call any more tools. Sir is waiting for an answer.\n\n"
        "Write ONE short spoken reply, in Sir's own words, that reports what you "
        "actually established. Rules you must not break:\n"
        "- Report ONLY what is listed as verified below. Anything not verified, "
        "say plainly you did not get to it.\n"
        "- NEVER claim the task is done if the evidence does not show that.\n"
        "- If the work was only partly done, say it is part-done and name the "
        "part you did not finish.\n"
        "- No preamble, no headings, no bullet points, no tool syntax. Spoken "
        "English only.\n\n"
        "HIS REQUEST: %s\n\n"
        "EVIDENCE:\n%s\n"
        % (tools_run, (request_text or "(not recorded)")[:400],
           "\n".join(evidence))
    )


async def _final_narration_completion(interpreter, request_text, tools_run,
                                     summary=None):
    """One final, tools-free completion. Returns text, or None.

    Returns None rather than raising on every failure mode: this runs on the
    dying edge of a turn, and an exception here would replace a truthful
    fallback with silence, which is the bug being fixed.

    It also refuses to run when no usable endpoint is configured, so it can
    never fire an unbounded retry loop against a dead provider.
    """
    summary = summary if summary is not None else _verified_work_summary()
    try:
        llm = getattr(interpreter, "llm", None)
        base = getattr(llm, "api_base", None)
        model = getattr(llm, "model", None)
        key = getattr(llm, "api_key", None)
        if not base or not model or not key:
            return None
        if str(model).startswith("ollama_chat/") or str(base).startswith("http://localhost"):
            # The local candidate is reached through OI, not OpenAI-compatible
            # HTTP. Narration is a nicety; it must not turn into a second local
            # inference that competes with a busy fallback brain.
            return None
        url = "%s/chat/completions" % str(base).rstrip("/")
        if not url.endswith("/v1/chat/completions"):
            url = "%s/v1/chat/completions" % str(base).rstrip("/")
        payload = {
            "model": str(model),
            "messages": [{"role": "user", "content": _final_narration_prompt(
                request_text, tools_run, summary)}],
            "max_tokens": FINAL_NARRATION_MAX_TOKENS,
            # No `tools` key at all: this completion cannot call anything.
        }
        headers = {"Authorization": "Bearer %s" % key,
                   "Content-Type": "application/json"}
        import requests as _requests

        response = await asyncio.to_thread(
            _requests.post, url, json=payload, headers=headers,
            timeout=FINAL_NARRATION_TIMEOUT,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        text = str(text or "").strip()
        return text or None
    except Exception:
        return None


def _reset_llm_response_shape(server_state):
    """Re-arm the per-turn provider-shape accumulator for a NEW turn.

    ROUND ZA. This used to live inside `traced_completions`, gated on
    `interpreter.messages[-1]["role"] == "user"`, which is an LLM-LAYER signal:
    it only ever fires once a turn has already decided to call the provider.
    Every deterministic turn therefore kept the PREVIOUS turn's numbers, which
    is how a turn that made no provider call could be recorded as
    `provider_chunks=6` next to a named model. The comment at the old site said
    its purpose was to stop exactly that inheritance - the reset was simply in
    the wrong lifecycle layer.

    Called from `_stamp_progress(..., turn_started=True)`, the one helper every
    channel already uses to mark "a new turn begins", so the state is correct
    by construction before any provider call rather than patched afterwards.
    """
    server_state["llm_response_shape"] = _blank_llm_response_shape()


def _stamp_progress(server_state, *, turn_started=False):
    """Record genuine forward progress for the watchdog (ROUND W, 2026-10-04).

    The watchdog measures "time since the last real work". That is only
    meaningful if the clock is re-armed at the moment work actually begins, and
    never counts idle time from before the turn.

    Two failures motivated this helper, both measured live:

    * BOOT FALSE POSITIVE. `last_progress` was seeded once at process start and
      only re-stamped on some paths, so the first request arriving more than
      FRIDAY_WATCHDOG_TIMEOUT after boot was killed on arrival ("no progress for
      181s" measured 2026-10-01; "1275s" measured 2026-10-04 13:40:35, one
      second into a turn that then ran to completion).

    * IDLE-BEFORE-TURN. Turn-start stamping lived on individual channels, so a
      turn arriving by a path that missed it was judged against the PREVIOUS
      turn's stamp.

    `turn_started=True` additionally marks the turn boundary, which is what lets
    the turn wall-clock budget be measured from the right origin.
    """
    try:
        server_state["last_progress"] = time.monotonic()
        if turn_started:
            server_state["turn_started_at"] = time.monotonic()
            server_state["turn_progress_observed"] = False
            # A fresh turn may be ended on its budget once. Re-armed here so
            # the NEXT turn is not silently exempted.
            server_state["turn_budget_enforced"] = False
            # ROUND ZA: the per-turn provider-shape accumulator belongs to the
            # turn, so it is re-armed here with the other per-turn budgets -
            # NOT inside traced_completions, which only runs for turns that
            # reach the provider and therefore cannot clear it for the ones
            # that do not. This site is reached by every channel, including the
            # four deterministic early returns in dispatch_text that return
            # before _mark_turn_dispatch. On the text and voice channels the
            # marker fires once more via _mark_turn_dispatch, still strictly
            # before the interpreter runs, so no provider call can ever have
            # recorded anything that a second firing could erase.
            _reset_llm_response_shape(server_state)
    except Exception:
        pass


# Fields `_stamp_progress(turn_started=True)` overwrites. Kept as one tuple so
# the snapshot/restore pair below can never drift from the writer.
_WATCHDOG_TURN_FIELDS = ("last_progress", "turn_started_at",
                         "turn_progress_observed", "turn_budget_enforced",
                         "llm_response_shape")
_MISSING = object()


def snapshot_turn_baseline(server_state):
    """Capture the RUNNING turn's watchdog/budget state before it is re-armed."""
    return {key: server_state.get(key, _MISSING) for key in _WATCHDOG_TURN_FIELDS}


def restore_turn_baseline(server_state, snapshot):
    """Undo a turn-start stamp for a request that was DEFERRED rather than run.

    A queued request must not extend the turn it is waiting behind. Measured
    2026-11-02: an accepted stop utterance hit `_stamp_progress(turn_started=True)`
    BEFORE the admission gate, which re-armed `last_progress` and
    `turn_started_at` for a turn that was never dispatched - so stall detection
    moved from ~14:29:06 to 14:31:38 (the queued utterance + 181s) and the
    running turn's 600s wall budget was reset and could never fire.
    """
    if not isinstance(snapshot, dict):
        return
    for key, value in snapshot.items():
        if value is _MISSING:
            server_state.pop(key, None)
        else:
            server_state[key] = value


def _mark_turn_dispatch(server_state, interpreter, *, user, content, source, reply_token=None):
    """Remember the current turn's source request so a provider failure can
    be retried on the next pool without losing context, and bound the carried
    conversation."""
    # ROUND W (2026-10-04): EVERY channel reaches this chokepoint, so this is
    # the one place that can correctly re-arm the watchdog for a new turn. The
    # per-channel stamps that used to do this were incomplete, which is how the
    # watchdog came to judge a fresh turn against a stamp from 21 minutes
    # earlier.
    _stamp_progress(server_state, turn_started=True)
    # 2026-10-01: bound the carried conversation to the newest 200 messages
    # (Sir: at least 100, 200 for safety). Done HERE, at a turn boundary, so an
    # in-flight tool call is never severed, and the retained window always starts
    # on a user message so no orphaned tool result survives the cut. Before this,
    # interpreter.messages was UNBOUNDED and reset only by a process restart.
    #
    # AUDIT D4 (2026-10-02): this used to be wrapped in `except Exception: pass`,
    # so if the import or the trim ever failed the cap silently stopped applying
    # and nothing would say so - the exact failure mode this function prevents.
    # It logs now. It MUST use the module-level logger: `_flog`/`trace` are
    # closures inside start_server() and are not visible here - using them
    # raised NameError on the first trim and turned the turn into a 500.
    try:
        from .self_awareness import trim_conversation_history

        _dropped, _kept = trim_conversation_history(interpreter, keep=200)
        if _dropped:
            _history_log.info(
                "conversation history trimmed dropped=%d kept=%d", _dropped, _kept)
    except Exception as error:
        _history_log.warning(
            "history_trim_failed error=%s", type(error).__name__, exc_info=True)
    # Stamp this turn's reply box so every chunk of its output is routed to the
    # request that asked for it rather than to whichever /chat caller is next.
    # Always stamp it, INCLUDING with None. Setting it only when a token was
    # passed left a STALE token on the state, so a later voice/Telegram turn
    # (which carries no token) routed its reply into the previous /chat
    # caller's already-closed box and the answer was lost.
    server_state["turn_reply_token"] = reply_token
    server_state["turn_user_text"] = _redact_text(user)
    server_state["turn_content"] = content
    server_state["turn_source"] = source
    # Provenance for the action-confirmation gate. A UIA action cannot be
    # authorised by a caller-supplied boolean; it needs a signed token minted from
    # the words Sir ACTUALLY said. This chokepoint is the only writer of that
    # ledger, and every channel (typed / voice / Telegram follow-up) passes
    # through it, so consent cannot be smuggled in another way.
    try:
        from . import action_confirmations as _confirmations

        _confirmations.note_user_turn(
            _request_words_only(content) or _request_words_only(user),
            source=source)
    except Exception as error:
        _history_log.warning("action confirmation ledger not updated: %s",
                             type(error).__name__)
    server_state["turn_retry_count"] = 0
    # Provider-hop budget for this turn. Enforced for EVERY channel (voice,
    # typed, Telegram) so a fully dead chain can never loop silently.
    server_state["turn_hop_count"] = 0
    server_state["turn_failed"] = False
    server_state["turn_error"] = ""
    server_state["turn_messages_len"] = len(getattr(interpreter, "messages", []))
    # MEASUREMENT (2026-10-03): wall-clock origin for this turn, so the hop and
    # tool-complete log lines can report a real elapsed_ms instead of forcing
    # anyone to subtract timestamps by hand. Monotonic: immune to clock changes.
    server_state["turn_started_at"] = time.monotonic()

    # Turn-scoped blacklist reset: strict forward-only failover — the chain
    # never revisits a candidate that failed earlier in this turn.

    # Disk-backed turn scratchpad: survives provider hops (files are not

    # rewound with interpreter.messages) so each replay inherits what the
    # previous replays tried instead of repeating them blindly.
    try:
        from .self_improve import scratchpad_note, scratchpad_reset

        scratchpad_reset()
        scratchpad_note(
            f"Sir asked: {_request_words_only(content) or _request_words_only(user)}",
            tag="request",
        )
        # Durable, verbatim record of the request. The scratchpad above dies with
        # this turn; this does not, so she can be truthful about what she was
        # asked and did (see self_improve.append_outcome for why the server
        # records this instead of trusting her to remember to).
        try:
            from .self_improve import append_outcome

            append_outcome(
                _request_words_only(content) or _request_words_only(user),
                tag="asked",
            )
        except Exception:
            pass
    except Exception:
        pass


def _turns_journal_path():
    """Append-only turn-anomaly journal: `~/.friday/logs/turns.jsonl`.

    MEASUREMENT (2026-10-03). `file_logger` opens `friday.log` with `mode="w"`,
    so a fresh app start TRUNCATES it - deliberate, because that file exists to
    mirror the console. The consequence for diagnosis is that the empty-turn
    evidence recorded on 2026-10-02 was gone by the next morning, so the issue
    could never accumulate samples. This journal is append-only and survives
    restarts, which is the only way a rare anomaly becomes investigable.

    Honours `FRIDAY_LOG_DIR` (the same override `file_logger.log_file_path`
    uses) so the test suite can redirect it via conftest.py.
    """
    base = os.environ.get("FRIDAY_LOG_DIR") or os.path.join(
        os.path.expanduser("~"), ".friday", "logs")
    return os.path.join(base, "turns.jsonl")


def _append_turn_record(record):
    """Append one JSON line. Never raises - diagnostics must not break a turn."""
    try:
        path = _turns_journal_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps(dict(record, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z")),
                          default=str)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except Exception as error:
        try:
            _turn_log.warning("turn anomaly not recorded: %s",
                              type(error).__name__)
        except Exception:
            pass
        return False


def _turn_elapsed_ms(server_state):
    """Milliseconds since this turn was dispatched, or None if unknown.

    MEASUREMENT (2026-10-03): the failover paths logged *that* a hop happened
    but never how long the failing candidate had already taken, so a slow
    provider and a slow tool were indistinguishable from the outside.
    """
    try:
        started = (server_state or {}).get("turn_started_at")
        if started is None:
            return None
        return int((time.monotonic() - started) * 1000)
    except Exception:
        return None


def _max_turn_retries():
    """Failover budget: every usable Brain cloud key plus model-level chain
    entries plus one local attempt — enough to walk the whole documented
    chain (Modal -> Groq -> OpenRouter -> Gemini -> local)."""
    summary = api_pools.describe_pools()
    total_keys = sum(
        summary.get(name, {}).get("total", 0)
        for name in (
            "brain_modal", "brain_groq", "brain_openrouter", "brain_gemini",
        )
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
    elif otype == "code":
        _record_tool_action(turn, output)
    elif otype == "reasoning":
        text = str(output.get("content") or "")
        if text and len(turn["thinking"]) < _TURN_THINKING_LIMIT:
            turn["thinking"].append(_redact_text(text)[:800])


def _flush_tool_action(turn):
    """Materialise the buffered tool call as exactly ONE recorded action.

    PHASE 4 (2026-10-03): `actions` was structurally always 0, because the
    recorder read `output.get("code") or output.get("result")` while OI streams
    a tool call as DELTA frames carrying **`content`** (`format` for the
    language). Proof it was wrong rather than merely rare: a turn that ran 12
    tools was audited as `action_count=0` (boot_console.log 2026-10-03 05:21:47).

    Simply reading `content` would over-count instead, because OI emits one
    frame per delta of the same call's `arguments.code`. The `start` flag is the
    real boundary (server.py increments tool_counter on it), so the buffer is
    flushed when the NEXT tool call starts and again at turn flush."""
    buffered = turn.pop("_code_buf", "") or ""
    language = turn.pop("_action_tool", "") or "code"
    if not str(buffered).strip():
        # Empty/malformed tool calls (e.g. Groq "failed to parse tool call
        # arguments") execute nothing - they must NOT count as a performed
        # action, so a turn-level failover stays possible.
        return
    if len(turn["actions"]) >= _TURN_ACTION_LIMIT:
        return
    turn["actions"].append({"tool": language, "snippet": _redact_text(buffered)[:400]})
    turn["actions"] = turn["actions"][-_TURN_CHUNK_LIMIT:]


def _record_tool_action(turn, output):
    if output.get("start"):
        # A new tool call begins: close the previous one first.
        _flush_tool_action(turn)
    # OI's tool-call frames carry the language under `format`; older/other
    # paths use `language`. Neither ever sets `code` or `result`.
    language = str(output.get("language") or output.get("format") or "code")
    payload = output.get("code") or output.get("result") or output.get("content")
    if payload:
        turn["_code_buf"] = (turn.get("_code_buf") or "") + str(payload)
        turn["_action_tool"] = language
    if output.get("end"):
        _flush_tool_action(turn)


def _describe_chunk(chunk):
    """Structural, CONTENT-FREE description of one provider stream chunk.

    Measured live against the primary on 2026-10-03 (structural probe, no
    payload captured), which is what this exists to record honestly:

    * the chunk is a LiteLLM **`ModelResponseStream` object**, not a dict, so
      duck-typed `.get()` on it can fail silently;
    * `choices[0]` carries `finish_reason`, `index`, `delta`, `logprobs`;
    * the visible answer arrives in **`delta.content`** and the thinking in
      **`delta.reasoning_content`** - they are separate fields, and with a tight
      `max_tokens` the model burns the budget on reasoning and `content` never
      appears at all (finish_reason then becomes `length`);
    * **`usage` is never present** in this provider's stream - it is `null` even
      on completely successful calls, so its absence carries no information.

    Only field NAMES, python TYPES and character LENGTHS are recorded. No
    content, no keys, no headers, no request/response payloads.
    """
    info = {"type": type(chunk).__name__, "top": [], "choice": [],
            "delta": [], "chars": {}, "finish_reason": None,
            "usage": None, "parse": "raw"}
    data = None
    for attempt in ("model_dump", "dict", "json", "raw"):
        try:
            if attempt == "model_dump" and hasattr(chunk, "model_dump"):
                data = chunk.model_dump()
            elif attempt == "dict" and isinstance(chunk, dict):
                data = chunk
            elif attempt == "json" and hasattr(chunk, "json"):
                data = chunk.json()
            elif attempt == "raw" and isinstance(chunk, dict):
                data = chunk
            if isinstance(data, dict):
                info["parse"] = attempt
                break
            data = None
        except Exception:
            data = None
    if data is None:
        return info

    info["top"] = sorted(str(k) for k in data.keys())[:24]
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        choice = choices[0]
        info["choice"] = sorted(str(k) for k in choice.keys())[:16]
        info["finish_reason"] = choice.get("finish_reason")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = choice.get("message") if isinstance(choice.get("message"), dict) else None
        if isinstance(delta, dict):
            info["delta"] = sorted(str(k) for k in delta.keys())[:16]
            for key, value in delta.items():
                if isinstance(value, str) and value:
                    name = str(key)[:32]
                    info["chars"][name] = info["chars"].get(name, 0) + len(value)
                elif key == "tool_calls" and value:
                    # Record only that tool calls were present, never their args.
                    info["chars"]["tool_calls"] = len(value)
    usage = data.get("usage")
    if isinstance(usage, dict):
        info["usage"] = sorted(str(k) for k in usage.keys())[:12]
    elif usage is not None:
        info["usage"] = type(usage).__name__
    return info


def _prompt_fingerprint(messages):
    """A stable SIZE + HASH for a request, so a recurrence can be tied to a
    specific request shape.

    Deliberately content-free: only the message count, the total character
    count, and a SHA-256 prefix. No text, no role names, no payload."""
    try:
        items = list(messages or [])
    except Exception:
        return {"messages": 0, "chars": 0, "sha256_12": ""}
    chars = 0
    digest = hashlib.sha256()
    for item in items:
        try:
            body = item.get("content") if isinstance(item, dict) else str(item)
        except Exception:
            body = ""
        if not isinstance(body, str):
            body = "" if body is None else str(body)
        chars += len(body)
        digest.update(body.encode("utf-8", "replace"))
        digest.update(b"\x1f")
    return {"messages": len(items), "chars": chars,
            "sha256_12": digest.hexdigest()[:12]}


def _scrub_tool_choice(value):
    """`tool_choice` may be a provider name, a dict, or "auto". Record which
    shape it was and any literal string - never a schema blob."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:24]
    if isinstance(value, dict):
        return {"type": str(value.get("type") or "?")[:24]}
    return type(value).__name__


def _merge_chunk_shape(accumulator, chunk):
    """Fold one chunk's structure into ONE provider call's accumulator."""
    try:
        info = _describe_chunk(chunk)
    except Exception:
        return
    accumulator["chunks"] = accumulator.get("chunks", 0) + 1
    accumulator["chunk_type"] = info["type"]
    accumulator["parse"] = info["parse"]
    for key in ("top", "choice", "delta", "usage"):
        if info.get(key) and not accumulator.get(key):
            accumulator[key] = info[key]
    if info.get("finish_reason"):
        accumulator["finish_reason"] = info["finish_reason"]
    for name, length in (info.get("chars") or {}).items():
        accumulator["chars"] = dict(accumulator.get("chars") or {})
        accumulator["chars"][name] = accumulator["chars"].get(name, 0) + length
    # Named counters kept for backwards compatibility with the first record.
    chars = accumulator.get("chars") or {}
    accumulator["content_len"] = chars.get("content", 0)
    accumulator["reasoning_len"] = chars.get("reasoning_content", 0) + \
        chars.get("reasoning", 0)


def _log_empty_turn(server_state, turn, response_shape=None,
                    turn_failed=None, turn_error=None):
    """Record WHY a turn produced no assistant text. Diagnostic only.

    2026-10-02 audit (#2). A turn can complete with `outcome=ok`, no actions and
    no text, and the user gets "(no reply was produced this turn)". I have not
    established the cause, so this does NOT guess and does NOT retry - it only
    makes the next occurrence diagnosable instead of invisible.

    What it logs is the signature that distinguishes the two candidate causes
    documented in SEARCHES.md 39 ("when a model returns nothing, check
    finish_reason and reasoning_tokens BEFORE blaming the provider"):

      * reasoning non-empty + no content  -> the token budget was consumed by
        thinking. Correct fix is a larger budget / lower reasoning_effort, NOT
        a failover.
      * reasoning empty + no content     -> something else entirely (parse,
        empty stream, provider); do not assume a budget problem.

    The audit log records none of this (its assistant_turn rows carry
    response_len and thinking but no serving model), and OI does not expose
    finish_reason, so the serving model is read here from the in-process router.

    Deliberately NOT routed into the provider-hop path: turn_log is also absent
    for screen turns that bypass the interpreter, so treating "empty" as
    "failed" would fire spurious retries and risk duplicate answers.
    """
    try:
        turn = turn or {}
        thinking = turn.get("thinking") or []
        model = pool = ""
        # PHASE 4: the caller passes the failure snapshot, because
        # `turn_failed` is cleared before this runs and reading it here would
        # always say False.
        if turn_failed is None:
            turn_failed = server_state.get("turn_failed")
        if turn_error is None:
            turn_error = server_state.get("turn_error")
        try:
            serving = _brain_router_module.live_serving_state()
            model = str(serving.get("current_model") or serving.get("last_model") or "")
            pool = str(serving.get("current_pool") or serving.get("last_pool") or "")
        except Exception:
            pass
        rs = dict(response_shape or {})
        calls = [c for c in (rs.get("calls") or []) if isinstance(c, dict)]
        # PHASE 4 (2026-10-03): the discriminator now comes from the PROVIDER,
        # not from OI output frames. OI's LLM layer has no `reasoning_content`
        # handler at all, so it never emits a `type: "reasoning"` frame and
        # `turn["thinking"]` was structurally always empty - which made
        # `shape=reasoning_only` unreachable dead code and `thinking=0` a
        # measurement that could not fail. The provider's reasoning length is
        # recorded by _merge_chunk_shape and IS the real signal.
        provider_reasoning = sum(int(c.get("reasoning_len") or 0) for c in calls)
        provider_content = sum(int(c.get("content_len") or 0) for c in calls)
        reasoning_seen = bool(thinking) or provider_reasoning > 0
        if provider_content > 0:
            shape = "content_but_no_text"
        elif reasoning_seen:
            shape = "reasoning_only"
        else:
            shape = "nothing_at_all"
        _turn_log.info(
            "empty turn: shape=%s thinking=%d actions=%d chunks=%d len=%d "
            "model=%s pool=%s calls=%d failed=%s",
            shape, len(thinking), len(turn.get("actions") or []),
            int(turn.get("assistant_chunks") or 0),
            int(turn.get("assistant_len") or 0), model or "?", pool or "?",
            len(calls), bool(turn_failed),
        )
        # MEASUREMENT (2026-10-03): append to a journal that survives restarts.
        # friday.log is opened with mode="w" and is truncated on every app start,
        # so the 2026-10-02 evidence for this issue did not survive the night and
        # zero samples could accumulate. This record carries the provider's own
        # response shape so the next occurrence is diagnosable, not re-guessed.
        _append_turn_record({
            "event": "empty_turn",
            "shape": shape,
            "thinking": len(thinking),
            "provider_reasoning_len": provider_reasoning,
            "provider_content_len": provider_content,
            "actions": len(turn.get("actions") or []),
            "assistant_chunks": int(turn.get("assistant_chunks") or 0),
            "assistant_len": int(turn.get("assistant_len") or 0),
            "model": model or "",
            "pool": pool or "",
            # PHASE 4: recorded so a hard provider rejection (413 / ConnectError
            # / BadRequest) can never again be filed beside a genuinely empty
            # completion. Previously the only way to tell them apart was to go
            # read boot_console.log by hand.
            "turn_failed": bool(turn_failed),
            "turn_error_class": _turn_error_class(turn_error),
            "elapsed_ms": _turn_elapsed_ms(server_state),
            "turn_log_armed": turn is not None,
            # Per-LLM-call records, each with its own chunk count, field names,
            # finish reason and request metadata. Field NAMES, TYPES, LENGTHS
            # and hashes only - never content, keys, headers or payloads.
            "llm_calls": calls,
            "calls_this_turn": len(calls),
            # False means this turn made NO provider call at all, so any shape
            # recorded elsewhere would be stale. Proved necessary: the
            # 2026-10-02 18:59:23 record showed chunks=0 beside a named Modal
            # model precisely because the accumulator leaked across turns.
            "shape_valid": bool(rs.get("turn_valid")),
            "baseline": "absent",
        })
    except Exception as error:
        _turn_log.warning("empty-turn diagnostic failed: %s", type(error).__name__)


# Canonical failure categories for the empty-turn anomaly record. The point of
# this vocabulary is that ONE failure always produces ONE spelling, so a
# recurrence can be grouped by category instead of by exception class name.
_TURN_ERROR_STATUS_CATEGORIES = (
    (r"(?:code[:=]\s*|status[_ ]?code[:=]\s*|HTTP/\S+\s)(413|429)\b", "RateLimit"),
    (r"request too large", "RateLimit"),
    (r"(?:code[:=]\s*|status[_ ]?code[:=]\s*|HTTP/\S+\s)(401|403)\b", "Auth"),
    (r"(?:code[:=]\s*|status[_ ]?code[:=]\s*|HTTP/\S+\s)404\b", "NotFound"),
    (r"(?:code[:=]\s*|status[_ ]?code[:=]\s*|HTTP/\S+\s)(500|502|503)\b", "Server"),
)
_TURN_ERROR_NAME_CATEGORIES = (
    ("ConnectError", "ConnectError"),
    ("ConnectTimeout", "ConnectError"),
    ("ReadTimeout", "Timeout"),
    ("Timeout", "Timeout"),
    ("RateLimitError", "RateLimit"),
    ("BadRequestError", "BadRequest"),
    ("AuthenticationError", "Auth"),
    ("PermissionDenied", "Auth"),
    ("NotFoundError", "NotFound"),
    ("InternalServerError", "Server"),
    ("ServiceUnavailable", "Server"),
    ("BadGateway", "Server"),
    ("APIStatusError", "ProviderError"),
    ("APIError", "ProviderError"),
    ("OpenAIError", "ProviderError"),
    ("ContentPolicyViolation", "ProviderError"),
)


def _turn_error_class(text):
    """Classify a provider failure into a short, STABLE category.

    This is the field that separates "the provider rejected/failed the request"
    from "the provider answered and said nothing" - the two were previously
    indistinguishable in the anomaly journal and could only be told apart by
    hand-reading boot_console.log.

    Deliberately returns a CATEGORY, never the message: the message can echo
    the request, the organisation id, or a provider key.

    Rehearsed live 2026-10-03 against a real 413 whose class name is
    `litellm...OpenAIError` and whose final traceback line is a caret underline
    - which is why status codes are matched first and the fallback insists on an
    identifier-shaped name.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""
    # 1. Status codes are the most reliable signal: they appear in the message
    #    whatever the exception class happens to be called.
    for pattern, category in _TURN_ERROR_STATUS_CATEGORIES:
        if re.search(pattern, raw, flags=re.I):
            return category
    # 2. Known exception names, most specific first.
    for marker, category in _TURN_ERROR_NAME_CATEGORIES:
        if marker in raw:
            return category
    # 3. Fall back to the last identifier-shaped `module.Class: message` line.
    for line in reversed(raw.strip().splitlines()):
        match = re.match(r"\s*([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*:", line)
        if match:
            return match.group(1).split(".")[-1][:48]
    return "UnknownError"


def _flush_turn_log(server_state):
    turn = server_state.get("turn_log")
    server_state["turn_log"] = None
    if not turn:
        return None
    # The last tool call of a turn may never receive its `end` frame, so close
    # any still-buffered one here rather than losing it.
    _flush_tool_action(turn)
    actions = turn["actions"][:20]
    thinking = " ".join(turn["thinking"])
    grounded = server_state.get("knowledge_turn") is not None
    log_action(
        actor="assistant", risk="conversation", operation="assistant_turn",
        allowed=True, source=turn.get("source"),
        user=turn.get("user", ""),
        response="" if grounded else _redact_text(turn["assistant_snippet"])[:2000],
        response_len=turn["assistant_len"],
        thinking="" if grounded else _redact_text(thinking)[:1000],
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
    from .audio_startup import enabled as audio_startup_enabled

    if audio_startup_enabled():
        raise RuntimeError("Use the Light CLI for the device-free audio startup check")

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

    # OI re-renders the ENTIRE system prompt (9 {{}} kernel blocks, measured
    # 5.55s) before every LLM call. A tool turn makes 3 calls, so ~16s of a
    # 47s turn was re-rendering an unchanged template. Cache it per turn.
    # Reversible with FRIDAY_PROMPT_RENDER_CACHE=0.
    # NOTE: printed rather than _flog.info - file logging is only configured a
    # few lines below, so a logger call here silently lands nowhere.
    from . import prompt_render_cache as _prompt_cache

    try:
        if _prompt_cache.install():
            print("[prompt cache] system prompt rendered once per turn",
                  flush=True)
    except Exception as prompt_cache_error:
        print(f"[prompt cache] install failed: {prompt_cache_error}", flush=True)

    # ROUND ZB-APP: start the Future Lab world ticker HERE, not lazily from the
    # HUD. Measured 2026-11-03: after a restart ~/.friday/world.db never
    # advanced past tick 0 for six minutes, i.e. the lazy HUD path did not
    # start it - a simulation whose liveness depends on a browser polling is
    # not a simulation. Daemon thread, no shared server locks, and never raises
    # into startup.
    try:
        from . import lab_state as _lab_state
        if _lab_state.ensure_started():
            print("[world] future-lab ticker started (%s)" % _lab_state.db_path(),
                  flush=True)
        else:
            print("[world] future-lab ticker FAILED to start: %s"
                  % (_lab_state.health().get("start_error") or "unknown"),
                  flush=True)
    except Exception as lab_error:
        print(f"[world] future-lab start failed: {lab_error}", flush=True)

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
    # The profile's model at this point is the guaranteed local fallback.
    # BrainRouter chooses the model that actually serves each turn later, so a
    # generic `model=` label here falsely looked like proof that Qwen served.
    _flog.info(
        "starting host=%s port=%s voice=%s debug=%s local_fallback_model=%s",
        server_host, server_port, voice, debug, _model_label or "?",
    )
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
        # PHASE 3 (2026-10-03): requests waiting behind a running turn, oldest
        # first. One record per request, so nothing is last-writer-wins.
        "pending_turns": collections.deque(),
        "last_wake_time": 0.0,
        "last_wake_text": "",
        "barge_in": False,
        "single_turn_loop": None,
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
        "schedule_store": _ScheduleStore(),
        # Option 2 (2026-10-18): ONE immutable ownership record per turn. The
        # old `telegram_capture` dict + `telegram_turn_mute` bool pair is gone;
        # every reader goes through _ownership() / is_external / muted.
        "turn_ownership": LOCAL_OWNERSHIP,
        # Per-turn "has this turn produced audio yet" - drives exactly one
        # `tts_first_audio` marker per turn. Here rather than in a closure local
        # so traced_completions/on_tts_chunk stay extractable for tests.
        "tts_audio_seen": False,
        # ROUND ZB-I: explicit /chat lifecycle bookkeeping (queued/active/
        # completed/expired/cancelled). Bounded, so a flood of callers cannot
        # grow it; keyed by opaque reply_token, never by the caller's words.
        "chat_requests": LifecycleRegistry(max_held=64),
        "turn_attempt": None,
        "turn_log": None,
        # ROUND Q (2026-10-03): a tool-cap nudge waiting for the next LLM call.
        # Held here rather than appended to interpreter.messages at the moment
        # the cap fires, because that position orphans the pending tool result
        # (see traced_completions). Flushed on the next llm call, discarded if
        # the turn ends first.
        "pending_tool_cap_nudge": None,
        "tool_cap_nudged": False,
        "last_user_text": "",
        "last_progress": time.monotonic(),  # fresh boot; watchdog starts clean
    }
    interpreter.server_state = server_state
    from .companion import for_runtime as _companion_for
    companion = _companion_for(interpreter, create=True)
    from .personality import Personality
    personality = interpreter.personality = Personality(companion)
    audio_link = server_state["audio_link"] = AudioLink(server_state)
    archive = interpreter.light_archive = ConversationArchive(archive_path())
    archive.recover()
    knowledge_store = KnowledgeStore()
    # ROUND ZB-CH: turn/update-aware speech queue (speech_queue.py). Held on
    # server_state rather than a module global for test isolation, and created
    # HERE so the playback branch never needs a relative import - some suites
    # extract that branch and exec it in a bare namespace where `from .x import
    # y` raises. Absent queue is treated as "no policy" by the readers.
    from .speech_queue import SpeechQueue as _SpeechQueue

    server_state["speech_queue"] = _SpeechQueue()
    server_state["brain_router"] = BrainRouter(enabled=api_pools.cloud_brain_enabled())
    startup_identity = getattr(interpreter, "friday_startup_identity", None)
    if startup_identity is not None:
        server_state["identity_integration"] = startup_identity
        server_state["brain_router"].identity_integration = startup_identity
    elif os.environ.get("FRIDAY_IDENTITY_INTEGRATION") == "1":
        from . import screen_understanding as _identity_vision
        server_state["identity_integration"] = _identity.from_environment(
            _brain_router_module, _identity_vision)
        server_state["brain_router"].identity_integration = server_state["identity_integration"]
        if server_state["identity_integration"] is None:
            _flog.warning("Identity integration inactive: inventory or stores unavailable")
    from . import lab_state as _task_lab_state
    _task_lab_state.configure_execution_identity(server_state.get("identity_integration"))

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

    def trace(*args, **detail):
        # CONTRACT (2026-11-06): the event name comes POSITIONALLY, and a
        # caller may also hand over a whole payload dict ({"event": ...}) or an
        # `event=` keyword. The old receiver took `event` as a named parameter,
        # so a positional name PLUS that keyword raised TypeError at the CALL
        # site - "got multiple values for argument 'event'" - and that one
        # collision produced 498 "input error" lines plus a state=error storm
        # in a single afternoon. A tolerant *args receiver makes every call
        # shape bindable, and the whole body is best-effort: telemetry must
        # never break the pipeline it observes.
        try:
            if len(args) == 1 and isinstance(args[0], dict):
                _payload = dict(args[0])
                event = str(_payload.pop("event", "event"))
                for _key, _value in _payload.items():
                    detail.setdefault(_key, _value)
            elif args:
                event = args[0]
            else:
                event = detail.pop("event", "event")
            _flog.debug(event)
            # ROUND ZB-VP: also timestamp the event into the per-turn voice trace so
            # "which stage was slow?" is answerable from evidence instead of guesswork.
            try:
                _voice_trace_get().record(event, detail or None)
            except Exception:
                pass
            if debug:
                elapsed = time.monotonic() - trace_start
                print(f"[trace +{elapsed:.3f}s] server {event}", flush=True)
        except Exception:
            # A failing telemetry sink is logged nowhere on purpose: the log
            # sink itself may be what failed. Swallowing here is the fix.
            pass

    def _tts_stage_now():
        """Which TTS stage actually served the last utterance (cloud/local/none)."""
        try:
            return str(getattr(engine, "LAST_STAGE", "") or "")
        except Exception:
            return ""

    def publish_status(state, detail=""):
        _flog.info("state=%s detail=%s", state, detail if detail else "-")
        if state == STATUS_THINKING and not server_state.get("pulse_active"):
            server_state["pulse_active"] = True
            server_state["pulse_started_at"] = time.monotonic()
        elif state != STATUS_THINKING:
            server_state["pulse_active"] = False
        if status_bus is not None:
            status_bus.publish(state, detail)

    def close_hot_window(reason, *, expected_deadline=None):
        with server_state["hot_window_lock"]:
            if not server_state["hot_window_active"]:
                return
            # A canceled timer may already be waiting on this lock. Only the
            # timer for the current window may expire it after a refresh.
            if expected_deadline is not None and (
                server_state["hot_window_expires_at"] != expected_deadline
            ):
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
            old_timer = server_state["hot_window_timer"]
            if old_timer is not None:
                old_timer.cancel()
            server_state["hot_window_active"] = True
            server_state["hot_window_opened_at"] = now
            server_state["hot_window_expires_at"] = now + server_state["hot_window_seconds"]
            timer = threading.Timer(
                server_state["hot_window_seconds"], close_hot_window,
                args=("expired",),
                kwargs={"expected_deadline": server_state["hot_window_expires_at"]},
            )
            timer.daemon = True
            server_state["hot_window_timer"] = timer
            timer.start()
        trace(f"hot_window_open seconds={server_state['hot_window_seconds']:.1f}")
        publish_status(STATUS_LISTENING, f"Follow-up window open ({server_state['hot_window_seconds']:.1f}s)")
        return True

    # --- ROUND ZB-PE: parked questions only Sir can answer -----------------
    #
    # WHY. n8n's workflow asked a question only the owner can decide, and FRIDAY
    # answered the WORKFLOW with the question - handing back what it already
    # knew and telling it nothing. The question must be parked, asked of Sir,
    # and held open until he answers, ignores it, or it is given up on.
    #
    # WHY ASYNC. Holding the HTTP request open for two minutes, then five, is
    # not reliable (proxies, n8n timeouts, a restart). So `/n8n` ACKNOWLEDGES
    # immediately with a correlation id and this worker does the waiting. n8n
    # learns the outcome either by polling `GET /n8n/pending/<id>` or by FRIDAY
    # calling its `pending_answer` webhook.
    from .pending_requests import (
        PendingStore, get_pending_store, STAGE_LOCAL, STAGE_TELEGRAM, STAGE_PENDING,
        LOCAL_WAIT_SECONDS, TELEGRAM_WAIT_SECONDS,
        decide_owner_request, is_owner_decision_command, next_unreminded, resolve_owner_decision,
    )

    # Registered process-wide so friday_pending() (the brain's read-only view)
    # and this server share ONE store rather than two writers on one file.
    pending_store = get_pending_store(PendingStore())
    server_state["pending_store"] = pending_store

    def _pending_prompt(request):
        return (f"An n8n request needs your decision: {request.text} "
                f"Use n8n decision {request.id}: followed by your answer or ignore.")

    def _pending_reminder(request):
        return f"Still awaiting your decision. {_pending_prompt(request)}"

    def _notify_pending(request, status, answer=""):
        """Tell the workflow the outcome. Best effort - never raises, never lies.

        Returns the adapter's result, or a dict saying it was skipped. Skipped
        is normal: until a `pending_answer` workflow exists, the polling
        endpoint is the answer and this costs nothing.
        """
        try:
            from .n8n_runtime import get_adapter as _adapter
            adapter = _adapter()
            if not adapter.config.get("enabled"):
                return {"ok": False, "skipped": True, "error": "n8n not configured"}
            return adapter.call("pending_answer", {
                "correlation_id": request.id,
                "status": status,
                "question": request.text,
                "answer": answer,
                "kind": request.kind,
                "payload": request.payload,
            })
        except Exception as error:
            _flog.warning("pending notify failed: %s", type(error).__name__)
            return {"ok": False, "skipped": True, "error": type(error).__name__}

    async def _wait_for_pending_answer(correlation_id, seconds):
        """Poll the store until the request leaves `open`, or time runs out.

        Polling rather than an event: the voice path runs on another thread, and
        a one-second poll costs nothing while staying free of cross-thread
        plumbing and of any held HTTP request.
        """
        deadline = time.monotonic() + max(1.0, float(seconds))
        while time.monotonic() < deadline:
            request = pending_store.get(correlation_id)
            if request is None or request.state != "open":
                return True
            await asyncio.sleep(1.0)
        return False

    def _owner_chat_id():
        """Sir's Telegram chat, read from the same place the bot already uses."""
        try:
            from .remote_telegram import load_config as _tg_config
            config = _tg_config()
            for chat_id in (config.allowed_chat_ids or []):
                value = str(chat_id or "").strip()
                if value and not value.startswith(TEXT_CHANNEL_CHAT_ID):
                    return value
        except Exception as error:
            _flog.debug("pending telegram chat lookup failed: %s",
                        type(error).__name__)
        return ""

    async def _promote_pending_to_telegram(request):
        """Ask the same question on Telegram. Returns True when it was sent."""
        chat_id = _owner_chat_id()
        if not chat_id:
            return False
        try:
            await telegram_adapter.send_text(
                chat_id,
                _pending_prompt(request))
            return True
        except Exception as error:
            _flog.warning("pending telegram promotion failed: %s",
                          type(error).__name__)
            return False

    async def _pending_worker(correlation_id):
        """Ask Sir, wait, promote, park. Never holds an HTTP request open."""
        try:
            request = pending_store.get(correlation_id)
            if request is None or not request.is_open():
                return
            trace(f"pending_start id={correlation_id} stage={request.stage}")
            queue_direct_response(_pending_prompt(request))
            open_hot_window()
            if await _wait_for_pending_answer(correlation_id, LOCAL_WAIT_SECONDS):
                trace(f"pending_resolved id={correlation_id} stage=local")
                return
            if not pending_store.advance(correlation_id, STAGE_TELEGRAM):
                return
            telegram_sent = await _promote_pending_to_telegram(request)
            if await _wait_for_pending_answer(correlation_id, TELEGRAM_WAIT_SECONDS):
                trace(f"pending_resolved id={correlation_id} stage=telegram")
                return
            if not pending_store.advance(correlation_id, STAGE_PENDING):
                return
            result = _notify_pending(request, status="pending")
            _flog.info("pending %s parked (telegram_sent=%s callback_http=%s)",
                       correlation_id, telegram_sent, result.get("http_status"))
        except Exception as error:
            _flog.warning("pending worker failed: %s", type(error).__name__)

    def _pending_reminder_if_any():
        """One reminder, once, the next time Sir is at the PC.

        `next_unreminded` is the duplicate guard - it returns a request only
        while it has not already nagged, so a parked question cannot interrupt
        every turn. The flag is persisted, so a restart does not restart the
        nagging either.
        """
        request = next_unreminded(pending_store)
        if request is None:
            return None
        request.reminded = True
        try:
            pending_store.save()
        except Exception as error:
            _flog.warning("pending reminder flag not saved: %s", type(error).__name__)
        trace(f"pending_reminder id={request.id}")
        return _pending_reminder(request)

    def _pending_resolve(text, source):
        return resolve_owner_decision(
            pending_store, text, source=source, notify=_notify_pending,
        )

    def _hud_pending_decision(correlation_id, answer):
        if server_state.get("is_paused"):
            return {"ok": False, "message": "FRIDAY is paused. Your decision was not sent."}
        return decide_owner_request(pending_store, correlation_id, answer,
                                    source="text", notify=_notify_pending)

    interpreter.submit_pending_decision = _hud_pending_decision



    def response_is_active():
        respond_thread = getattr(interpreter, "respond_thread", None)
        return respond_thread is not None and respond_thread.is_alive()

    def turn_is_busy():
        # The worker can exit before its completion is read, and deterministic
        # replies never have a worker. Both still own the output lifecycle.
        return _ownership(server_state) is not LOCAL_OWNERSHIP or response_is_active()

    background_life = _task_lab_state.configure_background_life(
        server_state.get("identity_integration") if os.environ.get("FRIDAY_IDENTITY_READER") == "1" else None,
        lambda: (turn_is_busy() or _pending_has(server_state)
                 or time.monotonic() < server_state.get("background_voice_until", 0)),
    )
    if background_life is not None:
        server_state["invalidate_background_life"] = background_life.invalidate

    def _ensure_owned_queue():
        if not isinstance(interpreter.output_queue, _OwnedOutputQueue):
            if interpreter.output_queue is None:
                import janus

                interpreter.output_queue = janus.Queue()
            interpreter.output_queue = _OwnedOutputQueue(interpreter.output_queue)

    old_respond = interpreter.respond
    execution_lock = RLock()
    interpreter.companion_execution_lock = execution_lock
    execution_gate = None

    def respond_with_attempt(self, *args, **kwargs):
        attempt = server_state.get("turn_attempt")
        if execution_gate is not None:
            attempt = execution_gate.output_attempt()
        token = _producing_attempt.set(attempt)
        try:
            with _identity.scoped(server_state, attempt):
                return old_respond(*args, **kwargs)
        finally:
            _producing_attempt.reset(token)

    interpreter.respond = types.MethodType(respond_with_attempt, interpreter)

    def _activate_owner(ownership):
        _ensure_owned_queue()
        with execution_lock:
            if background_life is not None:
                background_life.invalidate()
            companion.clear_overlay()
            companion.discard_pending()
            history_cut = server_state.pop("knowledge_history_cut", None)
            if history_cut is not None:
                del interpreter.messages[history_cut:]
            _install_ownership(server_state, ownership)
            server_state.update(turn_attempt=object(), cancelled_owner=None,
                                terminal_notice_owner=None, last_direct_text=None,
                                closing_attempt=None, tts_audio_seen=False,
                                turn_failed=False, turn_error="", provider_attempt=False,
                                tool_cap_terminated=False, knowledge_turn=None,
                                knowledge_read_failed=False)
            _identity.admit(server_state, ownership)
            trace("turn_admitted")
        _stamp_progress(server_state, turn_started=True)

    def _put_owned(frame):
        interpreter.output_queue.sync_q.put(_AttemptOutput(server_state["turn_attempt"], frame))

    def _speech_playback(self):
        owner = _ownership(server_state)
        generation = server_state.get("turn_attempt")
        playback = server_state.get("playback_attempt")
        if (playback is None or server_state.get("playback_owner") is not owner
                or server_state.get("playback_generation") is not generation):
            if server_state.pop("discarded_synthesis", False) and self.tts.is_playing():
                self.tts.stop()
            if playback is not None:
                if self.tts.is_playing():
                    self.tts.stop()
                notify_speech_stop(server_state, "new_speech", queue=self.output_queue)
            playback = object()
            server_state.update(playback_attempt=playback, playback_owner=owner,
                                playback_generation=generation)
            audio_link.begin(playback)
        return playback

    def _start_owned_playback(self, **kwargs):
        playback = _speech_playback(self)
        previous = getattr(self.tts, "play_thread", None)
        self.tts.play_async(on_audio_chunk=lambda chunk: self.on_tts_chunk(chunk, playback), **kwargs)
        worker = getattr(self.tts, "play_thread", None)
        if worker is not previous or worker is None:
            audio_link.watch(playback, worker, lambda frame: self.output_queue.sync_q.put(
                _AttemptOutput(playback, frame, playback=True)))

    def _seal_owned_playback(self):
        playback = server_state.get("playback_attempt")
        if (playback is not None and server_state.get("playback_owner") is _ownership(server_state)
                and server_state.get("playback_generation") is server_state.get("turn_attempt")):
            audio_link.finish_synthesis(playback, lambda frame: self.output_queue.sync_q.put(
                _AttemptOutput(playback, frame, playback=True)))

    def _set_voice_input(paused):
        with server_state["hot_window_lock"]:
            return audio_link.set_voice(paused, interpreter.output_queue.sync_q)

    async def _feed_turn(content, owner, *, retry=False):
        # A provider retry belongs to the same request, but gets a new output
        # generation. Late frames from the prior provider cannot complete it.
        if retry:
            with execution_lock:
                companion.clear_overlay(owner)
                companion.discard_pending()
                server_state["turn_attempt"] = object()
                _identity.advance(server_state, owner)
                personality.retry(owner, server_state["turn_attempt"])
            if archive is not None:
                archive.retry(owner, server_state["turn_attempt"])
        await old_input({"role": "user", "type": "message", "start": True})
        if server_state.get("cancelled_owner") is owner:
            _put_owned({"role": "server", "type": "status", "content": "complete"})
            return
        await old_input({"role": "user", "type": "message", "content": content})
        if server_state.get("cancelled_owner") is owner:
            _put_owned({"role": "server", "type": "status", "content": "complete"})
            return
        server_state["provider_attempt"] = True
        await _dispatch_provider_end(old_input, server_state, interpreter)

    def _cancel_turn(owner, reason):
        with execution_lock:
            if _ownership(server_state) is not owner:
                return False
            server_state["voice_capture_epoch"] = server_state.get("voice_capture_epoch", 0) + 1
            # A fresh Stop from the current owner also dismisses a completed
            # turn's lingering mark. A stale caller already returned above.
            companion.clear_overlay()
            companion.discard_pending()
            active = owner is not LOCAL_OWNERSHIP
            personality.stop(owner, idle=owner is LOCAL_OWNERSHIP)
            if not active and server_state.get("playback_attempt") is None:
                return False
            if active:
                server_state["cancelled_owner"] = owner
                mcp = server_state.get("mcp_turn")
                proposal = mcp.pending if mcp is not None else None
                if proposal is not None and proposal.owner is owner and proposal.dispatched:
                    chat_append("tool", "MCP stopped locally. Remote outcome unknown.")
                _identity.close(server_state, owner)
                server_state["barge_in"] = True
                if response_is_active():
                    try:
                        interpreter.stop_event.set()
                    except Exception as error:
                        _flog.warning("turn stop signal failed: %s", type(error).__name__)
                        active = False
        notify_speech_stop(server_state, reason, queue=interpreter.output_queue)
        companion.clear_overlay(owner)
        if active and archive is not None:
            archive.mark(owner, "cancelled")
        try:
            interpreter.tts.stop()
        except Exception:
            pass
        return active

    def _cancel_requests(owner, reason):
        if _ownership(server_state) is not owner:
            return False, 0
        registry = server_state.get("chat_requests")
        count = 0
        if registry is not None:
            try:
                count = int(registry.cancel_all())
            except Exception as error:
                _flog.warning("request lifecycle cancellation failed: %s", type(error).__name__)
        return _cancel_turn(owner, reason), count

    def _cancel_http_owner(reply_token):
        owner = _ownership(server_state)
        if owner.reply_token == reply_token:
            _cancel_turn(owner, "http_caller_cancelled")

    def stop_turn(reason):
        owner = _ownership(server_state)
        loop = server_state.get("event_loop")
        if loop is None or not loop.is_running():
            return False, 0
        try:
            in_server_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            in_server_loop = False
        if in_server_loop:
            return _cancel_requests(owner, reason)

        async def stop():
            return _cancel_requests(owner, reason)

        return asyncio.run_coroutine_threadsafe(stop(), loop).result(timeout=2.0)

    interpreter.cancel_owned_turn = stop_turn

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
        # `_friday_authored` marks frames FRIDAY herself queued: the post-cap
        # drain in new_output keeps these and discards only Open Interpreter's
        # residue, so a terminal narration can never be swallowed and the
        # model's killed-turn text can never be spoken twice.
        _put_owned({
            "role": "assistant",
            "type": "message",
            "content": text,
            "_friday_authored": True,
        })
        _put_owned({
            "role": "assistant",
            "type": "message",
            "end": True,
            "_friday_authored": True,
        })

    def queue_direct_response(content, *, record_activity=True, admitted=False):
        """Send deterministic adapter output through the normal response/TTS queue."""
        text = str(content or "")
        if not admitted:
            loop = server_state.get("event_loop")
            try:
                in_server_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                in_server_loop = False
            if not in_server_loop:
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(lambda: queue_direct_response(
                        text, record_activity=record_activity))
                return
            if turn_is_busy():
                if len(server_state["pending_turns"]) < _PENDING_QUEUE_MAX:
                    _pending_push(server_state, user_text="", direct_response=text)
                return
            _activate_owner(_request_owner())
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
        # A media-only reply still needs a completion to release admission.
        if text.strip():
            queue_speech_only(text)
        _put_owned({
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
                # Bound the journal: expires_at was NULL on every row, so it
                # only ever grew. Soft-prune the oldest (recoverable).
                server_state["memory_store"].prune_topic("activity", keep=400)
        except Exception as error:
            _flog.warning("activity journal failed: %s", error)

    def log_direct(actor, operation, outcome, spoken, *, risk="read_only", **extra):
        """Audit a deterministic adapter reply: what was said and why."""
        log_action(actor=actor, risk=risk, operation=operation, allowed=True,
                   outcome=outcome, spoken=_redact_text(spoken)[:400], **extra)

    server_state["local_stt_model"] = "base.en"
    interpreter.stt = AudioToTextRecorder(
        model=server_state["local_stt_model"],
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
    interpreter.tts = TextToAudioStream(
        engine,
        # ROUND ZB-VP: playback-finished marker. Without it tts_start is the last
        # event a turn ever emits, so "she stopped mid-sentence" is
        # indistinguishable from "she finished the sentence" in the logs - the
        # exact case we cannot diagnose. RealtimeTTS invokes this with no args
        # once the stream ends (realtime_tts/text_to_stream.py:313 and :471).
        # Best-effort via trace(), so a failure here can never break playback.
        on_audio_stream_stop=lambda: trace("tts_end", tts_stage=_tts_stage_now()),
    )
    # ROUND ZB-REC: keep the engine reachable so the playback sites can ask
    # "is every TTS stage dead?" before feeding the stream, and so a stuck
    # playback latch can be recovered. Only CloudFirstTTSEngine implements the
    # health API; other engines simply report healthy.
    server_state["tts_engine"] = engine

    reminder_worker = None

    from pathlib import Path as _Path

    desktop_ear = DesktopEar()
    desktop_ear.start()
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

    # 2026-10-02: near-real-time desktop awareness, layered cheapest-first so a
    # screen question never pays for pixels it does not need:
    #   1. window_events        - foreground / minimized / opened. No pixels.
    #   2. ui_probe             - an app's accessibility tree, read-only.
    #   3. screen_capture_service - one always-live frame per monitor, so the
    #                              existing capture path reads memory instead of
    #                              re-grabbing (51 ms per monitor -> ~2 ms).
    # Both are strictly advisory: if either fails to start, everything falls
    # back to the previous capture and router behaviour, unchanged.
    try:
        from . import window_events as _window_events

        _window_events.start()
        server_state["window_events"] = _window_events.tracker()
    except Exception as _error:
        _flog.info("window events unavailable: %s", type(_error).__name__)
    try:
        from . import screen_capture_service as _capture_service

        _capture_health = _capture_service.start()
        server_state["screen_capture"] = _capture_service.service()
        _flog.info("screen capture: %s", _capture_health.get("reason"))
    except Exception as _error:
        _flog.info("screen capture unavailable (%s); using Pillow capture",
                   type(_error).__name__)

    def _quiet_speak(message):
        if social_guard.active():
            _flog.info("proactive: call is active, staying silent")
            return
        # Ambient suggestions never become speech or an admitted action. The
        # return-to-PC event is only a chance to recheck already parked decisions.
        for request in pending_store.open_requests():
            personality.suggest("n8n_decision", request.id, current=True,
                live=lambda request=request: pending_store.get(request.id) is request
                and request.is_open() and not request.decision_deadline_passed())

    welcome_back = WelcomeBackMonitor(
        _quiet_speak,
        software_root=_Path(__file__).resolve().parents[2],
        flog=_flog,
        allowed=lambda: personality.preferences().get("values", {}).get("proactive_suggestions") is True,
        diagnostics=False,
    )
    welcome_back.start()
    interpreter.welcome_back = welcome_back

    # ROUND ZB-ACT: a MACHINE takes notes about what the PC is doing - no model
    # involved, so it cannot confabulate and costs almost nothing. Rolling
    # five-minute window written to ~/.friday/activity_journal.md. Answers "what
    # was I doing before this?" from a record instead of from memory.
    from .activity_journal import get_journal as _activity_journal
    _activity = _activity_journal()
    _activity_started = False
    try:
        _activity_started = _activity.start()
    except Exception as activity_error:
        _flog.warning("activity journal not started: %s",
                      type(activity_error).__name__)
    interpreter.activity_journal = _activity

    # ROUND ZB-SC: "FRIDAY, help me in this game" - a bounded watcher that
    # captures the screen, makes ONE vision call per cycle, and speaks a single
    # useful line when there is something worth saying. Never blocks the voice
    # path (it only queues a line), never repeats itself, and stops after its
    # window or after repeated failures. The brain decides when to start it -
    # it is not phrase-triggered.
    from .screen_companion import get_companion as _get_companion
    try:
        interpreter.screen_companion = _get_companion(speak=queue_direct_response)
    except Exception as companion_error:
        _flog.warning("screen companion not wired: %s", type(companion_error).__name__)
        interpreter.screen_companion = None

    _flog.info("background systems: ear=%s guard=%s focus=%s welcome=%s journal=%s companion=%s",
               desktop_ear.active, social_guard.active(), bool(focus_tracker),
               bool(welcome_back), _activity_started,
               bool(getattr(interpreter, "screen_companion", None)))

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
    # NB: the per-turn audio marker lives on `server_state["tts_audio_seen"]`,
    # not in a local here - a closure local could not be re-armed by
    # traced_completions without `nonlocal`, which breaks every suite that
    # compiles that function in isolation.
    tool_code_parts = []
    tool_counter = {"n": 0}
    # MEASUREMENT (2026-10-03): per-tool start stamp. `None` means no tool is
    # currently in flight, so the first captured script opens the window and
    # the completion line closes it.
    tool_timing = {"started": None}
    SERVER_TOOL_CAP = [
        float(os.getenv("FRIDAY_TOOL_CAP", "12"))
    ]
    # Hard ceiling: the soft nudge above is advisory (models ignore it). If a
    # turn keeps dry-running tools past this count, kill the agent turn outright
    # instead of letting it loop for dozens of executions (Roblox loop: 91 runs).
    HARD_TOOL_CAP = int(float(os.getenv("FRIDAY_HARD_TOOL_CAP", "24")))
    try:
        from interpreter.core.friday_execution import ExecutionDenied, ExecutionGate, validate_runtime

        if not validate_runtime():
            raise RuntimeError("Incomplete Open Interpreter execution hooks")
        class LightExecutionGate(ExecutionGate):
            def grant(self, language, code, row):
                knowledge = self.state.get("knowledge_turn")
                if knowledge is not None and knowledge.owner is _ownership(self.state):
                    raise ExecutionDenied("Imported-source answers cannot execute tools")
                return super().grant(language, code, row)

        execution_gate = LightExecutionGate(interpreter, server_state, execution_lock, HARD_TOOL_CAP)
    except (ImportError, AttributeError, RuntimeError) as error:
        _flog.error("Tool execution disabled: install the bundled FRIDAY Open Interpreter wheel (%s)",
                    type(error).__name__)

        def refuse_unpatched_execution(*args, **kwargs):
            raise RuntimeError("Tool execution unavailable. Install the bundled FRIDAY Open Interpreter wheel.")

        interpreter.computer.run = refuse_unpatched_execution
        interpreter.computer.terminal.run = refuse_unpatched_execution

    # RUNAWAY-SCRIPT GUARD, part 1 of 2: repetition, not volume.
    # The tool CAP above only counts code blocks that emit a `start` event. A
    # single script with an unbounded loop never ends, never increments that
    # counter, and therefore defeats the cap entirely - the turn hangs forever
    # and FRIDAY never replies (measured: generated python looping forever
    # because Open Interpreter never emitted an `end` event).
    #
    # A real runaway prints the SAME thing over and over; real work varies. An
    # earlier volume-based version killed a legitimate vision turn and FRIDAY
    # then reported a confident falsehood ("the code I wrote started looping").
    # So this trips only on N identical consecutive console lines.
    TOOL_FLOOD_EVENT_CAP = int(float(os.getenv("FRIDAY_TOOL_FLOOD_CAP", "300")))

    # FRUITLESS SCREEN-SEARCH BUDGET - ROUND ZB-FS (2026-11-03).
    #
    # Counted PER TURN over console lines that report an EMPTY screen search
    # (see `search_miss_target`). Reaching the cap means the same lookup has
    # already failed this many times, so one more attempt cannot add anything;
    # the turn is stopped and Sir gets the truthful answer instead of silence.
    #
    # 4 rather than a larger number: on the turn this was written for the model
    # produced its 4th empty result about 100 s in, against a 330 s+ runaway
    # whose only backstops were the 24-tool hard cap and the 600 s wall clock.
    # It is deliberately NOT a per-tool or per-step limit - a turn that merely
    # checks several different absent strings once each is not this failure, and
    # a search that finally HITS never increments the counter, so a late match
    # after three misses still succeeds.
    SEARCH_MISS_CAP = int(float(os.getenv("FRIDAY_SEARCH_MISS_CAP", "4")))

    # TURN WALL-CLOCK BUDGET - NOT a runaway-script defence. ROUND W (2026-10-04).
    #
    # This used to be called TOOL_WALL_CLOCK_LIMIT and documented as catching a
    # runaway script. It never did that job. It was evaluated ONLY inside the
    # console-output handler, so it was simultaneously:
    #   * too WEAK - a script that hangs while printing nothing never reached the
    #     check at all, which is the exact failure it was written for; and
    #   * too BLUNT - a healthy turn killed purely on cumulative elapsed time.
    # Measured live 2026-10-04 13:53:26: a 247 s turn of 12 tools, each
    # completing in 0-77 ms with every Modal call returning in ~5-10 s, was
    # killed and FRIDAY told Sir "the code was looping instead of finishing" -
    # false. Nothing was looping.
    #
    # It is now what it always was: an overall TURN budget, finite on purpose,
    # with runaway detection delegated to TOOL_FLOOD_EVENT_CAP / HARD_TOOL_CAP
    # and to the silent-hang check below.
    TURN_WALL_CLOCK_LIMIT = float(os.getenv("FRIDAY_TURN_WALL_CLOCK", "600"))
    if execution_gate is not None:
        try:
            from .mcp_runtime import MCPRuntime
            from .mcp_turn import MCPTurn

            runtime = MCPRuntime()
            server_state["mcp_runtime"] = runtime
            server_state["mcp_turn"] = MCPTurn(runtime)
        except Exception as error:
            server_state["mcp_startup_failed"] = True
            _flog.warning("MCP configuration unavailable (%s)", type(error).__name__)

    def mcp_live(item):
        return (execution_gate is not None and _ownership(server_state) is item.owner
                and not item.owner.is_external and item.generation is server_state.get("turn_attempt")
                and server_state.get("cancelled_owner") is not item.owner
                and not interpreter.stop_event.is_set() and not server_state.get("barge_in")
                and not server_state.get("tool_cap_terminated")
                and not server_state.get("turn_budget_enforced")
                and server_state.get("knowledge_turn") is None
                and time.monotonic() < item.deadline)

    def spend_mcp(item):
        with execution_lock:
            if not mcp_live(item):
                return False
            worker = execution_gate.bound
            if (worker is None or worker.owner is not item.owner or worker.attempt is not item.generation
                    or worker.stopped or not execution_gate.owns(worker)):
                return False
            if execution_gate.budget_owner is not item.owner:
                execution_gate.budget_owner, execution_gate.used = item.owner, 0
            if execution_gate.used >= HARD_TOOL_CAP or tool_counter["n"] >= HARD_TOOL_CAP:
                return False
            execution_gate.used += 1
            tool_counter["n"] += 1
            _stamp_progress(server_state)
            return True

    def mcp_decision(proposal_id, approve):
        controller = server_state.get("mcp_turn")
        with execution_lock:
            ok = controller is not None and controller.decide(proposal_id, approve, mcp_live)
        return {"ok": ok}

    interpreter.decide_mcp_proposal = mcp_decision

    # Opt-in webhook counterpart. Passive construction; no startup configuration writes.
    from .n8n_bridge import for_runtime as _workflow_bridge
    from .n8n_turn import WorkflowTurn
    workflow_bridge = _workflow_bridge(interpreter)
    workflow_bridge.runtime_live = lambda: not interpreter.stop_event.is_set()
    workflow_turn = WorkflowTurn(workflow_bridge)
    server_state["n8n_turn"] = workflow_turn

    def decide_workflow_request(proposal_id, choice):
        with execution_lock:
            return workflow_turn.decide(proposal_id, choice, mcp_live)

    interpreter.decide_workflow_request = decide_workflow_request

    def run_workflow_request(arguments):
        owner, generation = _ownership(server_state), _producing_attempt.get()
        loop = server_state.get("event_loop")
        if generation is None or owner.is_external or loop is None or not loop.is_running():
            return "n8n refused: no eligible local admitted request."
        future = asyncio.run_coroutine_threadsafe(workflow_turn.execute(
            owner, generation, arguments, server_state.get("turn_started_at", 0) + TURN_WALL_CLOCK_LIMIT,
            mcp_live, spend_mcp), loop)
        try:
            result = future.result(timeout=47)
            # Only a bounded visible reply is displayed. Raw remote payloads
            # never become trusted model instructions, code or action permission.
            if (result.get("ok") and _ownership(server_state) is owner
                    and generation is server_state.get("turn_attempt")
                    and server_state.get("cancelled_owner") is not owner
                    and not interpreter.stop_event.is_set()):
                from .n8n_bridge import safe_notice
                text = (result.get("result") or {}).get("text")
                if isinstance(text, str) and text:
                    chat_append("tool", "n8n reply (untrusted):\n" + safe_notice(
                        text, workflow_bridge.config.read()["secret"]))
            return "n8n request outcome: " + str(result.get("outcome") or result.get("error") or "unknown") + ". Check Settings n8n inbox; sending does not approve a write."
        except Exception:
            future.cancel()
            workflow_bridge.invalidate()
            return "n8n wait interrupted. If dispatched, remote outcome is unknown; do not repeat automatically."


    def run_mcp_proposal(proposal):
        controller = server_state.get("mcp_turn")
        owner, generation = _ownership(server_state), _producing_attempt.get()
        loop = server_state.get("event_loop")
        if proposal is False:
            return "MCP proposal refused: incomplete, unsupported or invalid tool call."
        if generation is None or owner.is_external or loop is None or not loop.is_running():
            return "MCP proposal refused: no eligible local request."
        deadline = server_state.get("turn_started_at", 0) + TURN_WALL_CLOCK_LIMIT

        def report(item, result):
            from .capability_inventory import text as safe_metadata
            from .redaction import redact

            if mcp_live(item):
                chat_append("tool", "MCP tool output (untrusted):\n" +
                            safe_metadata(redact(result, force=True), 8000))

        future = asyncio.run_coroutine_threadsafe(
            controller.execute(owner, generation, *proposal, deadline, mcp_live, spend_mcp, report), loop)
        try:
            return future.result(timeout=47)
        except Exception:
            future.cancel()
            return "MCP request interrupted. If dispatched, the remote outcome is unknown."

    server_state["run_mcp_proposal"] = run_mcp_proposal
    # Backwards-compatible alias: the old name is still read so an existing
    # FRIDAY_TURN_WALL_CLOCK override or a stale reference keeps working, but
    # nothing in FRIDAY uses the old name any more.
    TOOL_WALL_CLOCK_LIMIT = TURN_WALL_CLOCK_LIMIT

    # SILENT-HANG BUDGET (ROUND W). A turn that produces NO output at all and NO
    # interpreter output can still be wedged - an OI stream that never completes,
    # a kernel call that never returns. The old check could not see that case at
    # all, because it only ran inside the console-output handler.
    #
    # This is deliberately SHORTER than the turn budget, and is checked FIRST.
    # It has to be: a limit longer than the turn budget would be unreachable,
    # because the turn budget would always fire first. Its job is to end a
    # silently wedged turn PROMPTLY rather than making Sir wait out the whole
    # budget for nothing.
    #
    # Once ANY interpreter output has been seen this can never fire
    # (turn_progress_observed), so a slow-but-working turn is unaffected - it
    # gets the full turn budget instead.
    SILENT_HANG_LIMIT = float(os.getenv("FRIDAY_SILENT_HANG_LIMIT", "300"))

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
        # persists after tool activity, allowing only ONE duplicate text-only
        # call (LOOP_GUARD_CAP below) before the merge is skipped. Tool steps
        # have computer:console last, so they never count against the cap.
        llm_loop_guard = {"calls": 0}
        # ROUND O (2026-10-03): 2 -> 1. Measured on boot_console.log: 141 of259
        # reconstructable turns (54%) tripped this guard, and those turns ran a
        # median 55.0s against 26.0s unguarded. OI's respond.py else-branch
        # (reached only when the model emitted NO code) re-asks the LLM when the
        # last assistant message lacks one of its four loop_breakers, so a
        # normal conversational answer always drew a duplicate streamed call.
        # At cap=2 BOTH duplicates streamed (call 2 and call 3 was skipped);
        # at cap=1 the FIRST duplicate is skipped instead. Tool/code chaining is
        # untouched: those steps leave computer:console last, which fails the
        # `_assistant_text` half of the condition and never reaches this branch.
        LOOP_GUARD_CAP = int(float(os.getenv("FRIDAY_LLM_LOOP_GUARD", "1")))
        # MEASUREMENT (2026-10-03): raw provider response shape for THIS turn.
        # Read by _log_empty_turn to answer the 2026-10-02 empty-response
        # question from evidence rather than inference. Diagnostic only.
        # PHASE 4 (2026-10-03). This used to be ONE flat per-turn accumulator, so
        # `provider_chunks=6` on a turn that made FOUR llm calls meant "six chunks
        # somewhere across four calls" - it could not name the call that produced
        # nothing. Now every call gets its own record, so a recurrence is
        # attributable.
        # ROUND ZA (2026-10-05): the accumulator lives in `server_state` and is
        # re-armed by `_stamp_progress(..., turn_started=True)`. It used to be a
        # start_server() closure reset inside `traced_completions` on a user-role
        # signal, which meant a turn that never called the provider kept the
        # previous turn's shape. A closure could not be reached from the
        # module-level turn boundary anyway - that is the NameError trap the
        # 2026-10-02 audit recorded for _flog/trace.
        _reset_llm_response_shape(server_state)

        def _begin_llm_call_shape(params, call_index):
            """One record per provider call. Metadata only: no key, no header,
            no payload, no message content - just sizes, names and a hash."""
            record = {
                "call_index": call_index,
                "chunks": 0,
                "stream_opened": False,
                "skipped_stream": False,
                "finished": False,
                "finish_reason": None,
                "content_len": 0,
                "reasoning_len": 0,
                "usage": None,
                "chunk_type": None,
                "parse": None,
                "top": [],
                "choice": [],
                "delta": [],
                "chars": {},
                "model": str(params.get("model") or ""),
                "max_tokens": params.get("max_tokens"),
                "reasoning_effort": params.get("reasoning_effort"),
                "think": params.get("think", "UNSET"),
                "tool_choice": _scrub_tool_choice(params.get("tool_choice")),
                "stream_options": bool(params.get("stream_options")),
                "messages": len(params.get("messages") or []),
                "prompt_fingerprint": _prompt_fingerprint(params.get("messages")),
                "elapsed_ms": None,
            }
            # One record per provider call, appended to THIS turn's
            # accumulator. ROUND ZA: read through server_state rather than
            # through a closure, so the turn boundary can re-arm it.
            _shape = server_state["llm_response_shape"]
            _shape["calls"].append(record)
            _shape["turn_valid"] = True
            return record

        def traced_completions(**params):
            # `tts_audio_seen` records whether THIS turn has produced audio yet,
            # so exactly one `tts_first_audio` marker is traced per turn. It was
            # initialised once at boot and never reset, so the marker fired only
            # on the FIRST turn of a process - every later speaking turn logged
            # `tts_start` with no `tts_first_audio` and looked like a TTS failure
            # when the audio was fine (observed 2026-10-18 at 14:33:29 and
            # 14:35:14). It lives on `server_state` (not as a closure local) so
            # this block stays extractable - several test suites compile this
            # function in isolation and a `nonlocal` to a start_server local
            # would make every one of them SyntaxError.
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
            # ROUND Q (2026-10-03): deliver a DEFERRED tool-cap nudge here, at
            # the last possible moment before this request goes out.
            #
            # The guard used to append its nudge to interpreter.messages the
            # instant tool #12 finished. That lands the nudge BETWEEN a tool
            # call's code row and its console/result row, because OI only
            # appends the result row when the output chunk actually arrives.
            # run_tool_calling_llm.py pairs calls to results POSITIONALLY
            # (run_tool_calling_llm.py:52), so the interposed user row orphans
            # the result and OI's orphan branch synthesises a tool call whose
            # arguments are the literal non-JSON string
            # "# Automated tool call to fetch more output, triggered by the
            # user." (run_tool_calling_llm.py:80). That row is rebuilt from
            # interpreter.messages on EVERY later request, so the 400 was
            # permanent and identical across every provider until a restart.
            #
            # Flushing here guarantees the safe shape
            # [..., assistant_code, computer_console, user_nudge] instead.
            # Both sides are updated because respond() copies
            # interpreter.messages at respond.py:63 BEFORE llm.run() at
            # respond.py:88 - so appending to interpreter.messages alone would
            # miss this very request and the nudge would only arrive a call
            # late, changing the guard's meaning.
            _pending_nudge = server_state.get("pending_tool_cap_nudge")
            if _pending_nudge:
                if _last.get("role") == "user":
                    # A real new user turn began, so this nudge belongs to the
                    # PREVIOUS turn, which ended without another LLM call.
                    # Discard it rather than leaking it into an unrelated turn.
                    server_state["pending_tool_cap_nudge"] = None
                    _flog.info("tool loop-guard nudge discarded (turn ended first)")
                    trace("tool_cap_nudge_discarded")
                else:
                    server_state["pending_tool_cap_nudge"] = None
                    _nudge_row = {"role": "user", "type": "message", "content": _pending_nudge}
                    try:
                        interpreter.messages.append(dict(_nudge_row))
                    except Exception:
                        pass
                    # Deliver it on THIS request too, not just the next one.
                    if isinstance(params.get("messages"), list):
                        params["messages"].append(dict(_nudge_row))
                    _flog.info("tool loop-guard nudge delivered on next llm call")
                    trace("tool_cap_nudge_delivered")
                    # ROUND ZB-L4-2: bound the reasoning on the WRAP-UP call
                    # only. This is the exact completion that caused ZB-L4.
                    #
                    # Measured 2026-10-07 on the live provider: after 12 tools
                    # the nudge was delivered on call #13 and that call came
                    # back finish_reason="length", content_len=0, because the
                    # model spent its whole max_tokens=4096 budget on reasoning
                    # and was cut off before emitting anything. The nudge asks
                    # it to wrap up, which makes it reason hard, which is
                    # precisely what runs it out of budget. The reasoning model
                    # is not being throttled here - only the ONE completion
                    # whose job is to stop and report.
                    #
                    # WHY extra_body AND NOT THE TOP-LEVEL PARAMETER. Measured
                    # on this stack (LiteLLM 1.100.0):
                    #   * params["reasoning_effort"] - SILENTLY DROPPED.
                    #     get_supported_openai_params() does not list it, and
                    #     drop_params=True strips it without warning. The call
                    #     then behaves EXACTLY like the default: same 15.5k
                    #     reasoning chars, same finish=length, same zero
                    #     content. A control that is accepted and ignored.
                    #   * drop_params=False - hard UnsupportedParamsError.
                    #   * optional_params - also dropped.
                    #   * extra_body={"reasoning_effort": "low"} - FORWARDED.
                    #     finish=stop, 282 reasoning chars, 2851 content.
                    # FRIDAY's vision path already sets reasoning_effort
                    # successfully for the same reason: it posts raw requests
                    # instead of going through LiteLLM.
                    #
                    # WHY openai/ ONLY. brain_router configures cloud candidates
                    # as "openai/"+model and the local one as ollama_chat/*. But
                    # extra_body means DIFFERENT things to different LiteLLM
                    # providers (Azure, OpenRouter and Vertex each read it
                    # differently), so it is applied only where it was measured.
                    _low_model = params.get("model")
                    if isinstance(_low_model, str) and _low_model.startswith("openai/"):
                        # MERGE, never replace: extra_body may already carry
                        # something, and clobbering it would drop a live setting.
                        _extra = params.get("extra_body")
                        if not isinstance(_extra, dict):
                            _extra = {}
                            params["extra_body"] = _extra
                        _extra["reasoning_effort"] = "low"
                        _flog.info(
                            "wrap-up call: reasoning_effort=low via extra_body "
                            "(model=%s)", _low_model)
                        trace("wrap_up_low_reasoning")
            if _last.get("role") == "user":
                llm_loop_guard["calls"] = 0
                # ROUND ZA: the provider-shape reset used to be HERE, next to
                # this line. It is now in _stamp_progress(turn_started=True).
                # Being inside traced_completions meant it could only ever run
                # for a turn that had already decided to call the provider, so
                # every deterministic turn inherited the previous turn's
                # per-call records - which is precisely what the comment here
                # claimed to prevent. tool_counter below has the same
                # LLM-layer dependence and is deliberately NOT moved in this
                # round: it is unrelated state that merely sits nearby.
                tool_counter["n"] = 0  # per-turn tool budget (fixes lifetime accumulation)
                # Re-arm the first-audio marker with the other per-turn budgets.
                # Without this it stayed True forever after the process's first
                # playback, so `tts_first_audio` could never be observed again.
                server_state["tts_audio_seen"] = False
                # A pending tool-cap nudge can never outlive its turn: this is
                # the same new-user-row signal that re-arms every other per-turn
                # budget above, and it is RELIABLE because OI's loop_message is
                # appended to its own copy of the messages (respond.py:66-76),
                # never to interpreter.messages. Clearing here is the
                # cross-turn guarantee for the deferred nudge.
                server_state["pending_tool_cap_nudge"] = None
                # ZB-L4-3: the nudge latch shares the tool budget's boundary.
                # Reset only when OI starts a real user turn; queued arrivals
                # and progress stamps must not re-arm the running turn.
                server_state["tool_cap_nudged"] = False
                # Reset the runaway-script guard with the rest of the per-turn
                # budget, so one bad turn cannot poison the next one.
                turn_guard["started"] = time.monotonic()
                turn_guard["console_events"] = 0
                turn_guard["flood_stopped"] = False
                turn_guard["same_run"] = 0
                turn_guard["last_console"] = None
                # ROUND ZB-FS: the fruitless-search budget is per-turn like every
                # guard beside it, so one turn that gave up on a missing string
                # can never silence the next one.
                turn_guard["search_misses"] = 0
                turn_guard["search_miss_target"] = ""
            llm_loop_guard["calls"] += 1
            call_shape = _begin_llm_call_shape(params, llm_loop_guard["calls"])
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
                    # PHASE 4: this call returns BEFORE the stream is opened, so
                    # its record must say so - "0 chunks" is otherwise
                    # indistinguishable from "the provider sent nothing".
                    call_shape["skipped_stream"] = True
                    call_shape["guard"] = "plain_end"
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
                call_shape["skipped_stream"] = True
                call_shape["guard"] = "cap_end"
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
            knowledge = server_state.get("knowledge_turn")
            if (knowledge is not None and knowledge.owner is _ownership(server_state)
                    and _producing_attempt.get() is server_state.get("turn_attempt")
                    and server_state.get("cancelled_owner") is not knowledge.owner):
                original = params.get("messages")
                grounded = knowledge.prompt(original, _producing_attempt.get())
                if grounded is not original:
                    params["messages"] = grounded
                    params.pop("tools", None)
                    params.pop("tool_choice", None)
            mcp_stream = None
            tone_policy = getattr(interpreter, "personality", None)
            if (tone_policy is not None and _producing_attempt.get() is not None
                    and _producing_attempt.get() is server_state.get("turn_attempt")
                    and server_state.get("cancelled_owner") is not _ownership(server_state)):
                params["messages"] = tone_policy.prompt(
                    params.get("messages"), _ownership(server_state), _producing_attempt.get())
            mcp = server_state.get("mcp_runtime")
            if (knowledge is None and params.get("tools")
                    and not _ownership(server_state).is_external and _producing_attempt.get() is not None
                    and _producing_attempt.get() is server_state.get("turn_attempt")
                    and not interpreter.stop_event.is_set()):
                from .mcp_turn import ModelTools

                prefs = companion.preferences()
                workflow_config = workflow_bridge.snapshot().get("config") or {}
                candidate = ModelTools(mcp, params, presentation=(execution_gate is not None
                    and prefs["available"] and prefs["values"]["teaching"]),
                    highlight=(execution_gate is not None and prefs["available"]),
                    workflow=(workflow_config.get("capabilities", ())
                        if workflow_config.get("enabled") else ()))
                if candidate.refs:
                    mcp_stream = candidate
            first_chunk = True
            thinking_logged = False
            # MEASUREMENT (2026-10-03): accumulate the raw provider response
            # shape for this call, STRUCTURALLY. The first attempt on
            # 2026-10-02 recorded a 6-chunk empty Modal turn as
            # content_len=0 / reasoning_len=0 / usage=null and the cause could
            # not be told apart from a diagnostic bug, because the accumulator
            # only read three hard-coded field names on an object that is not a
            # dict. `_describe_chunk` records field NAMES, TYPES and LENGTHS so
            # the next occurrence is diagnosable instead of re-guessed.
            shape = {"finish_reason": None, "content_len": 0,
                     "reasoning_len": 0, "chunks": 0, "usage": None}
            call_shape["stream_opened"] = True
            _call_started = time.monotonic()
            try:
                for chunk in original_completions(**params):
                    shape["chunks"] += 1
                    # Mirror into THIS CALL's record so _log_empty_turn can read
                    # it after the stream finishes, and so a recurrence can be
                    # attributed to the call that produced nothing.
                    _merge_chunk_shape(call_shape, chunk)
                    if first_chunk:
                        first_chunk = False
                        trace("llm_first_chunk")
                    if not thinking_logged:
                        try:
                            choice = chunk["choices"][0]
                            data = choice.get("message") or choice.get("delta") or {}
                            reason = data.get("reasoning_content") if isinstance(data, dict) else None
                            if reason and isinstance(reason, str) and reason.strip() and knowledge is None:
                                _flog.info("thinking: %s", _redact_text(reason)[:600])
                                trace("llm_thinking")
                                thinking_logged = True
                        except Exception:
                            pass
                    if mcp_stream is None:
                        yield chunk
                    else:
                        yield from mcp_stream.filter(chunk)
                call_shape["finished"] = True
                call_shape["elapsed_ms"] = int((time.monotonic() - _call_started) * 1000)
                proposal = mcp_stream.proposal() if mcp_stream is not None else None
                if proposal is not None:
                    from .companion import PRESENTATION, HIGHLIGHT
                    if proposal is not False and proposal[0] is PRESENTATION:
                        owner, generation = _ownership(server_state), _producing_attempt.get()
                        from types import SimpleNamespace
                        item = SimpleNamespace(owner=owner, generation=generation,
                            deadline=time.monotonic() + 2)
                        try:
                            with execution_lock:
                                permitted = spend_mcp(item)
                                shown = permitted and companion.publish(owner, generation, proposal[1],
                                    live=lambda: mcp_live(item))
                            message = ("Added to your local teaching cards or inbox. It is display-only; no action was executed."
                                       if shown else "The presentation was not displayed: its turn or settings no longer permit it.")
                        except (ValueError, TypeError, RecursionError):
                            message = "The teaching card was refused because its data is unsupported or exceeds the limit."
                    elif proposal is not False and proposal[0] is HIGHLIGHT:
                        import concurrent.futures
                        from types import SimpleNamespace
                        item = SimpleNamespace(owner=_ownership(server_state), generation=_producing_attempt.get(),
                            deadline=time.monotonic()+2)
                        future = concurrent.futures.Future()
                        def draw_current_highlight():
                            if not future.set_running_or_notify_cancel():
                                return
                            try:
                                from . import screen_overlay
                                payload = proposal[1]
                                allowed_keys = {"query","shape","label","rect","points","ttl"}
                                with execution_lock:
                                    if not isinstance(payload, dict) or set(payload)-allowed_keys or not spend_mcp(item):
                                        result = {"ok":False,"reason":"Turn no longer permits this highlight"}
                                    else:
                                        screen_overlay.set_window_executor(server_state["event_loop"].call_soon_threadsafe)
                                        result = screen_overlay.annotate(**payload)
                                        if result.get("ok"):
                                            companion.overlay = (item.owner, item.generation, screen_overlay._active)
                                future.set_result(result)
                            except Exception:
                                future.set_result({"ok":False,"reason":"Highlight could not be drawn"})
                        loop = server_state.get("event_loop")
                        try:
                            if loop is None:
                                raise RuntimeError("No display owner")
                            loop.call_soon_threadsafe(draw_current_highlight)
                            result = future.result(timeout=2.5)
                            message = ("Temporary display-only highlight shown." if result.get("ok") else
                                       "Highlight not shown: " + str(result.get("reason") or "unavailable"))
                        except (RuntimeError, concurrent.futures.TimeoutError):
                            future.cancel()
                            message = "Highlight unconfirmed; it was not retried."
                    elif proposal and proposal[0] == "friday-workflow-request-v1":
                        message = run_workflow_request(proposal[1])
                    else:
                        message = server_state["run_mcp_proposal"](proposal)
                    yield {"choices": [{"delta": {"content": message}, "finish_reason": "stop"}]}
            except Exception as tool_hallucination_error:
                # gpt-oss sometimes invents tools that are not in the request
                # ("silent_search<|channel|>commentary"), aborting mid-stream.
                # Retry once in PLAIN TEXT mode (markdown code blocks) so the
                # turn still completes instead of erroring on the phone.
                error_text = str(tool_hallucination_error)
                if "not in request.tools" not in error_text and "Tool call validation" not in error_text:
                    raise
                router = server_state["brain_router"]
                if server_state.get("identity_integration") is not None and router.current_catalog_route_disabled():
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

    def chat_append(role, content, status="complete", *, knowledge=None):
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
            if knowledge is None:
                buffer.merge_stream(text)
            else:
                buffer.merge_stream(text, knowledge=knowledge)
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

    _ACTIVITY_QUERY_RE = re.compile(
        r"\b(?:what\s+did\s+you\s+do|did\s+you\s+(?:do|did)|have\s+you\s+(?:done|ever)|"
        r"what\s+(?:have\s+you|you)\s+(?:done|say)|your\s+(?:recent|latest|last)\s+"
        r"(?:turns?|actions?|activity)|last\s+time\s+you|earlier\s+you|"
        r"what\s+were\s+you\s+doing|what\s+was\s+you\s+doing)\b",
        re.IGNORECASE,
    )

    async def dispatch_text(text, *, reply_channel="", reply_token=None,
                            ownership=None, source="text"):
        """Admit a single request or defer it without touching the active owner."""
        request = str(text or "").strip()
        if not request:
            return {"accepted": False, "status": "invalid", "message": "Enter a message first."}
        if server_state["is_paused"]:
            return {"accepted": False, "status": "paused", "message": "Voice mode is paused."}
        owner = ownership if ownership is not None else _request_owner(reply_token)
        busy = turn_is_busy()
        if busy or _pending_has(server_state):
            if len(server_state["pending_turns"]) >= _PENDING_QUEUE_MAX:
                return {"accepted": False, "status": "busy",
                        "message": "My request queue is full. Try again shortly."}
            _pending_push(server_state, user_text=request, reply_token=owner.reply_token,
                          source=source, ownership=owner)
            trace("turn_deferred", source=source)
            if not busy:
                await _drain_pending()
            return {"accepted": True, "status": "queued"}
        return await _run_text_turn(request, owner, source=source)

    async def _run_text_turn(request, owner, *, source):
        _activate_owner(owner)
        if archive is not None:
            archive.admit(owner, server_state["turn_attempt"], source, request)
        try:
            status = await _prepare_text_turn(request, owner, source=source)
        except (Exception, asyncio.CancelledError):
            _abort_admission(owner)
            raise
        return {"accepted": True, "status": status or "queued"}

    def _abort_admission(owner):
        if _ownership(server_state) is not owner:
            return
        # The caller reports this error. Keep the slot reserved until any
        # started interpreter worker reaches its own terminal frame.
        server_state["terminal_notice_owner"] = owner
        server_state["cancelled_owner"] = owner
        if archive is not None:
            archive.mark(owner, "failed")
        _identity.close(server_state, owner)
        if response_is_active():
            interpreter.stop_event.set()
        else:
            _put_owned({"role": "server", "type": "status", "content": "complete"})

    async def _prepare_text_turn(request, owner, *, source):
        personality.admit(owner, server_state["turn_attempt"], request,
                          "hud" if owner.private_text and not owner.is_external and source == "text" else source)
        actor = "voice" if source == "voice" else "text"
        from_telegram = owner.channel == "telegram"
        server_state["last_user_text"] = request
        if not from_telegram:
            chat_append("user", request)
            publish_status(STATUS_THINKING, "FRIDAY is working")
        if source != "voice" and not from_telegram:
            try:
                from .self_improve import append_outcome

                append_outcome(_request_words_only(request), tag="asked")
            except Exception:
                pass
            log_action(actor="text", risk="read_only", operation="user_command",
                       allowed=True, text=_redact_text(request))
            _flog.info("typed: %s", _redact_text(request))

        pending_reply = _pending_resolve(request, source)
        if pending_reply is not None:
            queue_direct_response(pending_reply, admitted=True)
            return

        if from_telegram and _telegram_screen_request_kind(request) == "direct":
            result = await _send_direct_screenshot(request, owner)
            if result is None and server_state.get("cancelled_owner") is not owner:
                server_state["terminal_notice_owner"] = owner
            queue_direct_response(result, admitted=True)
            return "media_only"

        special = handle_special_command(request, actor=actor)
        if special is not None:
            queue_direct_response(special["response"], admitted=True)
            return
        command_result = route_command(request, actor=actor)
        if command_result is not None:
            queue_direct_response(command_result["response"], admitted=True)
            return
        if source == "voice":
            reminder = _pending_reminder_if_any()
            if reminder:
                queue_speech_only(reminder)
        memory_command = parse_memory_command(request)
        if memory_command is not None:
            if server_state["memory_store"] is None:
                response = "Memory is unavailable right now."
            else:
                try:
                    response = format_memory_response(memory_command, server_state["memory_store"])
                except Exception:
                    response = "I couldn’t update memory right now."
            log_direct(actor, "memory", "ok", response, command=memory_command)
            queue_direct_response(response, admitted=True)
            return

        content = request
        if _automatic_memory_enabled(interpreter) and server_state["memory_store"] is not None:
            try:
                extract_and_remember_automatic(content, server_state["memory_store"])
            except Exception as error:
                trace("automatic_memory_failed error=%s" % type(error).__name__)
        memory_context = ""
        if server_state["memory_store"] is not None:
            memory_context = build_memory_context(
                content, server_state["memory_store"], top_n=3, max_chars=1600,
                include_activity=bool(_ACTIVITY_QUERY_RE.search(content)),
                exclude_topics=("activity",) if _LIVE_STATE_QUERY_RE.search(content) else (),
            )
        passages = []
        if is_screen_request(content):
            vision_attempt = server_state.get("turn_attempt")
            vision_observation = object()
            server_state["active_vision_capture"] = vision_observation
            server_state["active_vision_pool"] = "active"

            def vision_is_current():
                return (_ownership(server_state) is owner
                        and server_state.get("turn_attempt") is vision_attempt
                        and server_state.get("cancelled_owner") is not owner)

            def cancelled_vision():
                if (_ownership(server_state) is owner
                        and server_state.get("turn_attempt") is vision_attempt
                        and server_state.get("cancelled_owner") is owner):
                    _put_owned({"role": "server", "type": "status", "content": "complete"})
                return "cancelled"

            try:
                with _identity.scoped(server_state, vision_attempt):
                    # Screen capture/provider work cannot occupy the listener
                    # loop. to_thread preserves the exact identity ContextVar.
                    work = asyncio.create_task(asyncio.to_thread(screen_response, content))
                    try:
                        response = await asyncio.shield(work)
                    except asyncio.CancelledError:
                        while not work.done():
                            try:
                                await asyncio.shield(work)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        try:
                            work.result()
                        except BaseException:
                            pass
                        raise
                if not vision_is_current():
                    return cancelled_vision()
            except Exception:
                if not vision_is_current():
                    return cancelled_vision()
                raise
            finally:
                if server_state.get("active_vision_capture") is vision_observation:
                    server_state["active_vision_pool"] = None
                    server_state.pop("active_vision_capture", None)
            _log_screen_call_ledger()
            try:
                from .self_improve import append_outcome

                append_outcome(response, tag="vision_read")
            except Exception:
                pass
            _record_fresh_screen_evidence(server_state)
            content = _screen_context_prefix(server_state) + content
        elif not owner.is_external and source in ("text", "voice"):
            try:
                passages = knowledge_store.search(request)
            except Exception as error:
                server_state["knowledge_read_failed"] = True
                _flog.warning("Local knowledge lookup unavailable (%s)", type(error).__name__)
        if memory_context:
            content = memory_context + "\n\nUser request:\n" + content
        if from_telegram:
            content = (
                "[Sir is talking to you on Telegram — your reply goes to his "
                "phone as a chat text. Answer in one short bubble. Do NOT "
                "open, show, or play anything on the PC, do NOT activate "
                "local voice, and do not send a file/screenshot unless he "
                "explicitly asked for one. When he says 'show me', the "
                "screenshot itself is handled for you; just acknowledge.]\n\n"
                + content
            )
        _begin_single_turn(interpreter, server_state, user=request, source=source)
        server_state["brain_router"].new_turn()
        if server_state["brain_router"].apply_for(request, interpreter) is None:
            if archive is not None:
                archive.mark(owner, "failed")
            queue_direct_response("No eligible thinking model is available right now.",
                                  record_activity=False, admitted=True)
            return "unavailable"
        _mark_turn_dispatch(server_state, interpreter, user=request, content=content,
                            source=source, reply_token=owner.reply_token)
        if passages:
            history_cut = len(interpreter.messages)
            server_state["knowledge_history_cut"] = history_cut
            server_state["knowledge_turn"] = KnowledgeTurn(owner, passages, history_cut, request)
        await _feed_turn(content, owner)

    def submit_text(text):
        loop = server_state.get("event_loop")
        if loop is None or not loop.is_running():
            return {"accepted": False, "status": "unavailable", "message": "FRIDAY server is not ready yet."}
        # Saved, explicit local text preference only. Never infer private speech
        # from a microphone, HTTP caller or local device. Queues retain this
        # immutable choice; admission installs it, never submission.
        private = personality.private_hud()
        owner = TurnOwnership(private_text=private, speaks_locally=not private)
        future = asyncio.run_coroutine_threadsafe(
            dispatch_text(text, ownership=owner, source="text"), loop)
        try:
            return future.result(timeout=2.0)
        except TimeoutError:
            return {"accepted": True, "status": "queued"}
        except Exception as error:
            return {"accepted": False, "status": "error", "message": "Unable to submit that message."}

    interpreter.submit_text = submit_text

    # --- Telegram remote: same brain, phone transport -----------------------
    from .remote_telegram import TelegramRemoteAdapter

    _clear_ownership(server_state)

    async def _send_direct_screenshot(text, owner):
        chat_id = owner.chat_id
        generation = server_state["turn_attempt"]
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
        # active brain composes the caption from what it truly sees. Never a
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
        # The caption is ALWAYS AI-authored and grounded (vision read, else
        # real OCR text, else the real open window titles). There is
        # deliberately NO canned fallback string: "Fresh screen capture
        # for you, Sir." is exactly the canned acknowledgement the project's
        # Screen/Vision/Media policy forbids, and it shipped as a real reply
        # because the vision chain had gone cold and the caption came back
        # empty.
        caption = caption or ""
        if _ownership(server_state) is not owner or server_state.get("cancelled_owner") is owner:
            return None
        if not caption:
            sent = await telegram_adapter.send_photo(chat_id, jpeg)
            if not sent:
                return "Sir, I captured the screen but couldn't upload it to Telegram. Check the PC and ask again if needed."
            return (
                "I sent the screenshot, but I could not read anything off it - "
                "my vision chain is cold right now. Ask me again in a moment "
                "and I will describe what is actually there."
            )
        try:
            sent = await telegram_adapter.send_photo(chat_id, jpeg, caption=caption[:1024])
        except Exception as error:
            _flog.warning("telegram screenshot send failed: %s", _redact_text(str(error)))
            return "Sir, I captured the screen but couldn't upload it to Telegram. Check the PC and ask again if needed."
        if not sent:
            return "Sir, I captured the screen but couldn't upload it to Telegram. Check the PC and ask again if needed."
        if archive is not None:
            archive.append(owner, generation, object(), caption[:1024], notice=True)
        return None  # photo + AI-authored caption already delivered

    async def telegram_command_handler(text, chat_id, message=None):
        decision_command = is_owner_decision_command(text)
        lowered = (text or "").lower().strip()
        screen_request_kind = None if decision_command else _telegram_screen_request_kind(text)
        speak_requested = not decision_command and bool(re.search(
            # anywhere in the message: 'Say "how are you"', 'Perfect, say ...',
            # 'say it out loud', 'on the headset' — position no longer matters
            r"\bsay\b|\bspeak\b|\bwhisper\b|\bannounce\b|\bshout\b|\brepeat\b|"
            r"\bout\s+loud\b|\b(?:on|through|via)\s+the\s+(?:headset|speakers?)\b|\baloud\b",
            lowered,
        ))
        ownership = TurnOwnership(
            channel="telegram", chat_id=chat_id, speaks_locally=bool(speak_requested),
            post_action_screenshot=screen_request_kind == "after_action",
            screen_request="" if decision_command else str(text or "")[:800],
        )
        result = await dispatch_text(text, ownership=ownership, source="telegram")
        if not result.get("accepted", False):
            return str(result.get("message") or "I couldn't accept that request right now.")
        if result.get("status") == "media_only":
            return None
        if result.get("response"):
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

    async def _deliver_post_action_screenshot(ownership, caption):
        """Attach a fresh result frame only after the owning turn completes.

        Takes the immutable ownership record, not the legacy capture dict - that
        dict was the last reader of the mirror, and it is being retired.
        """
        if not isinstance(ownership, TurnOwnership) or not ownership.post_action_screenshot:
            return False, "not_requested"
        chat_id = str(ownership.chat_id or "")
        if not chat_id or chat_id == TEXT_CHANNEL_CHAT_ID:
            return False, "invalid_chat"
        try:
            from .screen_understanding import capture_screen_jpeg

            jpeg = capture_screen_jpeg()
            if not jpeg:
                return False, "capture_empty"
            sent = await telegram_adapter.send_photo(
                chat_id,
                jpeg,
                caption=str(caption or "Finished — here is the result.")[:1024],
            )
            return bool(sent), "sent" if sent else "upload_failed"
        except Exception as error:
            _flog.warning(
                "telegram post-action screenshot failed: %s",
                _redact_text(str(error)),
            )
            return False, "capture_failed"

    async def _deliver_owned_result(owner, text, *, screenshot=False):
        if _ownership(server_state) is not owner or not text:
            return False
        generation = server_state["turn_attempt"]

        def delivered():
            if archive is not None:
                archive.append(owner, generation, object(), text, notice=True)
            return True

        if owner.channel == "text":
            return delivered() if _post_reply(server_state, text, ownership=owner) else False
        if owner.channel == "telegram":
            if screenshot and owner.post_action_screenshot and server_state.get("cancelled_owner") is not owner:
                sent, reason = await _deliver_post_action_screenshot(owner, text)
                if sent:
                    return delivered()
                text += " I couldn't attach the final screenshot."
                _flog.warning("telegram post-action screenshot not delivered: %s", reason)
            if (_ownership(server_state) is not owner
                    or (screenshot and server_state.get("cancelled_owner") is owner)):
                return False
            try:
                return delivered() if await telegram_adapter.send_text(owner.chat_id, text) else False
            except Exception as error:
                _flog.warning("telegram reply failed: %s", type(error).__name__)
                return False
        return False

    async def _terminal_notice(owner, text, *, screenshot=False, outcome=None):
        if _ownership(server_state) is not owner or server_state.get("terminal_notice_owner") is owner:
            return
        server_state["terminal_notice_owner"] = owner
        if outcome and archive is not None:
            archive.mark(owner, outcome)
        if owner.is_external:
            if not await _deliver_owned_result(owner, text, screenshot=screenshot) and archive is not None:
                archive.mark(owner, "failed")
        else:
            queue_speech_only(text)

    async def _drain_pending():
        while not server_state["is_paused"] and (record := _pending_pop(server_state)) is not None:
            owner = record["ownership"]
            registry = server_state.get("chat_requests")
            waiting = registry.get(owner.reply_token) if registry is not None and owner.reply_token else None
            if owner.reply_token and owner.reply_token not in (server_state.get("reply_waiters") or {}):
                continue
            if waiting is not None and not waiting.may_run:
                continue
            try:
                if record.get("direct_response") is not None:
                    _activate_owner(owner)
                    queue_direct_response(record["direct_response"], admitted=True)
                else:
                    await _run_text_turn(record["user_text"], owner, source=record["source"])
            except Exception as error:
                _flog.warning("deferred request failed: %s", type(error).__name__)
                if record.get("direct_response") is not None:
                    _abort_admission(owner)
                if owner.is_external:
                    await _deliver_owned_result(owner, "I couldn't start that request.")
            return True
        return False

    def resume_owned_voice():
        loop = server_state.get("event_loop")
        if loop is None or not loop.is_running():
            return

        async def resume():
            if not turn_is_busy():
                await _drain_pending()
            if not tts_is_degraded(server_state):
                queue_direct_response("Voice mode activated.", record_activity=False)

        asyncio.run_coroutine_threadsafe(resume(), loop)

    interpreter.resume_owned_voice = resume_owned_voice

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
        _ensure_owned_queue()
        mcp = server_state.get("mcp_runtime")
        if mcp is not None:
            await mcp.start()
        if os.getenv("FRIDAY_AUDIO_STARTUP_CHECK", "").strip().lower() not in ("1", "true", "yes", "on"):
            from .n8n_turn import event_loop as workflow_event_loop
            workflow_stop = asyncio.Event()
            server_state["n8n_event_stop"] = workflow_stop
            server_state["n8n_event_task"] = asyncio.create_task(workflow_event_loop(workflow_bridge, workflow_stop))
        publish_status(STATUS_IDLE, "Voice pipeline ready")
        await start_telegram_remote()

    async def stop_mcp():
        workflow_bridge.invalidate()
        if server_state.get("n8n_event_stop") is not None:
            server_state["n8n_event_stop"].set()
        task = server_state.get("n8n_event_task")
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        mcp = server_state.get("mcp_runtime")
        if mcp is not None:
            await mcp.stop()

    def close_mcp():
        loop = server_state.get("event_loop")
        if server_state.get("mcp_runtime") is not None and loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(stop_mcp(), loop)
            try:
                future.result(timeout=5)
            except Exception:
                future.cancel()

    interpreter.close_mcp = close_mcp

    try:
        interpreter.server.app.add_event_handler("startup", mark_event_loop)
        interpreter.server.app.add_event_handler("shutdown", stop_mcp)
    except AttributeError:
        pass

    async def new_input(self, chunk):
        server_state["event_loop"] = asyncio.get_running_loop()
        if isinstance(chunk, dict) and chunk.get("type") == "audio_capture_cancel":
            # Cancel the capture only. Never transcribe or dispatch its tail.
            server_state["voice_capture_epoch"] = server_state.get("voice_capture_epoch", 0) + 1
            self.audio_chunks = []
            server_state["background_voice_until"] = 0
            try:
                self.stt.stop()
                self.stt.clear_audio_queue()
            except Exception:
                pass
            if not turn_is_busy():
                publish_status(STATUS_IDLE, "Standing by")
            return
        if isinstance(chunk, dict) and chunk.get("type") == "playback":
            # Legacy byte-submission evidence must not extend a turn budget.
            record_playback(server_state, chunk)
            return
        _stamp_progress(server_state)
        await asyncio.sleep(0)
        try:
            if isinstance(chunk, bytes):
                self.stt.feed_audio(chunk)
                self.audio_chunks.append(chunk)
            elif isinstance(chunk, dict):
                # ROUND ZB-C: output-client playback acknowledgement. It carries
                # no start/end flags, so it can never be mistaken for a capture
                # boundary above; it is the ONLY evidence that audio was heard.
                if "start" in chunk:
                    server_state["voice_capture_epoch"] = server_state.get("voice_capture_epoch", 0) + 1
                    invalidate_life = server_state.get("invalidate_background_life")
                    if invalidate_life is not None:
                        server_state["background_voice_until"] = time.monotonic() + 65
                        invalidate_life()
                    trace("stt_start")
                    publish_status(STATUS_LISTENING, "Listening for a command")
                    self.stt.start()
                    self.audio_chunks = []

                    if response_is_active() and self.tts.is_playing():
                        # Genuine barge-in: user speaks while TTS is actually
                        # audible. Stop the in-flight TTS and let the rest of
                        # the response drain without speaking.
                        server_state["barge_in"] = True
                        if archive is not None:
                            archive.mark(_ownership(server_state), "interrupted")
                        self.tts.stop()
                        notify_speech_stop(server_state, "barge_in", queue=self.output_queue)
                        # ROUND ZB-CH: queued-but-unspoken text dies with the
                        # audio it would have followed; keep the metrics honest.
                        _sq = server_state.get("speech_queue")
                        if _sq is not None:
                            try:
                                _sq.reset("barge_in")
                            except Exception:
                                pass

                if "end" in chunk:
                    capture_epoch = server_state.get("voice_capture_epoch", 0)
                    capture_session, capture_revision = audio_link.session, audio_link.revision

                    def current_capture():
                        return (capture_epoch == server_state.get("voice_capture_epoch", 0)
                                and capture_session == audio_link.session
                                and capture_revision == audio_link.revision)

                    async def transcription_work(operation, *args):
                        # Native/local STT and cloud HTTP must not block /ping,
                        # Stop or the HUD. Cancellation cannot terminate a
                        # native call: finish it before releasing the input
                        # session, then discard its result.
                        work = asyncio.create_task(asyncio.to_thread(operation, *args))
                        try:
                            return await asyncio.shield(work)
                        except asyncio.CancelledError:
                            while not work.done():
                                try:
                                    await asyncio.shield(work)
                                except asyncio.CancelledError:
                                    continue
                                except Exception:
                                    break
                            try:
                                work.result()
                            except BaseException:
                                pass
                            raise

                    trace("stt_stop")
                    publish_status(STATUS_THINKING, "Transcribing and routing")
                    self.stt.stop()
                    if chunk.get("capture_incomplete") is True:
                        # A client capture ceiling is not a completed command.
                        # Do not transcribe/dispatch a possibly truncated action.
                        self.audio_chunks = []
                        _flog.warning("voice capture rejected: recording duration limit")
                        trace("wake_rejected reason=capture_incomplete")
                        publish_status(
                            STATUS_IDLE,
                            "Recording reached its limit; repeat with a pause.",
                        )
                        self.output_queue.sync_q.put({
                            "ignored": True, "end": True,
                            "capture_incomplete": True,
                        })
                        return
                    content = ""
                    if self.audio_chunks and api_pools.cloud_stt_enabled():
                        stt_observation = object()
                        server_state["active_stt_capture"] = stt_observation
                        server_state["active_stt_pool"] = "active"
                        try:
                            wav_bytes = pcm_to_wav(b"".join(self.audio_chunks))
                            content, stt_provider, stt_error = await transcription_work(transcribe_wav, wav_bytes)
                        finally:
                            if server_state.get("active_stt_capture") is stt_observation:
                                server_state["active_stt_pool"] = None
                                server_state.pop("active_stt_capture", None)
                        if not current_capture():
                            trace("stt_discarded reason=capture_superseded")
                            return
                        if content:
                            trace(f"stt_cloud provider={stt_provider}")
                            _flog.info("cloud stt ok provider=%s", stt_provider)
                        elif stt_error:
                            _flog.info("cloud stt failed: %s", stt_error)
                    if not content:
                        content = await transcription_work(self.stt.text)
                    if not current_capture():
                        trace("stt_discarded reason=capture_superseded")
                        return
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
                        if not _set_voice_input(True):
                            publish_status(STATUS_ERROR, "Voice input setting could not be queued")
                            self.output_queue.sync_q.put({"ignored": True, "end": True})
                            return
                        close_hot_window("paused")
                        publish_status(STATUS_IDLE, "Voice mode paused")
                        print("Voice mode paused. Press [ENTER] in the server terminal to resume listening.")
                        # ROUND ZB-REC: never feed a fixed string we cannot
                        # synthesize - it would accumulate in char_iter and give
                        # play() the exact condition it re-enters on.
                        _tts_recover_latch(self.tts)
                        if not turn_is_busy() and not tts_is_degraded(server_state):
                            _speech_playback(self)
                            self.tts.feed("Voice mode paused.")
                            _start_owned_playback(self, sentence_fragment_delimiters=".?!;,\n…)]}", minimum_sentence_length=1)
                            _seal_owned_playback(self)
                            # Audio IS starting here, so pair it with tts_start like
                            # every other playback site - otherwise this spoke while
                            # tracing tts_first_audio with nothing to match it.
                            trace("tts_start", tts_stage=_tts_stage_now())
                        else:
                            trace("tts_degraded",
                                  reason=tts_degraded_status(server_state).get("reason", ""),
                                  where="pause_ack")
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
                    if found_wake:
                        server_state["last_wake_match_at"] = time.time()
                    trace(
                        f"wake_filter final={text_lower!r} hot_window={hot_window} "
                        f"matched={matched_word or 'none'}"
                    )
                    
                    if not hot_window and not found_wake:
                        trace("wake_rejected reason=final_text_missing_wake")
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
                        # ROUND ZB-L0 (2026-10-07): a bare wake word ("Friday.")
                        # used to publish "Ready for a command" and return
                        # WITHOUT arming the follow-up window, so the very next
                        # utterance had to say "Friday" again. open_hot_window()
                        # was only ever called from the refresh path (inside
                        # `if hot_window:`) and two HTTP sites - never here. The
                        # window is the EXISTING bounded one: same 30s default,
                        # same 10-60 clamp, same timer, and it refuses to open
                        # while paused. Nothing else about the state machine
                        # changes, and every in-window guard (candidate, echo,
                        # duplicate) still runs before the next dispatch.
                        open_hot_window()
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
                    _flog.info("accepted voice: %s", _redact_text(clean_content))
                    log_action(actor="voice", risk="read_only", operation="user_command",
                               allowed=True, text=_redact_text(clean_content))
                    trace("wake_accepted")
                    publish_status(STATUS_THINKING, "FRIDAY is working")
                    # Same watchdog reasoning as the typed path: measure stall
                    # from when THIS turn began, not from boot or from the
                    # previous turn.
                    # ROUND ZB-P1: snapshot FIRST. If this utterance is later
                    # deferred by the admission gate, the baseline is restored
                    # so a queued request cannot extend the turn it waits on.
                    _pre_defer_baseline = snapshot_turn_baseline(server_state)
                    _stamp_progress(server_state, turn_started=True)
                    # "...that's enough / stop" only means "be quiet" while
                    # FRIDAY is mid-response. Silence instantly, swallow the
                    # rest of the in-flight turn, stay in standby.
                    if _STOP_PHRASE_RE.match(clean_content or "") and (
                        turn_is_busy() or self.tts.is_playing()
                    ):
                        cancelled = _cancel_turn(_ownership(server_state), "stop_phrase")
                        # ROUND ZB-CH: a spoken stop also drops whatever was
                        # queued behind the audio it just silenced.
                        _sq = server_state.get("speech_queue")
                        if _sq is not None:
                            try:
                                _sq.reset("stop_phrase")
                            except Exception:
                                pass
                        _play_chirp("tool_complete")
                        # ROUND ZB-P1: a spoken stop must interrupt the WORK,
                        # not only the audio. Previously only tts.stop() ran, so
                        # a long tool kept going while FRIDAY appeared to have
                        # stopped. `stop_event` is the same mechanism the
                        # watchdog and the HARD tool cap use - the only one that
                        # genuinely ends a running Open Interpreter turn.
                        _flog.info(
                            "stop phrase honored: response silenced; "
                            "active turn cancelled=%s", cancelled)
                        log_action(actor="voice", risk="read_only",
                                   operation="conversation_stop", allowed=True,
                                   spoken="Silenced response on user request")
                        trace("stop_honored", cancelled_active_turn=cancelled)
                        publish_status(STATUS_IDLE, "Standing by")
                        self.output_queue.sync_q.put({"ignored": True, "end": True})
                        # Deliberately NO `return` here (ROUND ZB-P1). Swallowing
                        # the utterance meant a stop request was accepted and
                        # then never answered. Falling through lets the same
                        # words be ADMITTED and answered ("Stopped."), which is
                        # the acknowledgement Sir asked for; if the cancelled
                        # turn is still winding down, the admission gate queues
                        # this one and the `status: complete` drain answers it -
                        # seconds later, not never.

                    # ROUND U (2026-10-04): the Intent Judge used to sit here.
                    # It ran on 34 of 138 voice turns, cost a median 6.52s
                    # (max 8.73s) and produced 0 useful decisions in 34/34
                    # attempts - every call timed out against a permanently
                    # cold llama3.2:3b (load_duration 26.26s vs a 6s timeout,
                    # while the brain keep-alive pins only qwen3:8b) and fell
                    # back to passing the query through unchanged. Its only
                    # unique behaviour, rejecting speech that used the wake word
                    # mid-sentence, never fired once. It was removed rather
                    # than repaired: no replacement classifier was introduced,
                    # and a voice request now goes straight from the wake-word
                    # gate into the normal pipeline (special commands, routing,
                    # memory, screen, then the provider chain). See MEMORY.md
                    # Round T/U and SEARCHES.md 60.

                    server_state["last_wake_time"] = now
                    server_state["last_wake_text"] = clean_content
                    if hot_window:
                        close_hot_window("follow-up accepted")
                    if turn_is_busy():
                        restore_turn_baseline(server_state, _pre_defer_baseline)
                    await dispatch_text(clean_content, source="voice")
        except Exception as e:
            if "current_capture" in locals() and not current_capture():
                trace("stt_discarded reason=capture_superseded")
                return
            # BOUNDED INPUT-ERROR REPORTING (2026-11-06).
            # One broken telemetry call (the trace `event` collision) used to
            # fire this handler on EVERY playback ack: 498 "input error" lines,
            # each republishing state=error and each calling
            # brain_router.mark_failure() - turn state poisoned by a diagnostic,
            # not by a real input failure. Now:
            #   * identical messages are reported ONCE (bounded memory, the
            #     oldest entries rotate out);
            #   * repeats are debug-only and never touch published state;
            #   * the brain router is NOT failed from here at all. An input-path
            #     exception happens outside any provider call - provider
            #     failures are already marked in the turn-error path below
            #     (`mark_failure(reason=turn_error)`), which is the one place
            #     that knows a provider actually broke.
            # Subsequent input events keep being processed either way: the
            # exception is caught per chunk and the loop never unwinds.
            _err_text = str(e)
            _seen = server_state.get("_input_error_seen")
            if not isinstance(_seen, list):
                _seen = []
                server_state["_input_error_seen"] = _seen
            if _err_text in _seen:
                _flog.debug("input error (repeat suppressed): %s", _err_text[:200])
                return
            _seen.append(_err_text)
            del _seen[:-8]  # bounded: at most 8 remembered messages
            _flog.warning("input error: %s", e)
            print(f"[STT/input error: {e}]")
            publish_status(STATUS_ERROR, f"Input error: {e}")
            output_queue = getattr(interpreter, "output_queue", None)
            if output_queue is not None:
                emit_input_terminal(output_queue)
        finally:
            if (isinstance(chunk, dict) and "end" in chunk
                    and ("capture_epoch" not in locals()
                         or capture_epoch == server_state.get("voice_capture_epoch", 0))):
                server_state["background_voice_until"] = 0

    old_output = interpreter.output

    async def _finish_owned_turn(owner, *, after_notice=False):
        if _ownership(server_state) is not owner:
            return
        if (not after_notice and not owner.is_external
                and server_state.get("terminal_notice_owner") is owner
                and server_state.get("cancelled_owner") is not owner):
            server_state["closing_attempt"] = server_state["turn_attempt"]
            _put_owned({"role": "server", "type": "status", "content": "complete",
                        "_friday_finish_only": True})
            return
        with execution_lock:
            knowledge = server_state.get("knowledge_turn")
            if knowledge is not None and knowledge.owner is owner:
                del interpreter.messages[knowledge.history_size:]
                server_state["knowledge_turn"] = None
            _clear_ownership(server_state, expected=owner)
        if archive is not None:
            archive.finish(owner)
        if not await _drain_pending() and not server_state["is_paused"] and not owner.is_external:
            if open_hot_window():
                interpreter.output_queue.sync_q.put({
                    "hot_window": True, "active": True, "seconds": server_state["hot_window_seconds"],
                })
        maybe_run_digest(server_state, status_bus)

    async def new_output(self):
        while True:
            try:
                queued_output = await old_output()
                current, output = _current_output(server_state, queued_output)
                if not current:
                    continue
                if isinstance(queued_output, _AttemptOutput) and queued_output.playback:
                    # A previous local utterance may still be audible while a
                    # silent request runs. Its audio is transport only.
                    if (isinstance(output, bytes)
                            and server_state.get("playback_owner") is _ownership(server_state)):
                        _record_tts_chunk()
                    if isinstance(output, bytes):
                        output = audio_link.packet(queued_output.attempt, output)
                        if output is None:
                            continue
                    return output
                if isinstance(output, dict) and (
                    output.get("speech_stop") is True or output.get("hot_window") is True
                ):
                    return output
                # ROUND W: a delivered output chunk is the strongest evidence of
                # real forward progress there is. Recording it explicitly lets
                # the turn-budget check tell "slow but working" from "wedged"
                # without guessing.
                if isinstance(queued_output, _AttemptOutput):
                    _stamp_progress(server_state)
                    server_state["turn_progress_observed"] = True
                # ROUND ZB-P1: bound ONE tool's wall clock. The budgets above
                # only fire on NO progress, and a tool that keeps printing makes
                # progress forever - see enforce_tool_timeout for the reasoning.
                _tool_over = enforce_tool_timeout(tool_timing, interpreter)
                if _tool_over is not None:
                    trace("tool_timeout", elapsed=round(_tool_over, 1))

                if isinstance(output, dict) and output.get("ignored") is True:
                    return {"ignored": True, "end": True}

                if isinstance(output, dict) and output.get("voice_paused") is not None:
                    return output

                if isinstance(output, bytes):
                    return output

                await asyncio.sleep(0)
                if not _current_output(server_state, queued_output)[0]:
                    continue
                owner = _ownership(server_state)
                is_complete = (isinstance(output, dict) and output.get("type") == "status"
                               and output.get("content") == "complete")
                if isinstance(queued_output, _AttemptOutput) and not queued_output.playback:
                    if (server_state.get("closing_attempt") is queued_output.attempt
                            and not (isinstance(output, dict) and (
                                output.get("_friday_finish_only") or output.get("_friday_authored")))):
                        continue
                    if server_state.get("cancelled_owner") is owner and not is_complete:
                        continue

                # POST-HARD-CAP DRAIN (2026-11-06). The websocket output loop
                # re-enters new_output after every return, so skipping ONE chunk
                # (the old `return` in the hard-stop block) did not terminate
                # anything: queued residue of the killed turn - late tool
                # start/end/console frames, the model's own assistant text -
                # kept flowing, fired the cap a SECOND time (#26 after #25) and
                # produced two competing final narrations. Once the cap has
                # fired for this turn, everything is drained EXCEPT the three
                # things that still own the terminal outcome:
                #   * FRIDAY's own queued frames (tagged `_friday_authored` by
                #     queue_speech_only / queue_direct_response) - a terminal
                #     narration or deterministic reply IS the single outcome;
                #   * OI's `status: complete` - the one turn-finalisation;
                #   * an error frame, so a real failure is still reported.
                if server_state.get("tool_cap_terminated"):
                    _drain_keep = isinstance(output, dict) and (
                        output.get("_friday_authored")
                        or output.get("_friday_narration")
                        or output.get("speech_stop") is True
                        or output.get("type") in ("status", "error")
                    )
                    if not _drain_keep:
                        continue

                if (server_state.get("identity_turn") is not None
                        and isinstance(queued_output, _AttemptOutput)
                        and isinstance(output, dict) and output.get("role") == "assistant"
                        and output.get("type") in ("message", "code")
                        and isinstance(output.get("content"), str) and output["content"].strip()
                        and not (output.get("_friday_authored") or output.get("_friday_narration"))):
                    _identity.brain_output(server_state, queued_output.attempt)

                if isinstance(output, dict) and output.get("type") == "error":
                    turn_error = _redact_text(str(output.get("content") or "unknown error"))[:4000]
                    _flog.warning("turn error: %s", turn_error)
                    if server_state.get("terminal_notice_owner") is owner:
                        continue
                    server_state["turn_failed"] = True
                    server_state["turn_error"] = turn_error
                    publish_status(STATUS_ERROR, "Assistant endpoint error")
                    continue

                if is_complete:
                    if output.get("_friday_finish_only"):
                        await _finish_owned_turn(owner, after_notice=True)
                        continue
                    if server_state.get("cancelled_owner") is owner:
                        server_state["barge_in"] = False
                        server_state["tts_echo_turn_active"] = False
                        _end_single_turn(self, server_state)
                        _flush_turn_log(server_state)
                        if owner.is_external:
                            await _terminal_notice(owner, "Cancelled.")
                        await _finish_owned_turn(owner)
                        continue
                    trace("response_complete")
                    publish_status(STATUS_IDLE, "Standing by")
                    turn_failed = server_state.get("turn_failed", False)
                    # PHASE 4: snapshot BEFORE the reset below clears it, so the
                    # anomaly record can still say whether the provider failed.
                    _failed_snapshot = (bool(turn_failed),
                                        server_state.get("turn_error") or "")
                    if turn_failed:
                        _observe_owned_brain_result(server_state, queued_output, "failure")
                        server_state["turn_failed"] = False
                        turn_error = server_state.get("turn_error") or "Unknown provider error"
                    elif (server_state.get("provider_attempt")
                          and server_state.get("terminal_notice_owner") is not owner):
                        _observe_owned_brain_result(server_state, queued_output, "success")
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
                    # PHASE 4 (2026-10-03): record a text-less turn HERE, in the
                    # single place every turn passes through. It used to be called
                    # from inside one channel's reply branch
                    # (`capture_chat_id == TEXT_CHANNEL_CHAT_ID`), so VOICE and
                    # TELEGRAM turns could never produce a record and a turn whose
                    # capture had already been cleared was missed entirely -
                    # measured at 5 records against 41 `snapshot_chars=0` events,
                    # with 12 of 32 audit-visible empties never written. Logging is
                    # deliberately NOT routed into the provider-hop path: "empty"
                    # is not "failed", and treating them alike would fire spurious
                    # retries and risk duplicate answers.
                    if turn is None or not turn.get("assistant_snippet"):
                          _log_empty_turn(
                              server_state, turn,
                              # ROUND ZA: read the turn's own accumulator, not
                              # a closure. Falls back to an empty shape so a
                              # caller can never observe a previous turn's data.
                              response_shape=(server_state.get("llm_response_shape")
                                              or _blank_llm_response_shape()),
                              turn_failed=_failed_snapshot[0],
                              turn_error=_failed_snapshot[1],
                          )
                    # ROUND ZB-L4 L4-1: the journal above is truthful but SILENT -
                    # it records why and says nothing to Sir. When the provider
                    # itself truncated the final call (finish=length, no content)
                    # after real work was done, speak. Detection-keyed: no new
                    # timeout, no new guard, no new budget, no new provider call
                    # beyond the single recovery completion.
                    _truncated, _trunc_call = _budget_truncated_turn(
                        server_state, turn)
                    recovered_narration = None
                    if _truncated and server_state.get("terminal_notice_owner") is not owner:
                        _flog.warning(
                            "provider truncated the final call "
                            "(finish=length, content_len=0, max_tokens=%s, "
                            "reasoning_len=%s) after %d tool(s) - recovering",
                            _trunc_call.get("max_tokens"),
                            _trunc_call.get("reasoning_len"),
                            len((turn or {}).get("actions") or []))
                        _rec_text, _rec_source = await _recover_truncated_turn(
                            interpreter, server_state, turn)
                        if server_state.get("cancelled_owner") is owner:
                            if owner.is_external:
                                await _terminal_notice(owner, "Cancelled.")
                            await _finish_owned_turn(owner)
                            continue
                        recovered_narration = _rec_text or None
                        if _rec_source == "narration":
                            _flog.info(
                                "truncated-turn recovery: final narration after "
                                "the provider cut the response short (%d chars)",
                                len(recovered_narration or ""))
                            trace("truncated_turn_narration")
                        else:
                            _flog.info(
                                "truncated-turn recovery: narration unavailable, "
                                "truthful fallback used")
                            trace("truncated_turn_truthful_fallback")
                    # Record what she ACTUALLY did, verbatim, in durable memory.
                    # Sir asked "did you learn from that / will it be faster next
                    # time?" and she said she remembers nothing - because the
                    # only trace was in the per-turn scratchpad, which was
                    # already wiped by the next request. Writing the real answer
                    # here makes that question answerable from evidence.
                    try:
                        from .self_improve import append_outcome

                        snippet = (turn or {}).get("assistant_snippet") or ""
                        if snippet and server_state.get("knowledge_turn") is None:
                            append_outcome(snippet, tag="answered")
                    except Exception:
                        pass

                    ownership = _ownership(server_state)
                    if turn_failed and server_state.get("terminal_notice_owner") is not ownership:
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
                            _flog.warning("hop to next candidate after failure elapsed_ms=%s pool=%s", _turn_elapsed_ms(server_state), getattr(server_state.get("brain_router"), "current_pool", "?"))
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
                                await _terminal_notice(
                                    ownership, "Sorry, Sir — every thinking model is unavailable right now.",
                                    outcome="failed")
                                await _finish_owned_turn(ownership)
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
                            if server_state.get("cancelled_owner") is owner:
                                await _terminal_notice(owner, "Cancelled.")
                                await _finish_owned_turn(owner)
                                continue
                            router = server_state["brain_router"]
                            if (server_state.get("identity_integration") is not None
                                    and router.current_catalog_route_disabled()
                                    and router.apply_for(server_state.get("turn_user_text"), interpreter) is None):
                                await _terminal_notice(owner, "No eligible thinking model is available right now.",
                                                       outcome="failed")
                                await _finish_owned_turn(owner)
                                continue
                            await _feed_turn(server_state.get("turn_content"), owner, retry=True)
                            continue
                        # A turn with effects must never be replayed.
                        await _terminal_notice(
                            ownership, "Sorry, Sir — the assistant couldn't finish that request.",
                            outcome="failed")

                    if server_state.get("terminal_notice_owner") is not ownership:
                        if ownership.is_external:
                            snippet = _clean_user_text(_redact_for_phone(
                                (turn or {}).get("assistant_snippet") or recovered_narration
                                or server_state.get("last_direct_text") or "").strip())
                            if not snippet:
                                snippet = "I produced no verified reply for that request."
                            await _terminal_notice(ownership, snippet, screenshot=True)
                        elif recovered_narration:
                            await _terminal_notice(ownership, recovered_narration)

                    if turn and turn.get("assistant_snippet"):
                        snapshot_text = turn["assistant_snippet"]
                        _flog.info("assistant: %s", _redact_text(snapshot_text))
                        print(snapshot_text, flush=True)
                    note_kind = maybe_record_project_note(turn)
                    if note_kind == "code":
                        _flog.info("note_taker: recorded project note silently")
                    await _finish_owned_turn(ownership)
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
                        # ASSEMBLY (2026-11-06). These frames are DELTAS of ONE
                        # tool call - the live lengths went 25, 15, 15, 2, 22,
                        # 1 ... (never monotonic, so they are not re-sent full
                        # text), which is what OI's own accumulator consumes
                        # (`_respond_and_store`: content += delta). The old
                        # code CLEARED the buffer on every frame, so the
                        # completed row, the log and LAST_TOOL_SCRIPT kept only
                        # the FINAL delta - `code=0)` sat next to a traceback
                        # while the block that actually ran was far longer.
                        # Accumulate every frame exactly as OI does, so the
                        # assembled text equals the executed block.
                        tool_code_parts.append(fragment)
                        assembled = "".join(tool_code_parts)
                        # MEASUREMENT (2026-10-03): stamp when this tool
                        # started so the completion line can report a real
                        # elapsed_ms. Previously tool cost had to be inferred
                        # from two same-second log lines.
                        if tool_timing.get("started") is None:
                            tool_timing["started"] = time.monotonic()
                        _flog.info(
                            "tool script captured: format=%s chars=%s",
                            output.get("format"), len(fragment),
                        )
                        chat_upsert_tool(
                            f"TOOL #{tool_counter['n']} — script "
                            f"({output.get('format') or 'code'}, {len(assembled)} chars):\n"
                            f"{assembled[:2000]}",
                            key=f"tool-{tool_counter['n']}-code",
                            status="streaming",
                        )

                if isinstance(output, dict) and output.get("type") == "code":
                    # BOUNDED BEFORE USE (2026-10-03). `language` is read by the
                    # tool-END branch below, but was only ever assigned inside the
                    # tool-START branch. A terminal chunk that arrived without a
                    # preceding start therefore raised "cannot access local
                    # variable 'language'" and aborted the rest of that chunk.
                    # The value is derived purely from `output`, so computing it
                    # once here is equivalent and keeps both branches working.
                    language = str(output.get("language") or "code").strip()
                    # The frame-by-frame ASSEMBLY lives in the capture block
                    # above (single assembler, both payload keys). This second
                    # block used to re-accumulate `code`-keyed frames on top of
                    # that, which could double a frame; it now only handles the
                    # start/end boundaries below.
                    if output.get("start"):
                        tool_code_parts.clear()
                        tool_counter["n"] += 1
                        if tool_counter["n"] > HARD_TOOL_CAP:
                            if server_state.get("tool_cap_terminated"):
                                # The cap already fired once for THIS turn. The
                                # drain upstream should have swallowed this
                                # frame; if it ever reaches here, still never
                                # re-narrate, re-notify or re-publish the stop.
                                return
                            # Hard stop: the soft nudge was ignored; kill the
                            # agent turn so it cannot keep dry-running tools
                            # (async_core checks stop_event before each chunk).
                            _flog.warning(
                                "HARD tool stop at #%s (cap %s) — terminating victim turn",
                                tool_counter["n"], HARD_TOOL_CAP,
                            )
                            # ROUND ZB-L3 L3-2: block THIS tool outright. Setting
                            # stop_event is cooperative - the streaming handler
                            # kept running and tool #25 executed anyway on the
                            # live turn that produced this code. A cap that
                            # still runs one more tool is not a cap.
                            chat_append(
                                "tool",
                                f"HARD STOP at tool #{tool_counter['n']} — loop "
                                "terminated, no further tools will run",
                                status="complete",
                            )
                            try:
                                interpreter.stop_event.set()
                            except Exception:
                                pass
                            # TERMINATION LATCH (2026-11-06): the turn is over.
                            # From here the POST-HARD-CAP DRAIN in new_output
                            # discards every late frame of this turn - a second
                            # cap fire, another tool's start/end/console, the
                            # model's own text - while still letting FRIDAY's
                            # tagged narration and OI's single `status: complete`
                            # through. Set BEFORE the narration await below, so
                            # the outcome is latched exactly once and the block
                            # below produces the ONE terminal narration.
                            server_state["tool_cap_terminated"] = True
                            if server_state.get("identity_integration") is not None:
                                _identity.close(server_state, _ownership(server_state))
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
                            # ROUND ZB-L3 L3-2: try ONE tools-free completion
                            # first, and fall back to a truthful deterministic
                            # sentence. The old code only ever produced the
                            # generic scratchpad wrap, so a turn that had
                            # genuinely verified a viewport change still said
                            # nothing about it.
                            _summary = _verified_work_summary()
                            _tools_run = tool_counter["n"]
                            _narration = await _final_narration_completion(
                                interpreter,
                                server_state.get("last_user_text") or "",
                                _tools_run,
                                _summary,
                            )
                            if _narration:
                                _flog.info(
                                    "final narration after tool cap: %d chars",
                                    len(_narration),
                                )
                                trace("tool_cap_final_narration")
                            else:
                                _narration = _tool_cap_fallback_text(
                                    _tools_run, _summary)
                                _flog.info(
                                    "final narration unavailable; truthful "
                                    "fallback used (tools=%d verified=%d)",
                                    _tools_run, len(_summary.get("verified") or []),
                                )
                                trace("tool_cap_truthful_fallback")
                            wrap = _narration
                            if server_state.get("cancelled_owner") is not owner:
                                await _terminal_notice(owner, wrap, outcome="interrupted")
                            # Do NOT fall through: this tool must never execute.
                            return
                        trace("tool_start")
                        publish_status(STATUS_THINKING, f"Running tool #{tool_counter['n']}")
                        _play_chirp("tool_start")
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
                        # HONEST OUTCOME (2026-11-06). OI never sets a success
                        # flag on a code-end frame: that frame means the model
                        # finished EMITTING the code, not that it ran. The old
                        # default-True read of the missing key therefore
                        # reported success=True for every tool - including the
                        # run whose console then printed a FileNotFoundError
                        # traceback.
                        # Three states, and only two of them are claims:
                        #   None  -> code captured; execution not yet observed
                        #   False -> its console output showed a failure
                        #   True  -> console output arrived, no failure seen
                        # The console branch below is what promotes None to
                        # True/False ("process invoked" vs "task completed").
                        success = output.get("success")
                        if success is None:
                            _outcome = "captured"
                        elif success:
                            _outcome = "ok"
                        else:
                            _outcome = "FAILED"
                        # MEASUREMENT (2026-10-03): report real tool wall time.
                        # Previously the captured and completed lines landed in
                        # the same log second, so tool cost had to be inferred.
                        tool_elapsed_ms = None
                        if tool_timing.get("started") is not None:
                            tool_elapsed_ms = int(
                                (time.monotonic() - tool_timing["started"]) * 1000)
                            tool_timing["started"] = None
                        if tool_code:
                            _flog.info(
                                "tool code captured: outcome=%s elapsed_ms=%s code=%s",
                                _outcome, tool_elapsed_ms, _redact_text(tool_code)[:800])
                            global LAST_TOOL_SCRIPT
                            LAST_TOOL_SCRIPT = {
                                "tool_number": tool_counter["n"],
                                "language": language,
                                "success": success,
                                "code": _redact_text(tool_code)[:4000],
                                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                            chat_upsert_tool(
                                f"TOOL #{tool_counter['n']} — script ({len(tool_code)} chars, "
                                f"{_outcome}):\n{tool_code[:2000]}",
                                key=f"tool-{tool_counter['n']}-code",
                                status="complete",
                            )
                            try:
                                print(f"[Tool #{tool_counter['n']} {_outcome} code]\n{tool_code[:2000]}", flush=True)
                            except Exception:
                                pass
                            try:
                                from .self_improve import scratchpad_note

                                scratchpad_note(
                                    f"tool #{tool_counter['n']} ({_outcome}): "
                                    f"{tool_code[:160]}",
                                    tag="tool",
                                )
                            except Exception:
                                pass
                        else:
                            _flog.info(
                                "tool code captured: outcome=%s elapsed_ms=%s",
                                _outcome, tool_elapsed_ms)
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
                            # ROUND Q (2026-10-03): DEFERRAL. This used to append
                            # the nudge straight into interpreter.messages here.
                            # It fired from the tool-complete handler, which runs
                            # BEFORE OI appends this tool's console/result row -
                            # so the nudge landed wedged between the tool call
                            # and its result. OI pairs those positionally, and
                            # its orphan branch then rebuilt a tool call with
                            # non-JSON arguments on EVERY later request, 400ing
                            # every provider until the process restarted.
                            # Stash it instead; traced_completions flushes it
                            # once the result row exists, and discards it if the
                            # turn ends first. The guard still fires and the
                            # model still gets its one further chance - only the
                            # POSITION in history changed.
                            server_state["pending_tool_cap_nudge"] = guard_text
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
                    # ---- FRUITLESS SCREEN-SEARCH GUARD (ROUND ZB-FS) ----------
                    # Deliberately BEFORE the flood guard below it, because that
                    # one demands byte-identical repetition and therefore never
                    # fired on the failure this exists for: the model reworded
                    # every empty result ("test marker" / "marker" /
                    # "TEST MARKER"), so `same_run` reset on every line.
                    if not turn_guard.get("flood_stopped"):
                        _miss_target = search_miss_target(console_text)
                        if _miss_target is not None:
                            turn_guard["search_misses"] = (
                                turn_guard.get("search_misses", 0) + 1
                            )
                            if _miss_target:
                                turn_guard["search_miss_target"] = _miss_target
                            _misses = turn_guard.get("search_misses", 0)
                            if _misses >= SEARCH_MISS_CAP:
                                # One more attempt cannot add anything: the same
                                # lookup has already failed this many times.
                                _missed_what = (
                                    str(turn_guard.get("search_miss_target") or "")
                                    .strip() or "that text"
                                )
                                _flog.warning(
                                    "fruitless search guard: %s empty screen "
                                    "searches (cap %s) - stopping turn",
                                    _misses, SEARCH_MISS_CAP,
                                )
                                turn_guard["flood_stopped"] = True
                                publish_status(
                                    STATUS_ERROR,
                                    "I stopped searching - it is not on screen",
                                )
                                chat_append(
                                    "tool",
                                    "SEARCH STOPPED - I looked for %s on screen %s "
                                    "times and it is not there, so I stopped "
                                    "rather than search again"
                                    % (_missed_what, _misses),
                                    status="complete",
                                )
                                # The same cooperative mechanism the watchdog,
                                # the flood guard and the HARD tool cap use - the
                                # only one that genuinely ends a running OI turn.
                                try:
                                    interpreter.stop_event.set()
                                except Exception:
                                    pass
                                # TRUTHFUL, BOUNDED, NOT BLAMED ON A LOOP: the
                                # real problem is an absent target, so say that.
                                # Never "my code was looping".
                                _search_wrap = (
                                    "I searched the screen for %s %s times, Sir, "
                                    "and it is not there - so I stopped looking "
                                    "instead of taking more screenshots. It may "
                                    "not be open, may have closed, or may be worded "
                                    "differently. Tell me what else to try."
                                    % (_missed_what, _misses)
                                )
                                await _terminal_notice(_ownership(server_state), _search_wrap,
                                                       outcome="interrupted")
                                _record_turn_output(server_state, output)
                                continue
                    # ---- INFINITE-LOOP / FLOOD GUARD (2026-09-30) -------------
                    # Observed live: after a Groq 429 failover, the brain wrote
                    # python containing an unbounded loop that printed "Task is
                    # not complete. Continuing..." forever. Because no `end`
                    # event ever arrived, the tool counter NEVER advanced, so
                    # the tool-cap loop guard could not fire and the turn hung
                    # indefinitely - FRIDAY simply never answered. The counter
                    # is the wrong place to catch this, so the repetition guard below and the
                    # independent turn-budget/silent-hang checks bound it.
                    turn_guard["console_events"] = turn_guard.get("console_events", 0) + 1
                    elapsed = time.monotonic() - turn_guard["started"]
                    # A real runaway repeats the SAME output forever. A
                    # legitimate turn (a slow vision chain, a long OCR pass)
                    # produces VARIED output. The first version of this guard
                    # only counted events and killed a perfectly good vision
                    # turn, which FRIDAY then reported to the phone as "the
                    # code I wrote started looping" - a confident, false
                    # statement about work that never looped. So the flood trip
                    # now requires REPETITION, not volume.
                    repeat_key = console_text[:200]
                    if turn_guard.get("last_console") == repeat_key:
                        turn_guard["same_run"] = turn_guard.get("same_run", 0) + 1
                    else:
                        turn_guard["same_run"] = 1
                    turn_guard["last_console"] = repeat_key
                    over_events = turn_guard["same_run"] > TOOL_FLOOD_EVENT_CAP
                    # ROUND W: the turn budget is no longer enforced HERE. It used
                    # to be, which meant it only existed for turns that printed
                    # something. It is now enforced by the watchdog tick
                    # (`_turn_budget_check`) on EVERY tick regardless of console
                    # output, so a silent hang is caught too. Only the genuine
                    # runaway signal - repetition - is judged at this point.
                    over_time = False
                    if over_events:
                        if not turn_guard.get("flood_stopped"):
                            turn_guard["flood_stopped"] = True
                            reason = f"same output {turn_guard['same_run']}x in a row"
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
                            # her. Mirrors the HARD tool-cap path. Wording is
                            # chosen to match the ACTUAL reason - never claim a
                            # loop when the real problem was elapsed time.
                            if over_events:
                                flood_wrap = (
                                    "I stopped that myself - the code I wrote kept "
                                    "repeating itself instead of finishing, so I "
                                    "killed it rather than hang. Ask me again and "
                                    "I'll take a direct route."
                                )
                            else:
                                flood_wrap = (
                                    "I gave up on that one - it ran past my time "
                                    "limit without finishing, so I stopped it rather "
                                    "than hang. Ask me again and I'll take a "
                                    "shorter route."
                                )
                            await _terminal_notice(_ownership(server_state), flood_wrap,
                                                   outcome="interrupted")
                        # Keep the log, but stop flooding it and the feed.
                        if turn_guard["console_events"] % 50 == 0:
                            _flog.info(
                                "tool console suppressed after runaway stop (%s total)",
                                turn_guard["console_events"],
                            )
                        _record_turn_output(server_state, output)
                        continue
                    # PROCESS INVOKED vs TASK COMPLETED (2026-11-06). The
                    # code-end frame above could only say "captured"; THIS is
                    # where the run's own output decides the outcome. A
                    # traceback flips the recorded success to False and
                    # rewrites the tool row, so a success flag can no longer
                    # sit beside an exception; a clean console promotes the
                    # pending None to True. Placed AFTER the fruitless-search
                    # guard (which must stay immediately after `console_text`)
                    # and AFTER the flood guard, so a runaway turn still
                    # short-circuits here first. Bounded to the current tool.
                    if _console_shows_failure(console_text):
                        _fail_n = tool_counter["n"]
                        if (isinstance(LAST_TOOL_SCRIPT, dict)
                                and LAST_TOOL_SCRIPT.get("tool_number") == _fail_n
                                and LAST_TOOL_SCRIPT.get("success") is not False):
                            _flog.warning(
                                "tool #%s output shows a failure; recording FAILED",
                                _fail_n,
                            )
                            LAST_TOOL_SCRIPT["success"] = False
                            LAST_TOOL_SCRIPT["output"] = _redact_text(console_text)[:1000]
                            _failed_code = "".join(tool_code_parts).strip()
                            if _failed_code:
                                chat_upsert_tool(
                                    f"TOOL #{_fail_n} — script ({len(_failed_code)} chars, "
                                    f"FAILED):\n{_failed_code[:2000]}",
                                    key=f"tool-{_fail_n}-code",
                                    status="complete",
                                )
                    elif (isinstance(LAST_TOOL_SCRIPT, dict)
                          and LAST_TOOL_SCRIPT.get("tool_number") == tool_counter["n"]
                          and LAST_TOOL_SCRIPT.get("success") is None):
                        LAST_TOOL_SCRIPT["success"] = True
                        LAST_TOOL_SCRIPT["output"] = _redact_text(console_text)[:1000]
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
                    if (isinstance(queued_output, _AttemptOutput) and output.get("role") == "assistant" and not owner.is_external
                            and isinstance(content, str)
                            and output.get("format") not in ("analysis", "reasoning", "tool", "function")
                            and not _looks_like_tool_json(content)):
                        with execution_lock:
                            companion.observe_text(owner, queued_output.attempt, _redact_text(content), live=lambda:
                                _ownership(server_state) is owner
                                and server_state.get("turn_attempt") is queued_output.attempt
                                and server_state.get("cancelled_owner") is not owner
                                and not server_state.get("barge_in"))
                    if (archive is not None and isinstance(queued_output, _AttemptOutput)
                            and output.get("role") == "assistant" and not owner.is_external
                            and output.get("format") not in ("analysis", "reasoning", "tool", "function")):
                        archive.append(owner, queued_output.attempt, queued_output.event_id, content,
                                       notice=(server_state.get("terminal_notice_owner") is owner
                                               and bool(output.get("_friday_authored"))))
                    # Capture every assistant message for the UI feed
                    knowledge = server_state.get("knowledge_turn")
                    references = (knowledge.references(server_state["turn_attempt"])
                                  if knowledge is not None and knowledge.owner is owner else [])
                    warning = ("Local Knowledge Base was unavailable. This answer continued without imported sources."
                               if server_state.get("knowledge_read_failed") else None)
                    if references or warning:
                        chat_append("assistant", content, status="streaming",
                                    knowledge={"references": references, "warning": warning})
                    else:
                        chat_append("assistant", content, status="streaming")
                    _record_turn_output(server_state, output)
                    if knowledge is None and isinstance(content, str) and content and not _looks_like_tool_json(content):
                        _flog.info("assistant: %s", _redact_text(content))
                        try:
                            print(content, flush=True)
                        except Exception:
                            pass
                    if not _is_tts_text_speakable(content, self.tts.text()):
                        continue
                    if content.strip() in (".", "!", "?", ","):
                        continue
                    if _ownership(server_state).muted:
                        # Telegram turn: the answer goes to the phone chat,
                        # nothing is spoken through the local head unit.
                        if server_state["tts_echo_turn_active"]:
                            server_state["tts_echo_turn_active"] = False
                        continue
                    if not server_state["tts_echo_turn_active"]:
                        server_state["recent_tts_texts"] = []
                        server_state["tts_echo_buffer"] = ""
                        server_state["tts_echo_turn_active"] = True
                    # ROUND ZB-REC: repair a latched stream, then refuse to feed
                    # text that provably cannot be synthesized. Feeding is what
                    # lets RealtimeTTS play() re-enter itself
                    # (text_to_stream.py:448) until RecursionError latches the
                    # stream for the rest of the process.
                    _tts_recover_latch(self.tts)
                    if tts_is_degraded(server_state):
                        status = note_tts_degraded(server_state)
                        trace("tts_degraded",
                              reason=status.get("reason", ""),
                              retry_in=status.get("retry_in_seconds", 0.0))
                        # The assistant text has already been logged and pushed
                        # to the chat buffer above; we only withhold AUDIO,
                        # because there is no engine left to produce it.
                        continue
                    # ROUND ZB-CH: turn/update-aware admission. The UI text has
                    # ALREADY been streamed above - we never hold it back for
                    # audio. This only governs the audio path: newer screen/chess
                    # guidance supersedes older guidance that has not been
                    # spoken, instead of playing it late (measured: one stream
                    # ran 76s while updates arrived every ~18s, leaving the
                    # voice a full update behind the screen).
                    # Read from server_state only: no relative import here,
                    # because some suites exec this branch in a bare namespace.
                    _speech = server_state.get("speech_queue")
                    decision = (_speech.admit(content) if _speech is not None
                                else {"preempt": False, "dropped": []})
                    if decision.get("preempt"):
                        # Superseded/stale text is already inside RealtimeTTS;
                        # clearing it is the only way to drop it. The sentence
                        # being cut is itself the stale guidance we are
                        # replacing, so this is necessary, not gratuitous.
                        self.tts.stop()
                        # ROUND ZB-C: superseded audio must not linger either -
                        # tell the client to drop it and clear the captions.
                        notify_speech_stop(server_state, "superseded",
                                           queue=self.output_queue)
                        trace("tts_preempted",
                              reason=decision.get("reason", ""),
                              dropped=len(decision.get("dropped") or []))
                        for item in decision.get("dropped") or []:
                            trace("tts_superseded", seq=item.get("seq"),
                                  age=item.get("age_seconds"),
                                  text=item.get("text"))
                    playback = _speech_playback(self)
                    self.tts.feed(content)
                    if _speech is not None:
                        _speech.mark_fed()
                    _remember_tts_text(server_state, content)
                    # ROUND ZB-C: label the phrase so the client can caption it
                    # and acknowledge THAT phrase (unframed PCM has no edges).
                    # try/except on purpose: several suites execute this branch
                    # in a bare namespace where `emit_speech_frame` does not
                    # exist, and a captioning failure must never break speech.
                    try:
                        _frame = emit_speech_frame(server_state, content)
                        if _frame:
                            trace("speech_frame", seq=_frame["seq"],
                                  utterance=_frame["utterance"])
                            self.output_queue.sync_q.put(_AttemptOutput(playback, _frame, playback=True))
                    except Exception:
                        pass

                    if not self.tts.is_playing() and any([c in delimiters for c in content]): 
                        publish_status(STATUS_SPEAKING, "FRIDAY is speaking")
                        _start_owned_playback(self, muted=not self.play_audio, sentence_fragment_delimiters=delimiters, minimum_sentence_length=5)
                        trace("tts_start", tts_stage=_tts_stage_now())
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "start": True}

                if (isinstance(output, dict) and output.get("role") == "assistant"
                        and output.get("type") == "message" and output.get("end") is True):
                    trace("final_response_end")
                    if server_state["barge_in"]:
                        # Interrupted response segment ended. Stay muted for the
                        # rest of the response; the flag clears at "complete".
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
                    if _ownership(server_state).muted:
                        # Telegram turn: no local playback; audio already
                        # directed at the phone chat via text.
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
                    if not self.tts.is_playing():
                        # ROUND ZB-REC: a RecursionError inside play() leaves
                        # is_playing_flag latched while is_playing() is False,
                        # so this branch looks "idle" and play_async() would
                        # silently refuse forever. Repair before starting.
                        _tts_recover_latch(self.tts)
                        if tts_is_degraded(server_state):
                            status = note_tts_degraded(server_state)
                            trace("tts_degraded",
                                  reason=status.get("reason", ""),
                                  retry_in=status.get("retry_in_seconds", 0.0))
                            # No engine can speak: end the audio stream cleanly
                            # instead of claiming to start playback.
                            return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
                        publish_status(STATUS_SPEAKING, "FRIDAY is speaking")
                        _start_owned_playback(self, muted=not self.play_audio, sentence_fragment_delimiters=delimiters, minimum_sentence_length=5)
                        _seal_owned_playback(self)
                        # Same marker as the streaming branch above: this path
                        # also starts playback, so without it a turn that only
                        # spoke here would log `tts_first_audio` with no
                        # `tts_start` to pair with.
                        trace("tts_start", tts_stage=_tts_stage_now())
                        return {"role": "assistant", "type": "audio", "format": "bytes.wav", "start": True}
                    _seal_owned_playback(self)
                    return {"role": "assistant", "type": "audio", "format": "bytes.wav", "end": True}
            except Exception as e:
                _flog.warning("output error: %s", e)
                print(f"[TTS/output error: {e}]")
                publish_status(STATUS_ERROR, f"Output error: {e}")
                await asyncio.sleep(0.5)

    def on_tts_chunk(self, chunk, playback):
        if server_state.get("playback_attempt") is playback:
            self.output_queue.sync_q.put(_AttemptOutput(playback, chunk, playback=True))

    def _record_tts_chunk():
        # ROUND ZB-REC: real audio is arriving again, so the degradation record
        # is stale. The engine clears its own breaker on the same signal.
        if server_state.get("tts_degraded"):
            server_state["tts_degraded"] = None
        # ROUND ZB-CH: observe the speech queue so queued->synthesized lag is
        # measurable. Deliberately reached through server_state with NO import:
        # some suites extract this function and execute it in a bare namespace
        # where a relative import would raise. Absent queue => no-op.
        _speech = server_state.get("speech_queue") if isinstance(server_state, dict) else None
        if _speech is not None:
            try:
                _speech.on_chunk()
            except Exception:
                pass
        if not server_state.get("tts_audio_seen"):
            server_state["tts_audio_seen"] = True
            trace("tts_first_audio")

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

    output_lock = asyncio.Lock()
    output_read = None

    async def serialized_output(self):
        nonlocal output_read
        async with output_lock:
            if output_read is None:
                output_read = asyncio.create_task(new_output(self))
            # The Light socket can disconnect while its output reader is
            # delivering an external reply. Preserve that read and let the
            # next socket resume it instead of losing the consumed completion.
            output = await asyncio.shield(output_read)
            output_read = None
            return output

    interpreter.input = types.MethodType(new_input, interpreter)
    interpreter.output = types.MethodType(serialized_output, interpreter)
    interpreter.on_tts_chunk = types.MethodType(on_tts_chunk, interpreter)
    interpreter.set_session_voice_input = _set_voice_input
    interpreter.server.app.add_middleware(AudioWebSocket, link=audio_link, interpreter=interpreter)
    from .accounts import attach_accounts
    attach_accounts(interpreter.server.app)

    # ROUND ZB-EXPOSE: when n8n's cloud workflow can reach FRIDAY, this port
    # becomes reachable from the internet as well - and it serves /chat (full
    # tool access), the screen-history frames and /open_app. Those routes were
    # written for a localhost-only server and trusted their caller.
    # public_access.check_access() closes exactly that hole: anything arriving
    # through a tunnel must present the shared secret, while the local HUD
    # (which connects directly, with no forwarding header) is untouched.
    from .public_access import check_access as _check_access

    def _guard(request):
        """Deny with a truthful status when a forwarded caller has no secret.

        Returns None to mean "allowed"; every guarded route returns this
        straight through when it is not None.
        """
        denied = _check_access(request)
        if denied is None:
            return None
        status, message = denied
        return JSONResponse({"ok": False, "error": message}, status_code=status)

    @interpreter.server.app.get("/ping")
    async def ping():
        return PlainTextResponse("pong")

    @interpreter.server.app.get("/open_app")
    async def open_app(request: Request):
        """Second-launch handler: raise the HUD window of THIS instance
        (the tray-boot instance keeps it hidden until Sir opens it).

        2026-11-04: this used to call pywebview's `window.show()/restore()`
        from the asyncio thread. Those must run on the GUI thread - measured
        live, this route hung for the full 8s client timeout while `/ping`
        answered in 361ms on the same healthy server. Win32 ShowWindow is
        safe from any thread, so the window is driven by hwnd instead.
        """
        denied = _guard(request)
        if denied is not None:
            return denied

        try:
            import win32con
            import win32gui
            hwnd = win32gui.FindWindow(None, "FRIDAY")
            if not hwnd:
                return PlainTextResponse("no window")
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                try:
                    win32gui.BringWindowToTop(hwnd)
                except Exception:
                    pass
        except ImportError:
            # Non-Windows fallback: still correct when pywebview can help.
            try:
                import webview as _webview
                for window in _webview.windows:
                    window.show()
                    window.restore()
            except Exception:
                return PlainTextResponse("no window")
        except Exception:
            return PlainTextResponse("no window")
        # LAB-3R7: showing the window IS presence "open" - the tray's
        # _show_app already calls set_presence("open"), but this HTTP path
        # only drove Win32, so a persisted presence=away (window-X or tray
        # Close App from a previous session) kept world.paused=1 while the
        # HUD was visible: the town stood still with a RUNNING ticker.
        try:
            from . import lab_state as _ls
            _ls.set_presence("open")
        except Exception:
            pass
        from .status_ui import note_window_open
        note_window_open()
        return PlainTextResponse("FRIDAY window raised")

    # -- ROUND ZB-L3 L3-1: cross-process screen history ----------------------
    #
    # WHY THIS EXISTS. The screen-history service (ZB-L2) lives in THIS process,
    # behind a process-wide singleton. The model's authored python runs in a
    # separate `ipykernel_launcher` process, so when she called
    # `describe_recent_screen()` from there it saw a DIFFERENT, empty singleton
    # and returned ok=False. Proved live on 2026-10-07: she read Notepad's
    # on-disk tab-state file instead and answered from that, because the real
    # feature was unreachable from the only place she can call code.
    #
    # These routes close that boundary by reading THIS process's real capture
    # singleton. Deliberately READ-ONLY and deliberately NO second capture
    # service and NO second history buffer: the kernel must not start its own
    # dxcam grab loop, or two processes would race for the same DXGI outputs and
    # each would hold a private copy of the frames.
    @interpreter.server.app.get("/screen/history")
    async def screen_history(request: Request, age: float = 0.0, monitor: int = 0):
        """Frames retained in memory from the last FRIDAY_SCREEN_HISTORY_SECONDS.

        `age` is seconds back from now; 0 means "the newest retained frame".
        Returns the frame's TRUE age plus a JPEG so the caller can read it. The
        age is never rounded away: a caller must be able to tell 4s ago from
        40s ago, because "what was there then" and "what is there now" are
        different questions.
        """
        denied = _guard(request)
        if denied is not None:
            return denied
        from .screen_capture_service import HISTORY_UNAVAILABLE, history_enabled
        from . import screen_capture_service as _cap

        if not history_enabled():
            return JSONResponse({
                "ok": False, "reason": "screen history is disabled",
                "text": HISTORY_UNAVAILABLE,
            })
        try:
            found = _cap.history_image(monitor, float(age or 0.0))
        except Exception as history_error:
            return JSONResponse({
                "ok": False,
                "reason": "history read failed: %s" % type(history_error).__name__,
            })
        if found is None:
            # Honest unavailable. NEVER substitute the live screen here.
            return JSONResponse({
                "ok": False,
                "reason": ("no retained frame at %.1fs old on monitor %s"
                           % (float(age or 0.0), monitor)),
                "text": HISTORY_UNAVAILABLE,
            })
        found_age, image = found
        try:
            import io as _io

            from .screen_understanding import _encode_jpeg

            jpeg = _encode_jpeg(image)
        except Exception as encode_error:
            return JSONResponse({
                "ok": False,
                "reason": "could not encode frame: %s"
                          % type(encode_error).__name__,
            })
        if not jpeg:
            return JSONResponse({
                "ok": False, "reason": "could not encode frame",
            })
        # base64 so the payload is plain JSON and needs no multipart parser.
        import base64 as _b64

        return JSONResponse({
            "ok": True,
            "age_seconds": round(float(found_age), 2),
            "as_of": round(time.time() - float(found_age), 2),
            "monitor": int(monitor),
            "is_live_screen": False,
            "width": int(getattr(image, "width", 0) or 0),
            "height": int(getattr(image, "height", 0) or 0),
            "jpeg_b64": _b64.b64encode(jpeg).decode("ascii"),
        })

    @interpreter.server.app.get("/screen/history/stats")
    async def screen_history_stats(request: Request):
        """How much history is retained right now, per monitor."""
        denied = _guard(request)
        if denied is not None:
            return denied
        from . import screen_capture_service as _cap

        try:
            return JSONResponse(_cap.history_stats())
        except Exception as stats_error:
            return JSONResponse({
                "enabled": False,
                "reason": type(stats_error).__name__,
            })

    @interpreter.server.app.post("/chat")
    async def chat(request: Request):
        """No-voice text command: POST the raw text (or {"text": "..."}) and
        receive FRIDAY's final reply. Runs the SAME dispatch_text chain as the
        HUD chat input and Telegram, so routers/memory/screen/brain all apply.
        The reply is delivered to the text_reply_queue instead of being spoken."""
        # Guard FIRST: this endpoint runs a full turn with tool access, so a
        # forwarded caller must present the shared secret. Checked before the
        # body is read so an unauthenticated caller costs no work at all.
        denied = _guard(request)
        if denied is not None:
            return denied
        raw = (await request.body()).decode("utf-8", errors="replace").strip()
        text_in = raw
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    # Accept every common envelope key. This used to read ONLY
                    # "text", so a caller posting {"message": "..."} had its whole
                    # JSON body treated as Sir's literal words - which is also why
                    # the journal recorded '{"message": ...}' verbatim.
                    for _key in ("text", "message", "prompt", "content", "query"):
                        _val = parsed.get(_key)
                        if isinstance(_val, str) and _val.strip():
                            text_in = _val.strip()
                            break
            except Exception:
                pass
        if not text_in:
            return PlainTextResponse("empty request", status_code=400)
        timeout = float(os.environ.get("FRIDAY_TEXT_TIMEOUT", "240"))
        # 2026-10-01: every request gets its OWN reply box. Previously both
        # callers waited on ONE shared asyncio.Queue, so two simultaneous /chat
        # requests could receive each other's answer - or one could hang while
        # the other consumed the reply. The token is handed to dispatch_text so
        # every chunk of this turn is routed to this caller only.
        reply_token = _new_reply_token(server_state)
        # ROUND ZB-I: give this call an explicit lifecycle. The record is what
        # makes "expired" a real state instead of an implication of a 504 - so
        # late work can be recognised as late rather than surviving ownerless.
        chat_requests = server_state.get("chat_requests") or LifecycleRegistry()
        server_state["chat_requests"] = chat_requests
        chat_request = chat_requests.register(
            ChatRequest(reply_token, text_in, timeout))
        chat_request.sweep()   # refuse already-expired before doing anything
        # FRIDAY has ONE brain and one interpreter, so two turns genuinely
        # cannot both run: turn_channel/telegram_capture/turn_reply_token are
        # single slots. Letting two /chat requests interleave is therefore not
        # a feature - it is how callers received each other's replies. Serialize
        # them and report honestly if one is already in flight.
        turn_lock = server_state.setdefault("turn_lock", asyncio.Lock())
        try:
            await asyncio.wait_for(turn_lock.acquire(), timeout=timeout)
        except asyncio.CancelledError:
            chat_request.cancel()
            _close_reply_box(server_state, reply_token)
            raise
        except asyncio.TimeoutError:
            # Never owned the turn: expire rather than leave a queued record
            # that a later sweep would mistake for pending work.
            chat_request.expire()
            _close_reply_box(server_state, reply_token)
            return PlainTextResponse(
                "NOT ACCEPTED (busy): another turn is already running",
                status_code=503,
            )
        if not chat_request.activate():
            # Expiry is decided in exactly one place (sweep/activate). If we got
            # here past the deadline, do no work for a caller who is gone.
            turn_lock.release()
            _close_reply_box(server_state, reply_token)
            return PlainTextResponse("(request expired)", status_code=504)
        try:
            result = await dispatch_text(
                text_in, reply_channel="text", reply_token=reply_token)
            if not result.get("accepted"):
                # Do NOT answer a rejected dispatch with a bare 500. Report WHY,
                # with a status the caller can actually branch on: 503 while
                # FRIDAY is paused or still booting, 400 for bad input, 502
                # otherwise. A bare 500 told my test harness the request was
                # malformed when in truth it simply arrived mid-turn.
                _status = str(result.get("status") or "")
                _detail = str(result.get("message") or result)
                if _status in ("paused", "unavailable", "busy"):
                    code = 503
                elif _status == "invalid":
                    code = 400
                else:
                    code = 502
                # Never ran - `cancelled` is the honest state (the contract has
                # no "rejected", and expired would wrongly imply it timed out).
                chat_request.cancel()
                return PlainTextResponse(
                    "NOT ACCEPTED (%s): %s" % (_status or "unknown", _detail),
                    status_code=code,
                )
            waiter = (server_state.get("reply_waiters") or {}).get(reply_token)
            if waiter is None:
                return PlainTextResponse("(reply box lost)", status_code=502)
            try:
                # Drain every chunk of this turn, so a multi-sentence answer is
                # not silently truncated to its first fragment.
                deadline = time.monotonic() + timeout
                parts = []
                while True:
                    remaining = max(0.05, deadline - time.monotonic())
                    parts.append(await asyncio.wait_for(waiter.get(), timeout=remaining))
                    if not waiter.empty() or time.monotonic() >= deadline:
                        break
                    # Give the turn a moment to finish before deciding it is done.
                    try:
                        grace = await asyncio.wait_for(waiter.get(), timeout=0.4)
                    except asyncio.TimeoutError:
                        break
                    # FIX (2026-10-18): this value used to be DISCARDED, so the
                    # final chunk of a multi-sentence answer could vanish while
                    # everything else in the turn succeeded.
                    parts.append(grace)
            except asyncio.TimeoutError:
                # The caller is gone. Mark it EXPIRED so the still-running turn's
                # late output has an owner that recognises it as late, instead of
                # falling into an unowned sink with nobody to read it.
                chat_request.expire()
                return PlainTextResponse("(no reply within timeout)", status_code=504)
            if parts:
                chat_request.complete()
            else:
                chat_request.expire()   # nothing reached the caller
            return PlainTextResponse(" ".join(p for p in parts if p).strip())
        except asyncio.CancelledError:
            chat_request.cancel()
            _cancel_http_owner(reply_token)
            raise
        finally:
            turn_lock.release()
            _close_reply_box(server_state, reply_token)
            try:
                chat_requests.sweep()
            except Exception:
                pass

    # --- ROUND ZB-FS: a practical cancel for a runaway turn ------------------
    #
    # WHY THIS EXISTS: on 2026-11-03 a turn that kept re-searching the screen
    # for text that was not there held FRIDAY for 330 s+, and the only ways to
    # end it were the spoken stop phrase (Sir has to be at the mic) or killing
    # the process. /chat had no cancel at all, so a text/HUD caller could not
    # stop a turn it had started.
    #
    # The mechanism is `interpreter.stop_event` - what the watchdog, the flood
    # guard, the fruitless-search guard and the HARD tool cap already use, and
    # the only one that genuinely ends a running Open Interpreter turn. Note
    # `interpreter.stop` DOES NOT EXIST (see _stop_active_turn), so nothing
    # here calls it.
    @interpreter.server.app.post("/chat/cancel")
    async def chat_cancel(request: Request):
        """Stop the running turn and cancel everything waiting behind it.

        Idempotent on purpose: with nothing running it still answers 200 and
        reports `stopped_active_turn: false`, because "cancel when idle" is a
        correct outcome, not an error.
        """
        # Guard FIRST: this endpoint ENDS work, so a forwarded caller must
        # present the shared secret. Checked before the body is read so an
        # unauthenticated caller costs no work at all - the same order /chat
        # uses, and what TestSensitiveRoutesAreActuallyGuarded pins.
        denied = _guard(request)
        if denied is not None:
            return denied

        stopped, cancelled_requests = _cancel_requests(_ownership(server_state), "chat_cancel")
        if stopped or cancelled_requests:
            publish_status(STATUS_IDLE, "Cancelled on request")
        return JSONResponse({
            "ok": True,
            "stopped_active_turn": bool(stopped),
            "cancelled_requests": int(cancelled_requests),
        })

    # --- ROUND ZB-N8N: inbound n8n -> FRIDAY --------------------------------
    #
    # n8n posts a notice here ("new email from X", "calendar reminder", an
    # approval question) and it runs as a REAL turn through the same dispatch
    # chain as Telegram and /chat, so memory, routing, ownership and the
    # /chat lifecycle bookkeeping all apply. The reply is returned as JSON so
    # the workflow can route it.
    #
    # TRUST: this endpoint can make FRIDAY act, so it FAILS CLOSED. With no
    # shared secret configured it refuses everyone, rather than trusting the
    # network to be private. The secret is the SAME one n8n_runtime already
    # sends as `X-Friday-Token` when FRIDAY calls n8n, so there is one secret
    # in both directions. It is compared in constant time and never echoed.
    import hmac as _hmac
    from .n8n_runtime import get_adapter as _n8n_adapter

    @interpreter.server.app.post("/n8n")
    async def n8n_inbound(request: Request):
        presented = str(request.headers.get("x-friday-token") or "").strip()
        if not presented:
            authorization = str(request.headers.get("authorization") or "")
            if authorization.lower().startswith("bearer "):
                presented = authorization[7:].strip()

        shared_secret = str(_n8n_adapter().config.get("secret") or "")
        if not shared_secret:
            return JSONResponse(
                {"ok": False, "error":
                 "n8n inbound is not configured: set `secret` in "
                 "~/.friday/n8n.json (or FRIDAY_N8N_SECRET). It refuses "
                 "everyone while unset."},
                status_code=403,
            )
        if not presented or not _hmac.compare_digest(
                presented.encode("utf-8"), shared_secret.encode("utf-8")):
            return JSONResponse({"ok": False, "error": "unauthorized"},
                                status_code=401)

        raw = (await request.body()).decode("utf-8", errors="replace").strip()
        if len(raw) > 65536:
            return JSONResponse({"ok": False, "error": "payload too large"},
                                status_code=413)
        text_in = raw
        kind = "custom"
        payload = {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    for _key in ("text", "message", "prompt", "content", "query"):
                        value = parsed.get(_key)
                        if isinstance(value, str) and value.strip():
                            text_in = value.strip()
                            break
                    if isinstance(parsed.get("kind"), str):
                        kind = parsed["kind"][:64]
                    if isinstance(parsed.get("payload"), dict):
                        payload = parsed["payload"]
            except Exception:
                pass
        if not text_in:
            return JSONResponse({"ok": False, "error": "empty request"},
                                status_code=400)

        # ROUND ZB-PE: a question only Sir can answer is PARKED, not answered.
        # Returning the question to the workflow as if it were the reply tells it
        # nothing - it already knew the question. So park it with a correlation
        # id, acknowledge IMMEDIATELY (holding this request open for two minutes
        # then five is not reliable), and let the worker ask Sir over voice and
        # then Telegram. The workflow learns the outcome by polling the id, or
        # by FRIDAY calling its `pending_answer` webhook.
        #
        # COMPATIBILITY: a message without an explicit "needs the owner" marker
        # behaves exactly as it did - one turn, one reply.
        needs_owner = bool(payload.get("needs_owner")) or str(kind).lower() in (
            "email_question", "question")
        if needs_owner:
            parked = pending_store.add(
                text_in, kind=kind, payload=payload, source="n8n")
            if parked is None:
                return JSONResponse(
                    {"ok": False, "error":
                     "too many questions are already waiting for Sir"},
                    status_code=429)
            personality.suggest("n8n_decision", parked.id, current=True,
                live=lambda: pending_store.get(parked.id) is parked and parked.is_open()
                and not parked.decision_deadline_passed())
            try:
                asyncio.ensure_future(_pending_worker(parked.id))
            except RuntimeError:
                # No running loop (a test harness, or a rare call context):
                # the question is still parked and pollable, so say so truthfully
                # rather than pretending the worker started.
                pass
            return JSONResponse({
                "ok": True,
                "state": "pending",
                "correlation_id": parked.id,
                "kind": kind,
                "reply": ("Question recorded and awaiting the owner's decision. "
                          "Local status: /n8n/pending/%s." % parked.id),
                "payload": payload,
            })

        # Provenance: tell FRIDAY who is talking. Without it she cannot tell an
        # incoming workflow notice from her own task, and judging "is this
        # important? should I reply?" needs to know it came from n8n.
        text_in = "[incoming from n8n workflow, kind=%s]\n%s" % (kind, text_in)

        timeout = float(os.environ.get("FRIDAY_TEXT_TIMEOUT", "240"))
        reply_token = _new_reply_token(server_state)
        chat_requests = server_state.get("chat_requests") or LifecycleRegistry()
        server_state["chat_requests"] = chat_requests
        chat_request = chat_requests.register(
            ChatRequest(reply_token, text_in, timeout, owner="n8n"))
        chat_request.sweep()

        turn_lock = server_state.setdefault("turn_lock", asyncio.Lock())
        try:
            await asyncio.wait_for(turn_lock.acquire(), timeout=min(timeout, 60.0))
        except asyncio.CancelledError:
            chat_request.cancel()
            _close_reply_box(server_state, reply_token)
            raise
        except asyncio.TimeoutError:
            chat_request.expire()
            _close_reply_box(server_state, reply_token)
            return JSONResponse(
                {"ok": False, "error": "busy", "state": "expired"},
                status_code=503,
            )
        if not chat_request.activate():
            turn_lock.release()
            _close_reply_box(server_state, reply_token)
            return JSONResponse(
                {"ok": False, "error": "request expired", "state": "expired"},
                status_code=504,
            )
        try:
            result = await dispatch_text(text_in, reply_channel="text",
                                         reply_token=reply_token, source="n8n")
            if not result.get("accepted"):
                chat_request.cancel()
                return JSONResponse(
                    {"ok": False, "error": str(result.get("message") or result),
                     "state": "cancelled"},
                    status_code=503,
                )
            waiter = (server_state.get("reply_waiters") or {}).get(reply_token)
            if waiter is None:
                return JSONResponse(
                    {"ok": False, "error": "reply box lost", "state": "cancelled"},
                    status_code=502,
                )
            try:
                deadline = time.monotonic() + timeout
                parts = []
                while True:
                    remaining = max(0.05, deadline - time.monotonic())
                    parts.append(await asyncio.wait_for(
                        waiter.get(), timeout=remaining))
                    if not waiter.empty() or time.monotonic() >= deadline:
                        break
                    try:
                        grace = await asyncio.wait_for(waiter.get(), timeout=0.4)
                    except asyncio.TimeoutError:
                        break
                    parts.append(grace)
            except asyncio.TimeoutError:
                chat_request.expire()
                return JSONResponse(
                    {"ok": False, "error": "no reply within timeout",
                     "state": "expired"},
                    status_code=504,
                )
            reply = " ".join(p for p in parts if p).strip()
            # The turn ran. Deliberate silence is a legitimate outcome - FRIDAY
            # reads a notice, judges it not worth answering, and says nothing.
            # Reporting ok=False for that would tell the workflow it failed.
            # `replied` is what distinguishes "answered" from "no reply needed".
            chat_request.complete()
            return JSONResponse({
                "ok": True,
                "replied": bool(reply),
                "kind": kind,
                "state": chat_request.state,
                "reply": reply,
                "payload": payload,
            })
        except asyncio.CancelledError:
            chat_request.cancel()
            _cancel_http_owner(reply_token)
            raise
        finally:
            turn_lock.release()
            _close_reply_box(server_state, reply_token)
            try:
                chat_requests.sweep()
            except Exception:
                pass

    # -- ROUND ZB-PE: how the workflow learns the outcome ------------------
    #
    # `/n8n` returns a correlation id the moment it parks a question. These two
    # routes let n8n POLL for the answer, which is the reliable path: it does
    # not depend on FRIDAY reaching n8n later, and it cannot be lost to a
    # restart. They are guarded exactly like `/n8n` - ALWAYS, not only when
    # forwarded - because a parked question contains email content.
    def _require_n8n_token(request):
        presented = str(request.headers.get("x-friday-token") or "").strip()
        if not presented:
            authorization = str(request.headers.get("authorization") or "")
            if authorization.lower().startswith("bearer "):
                presented = authorization[7:].strip()
        secret = str(_n8n_adapter().config.get("secret") or "")
        if not secret:
            return JSONResponse(
                {"ok": False, "error":
                 "n8n inbound is not configured: set `secret` in "
                 "~/.friday/n8n.json (or FRIDAY_N8N_SECRET)."},
                status_code=403)
        if not presented or not _hmac.compare_digest(
                presented.encode("utf-8"), secret.encode("utf-8")):
            return JSONResponse({"ok": False, "error": "unauthorized"},
                                status_code=401)
        return None

    @interpreter.server.app.get("/n8n/pending")
    async def n8n_pending_list(request: Request):
        """Every question still waiting on Sir, oldest first."""
        denied = _require_n8n_token(request)
        if denied is not None:
            return denied
        pending_store.sweep()
        return JSONResponse({
            "ok": True,
            "requests": [item.public_view() for item in pending_store.open_requests()],
        })

    @interpreter.server.app.get("/n8n/pending/{correlation_id}")
    async def n8n_pending_one(request: Request, correlation_id: str):
        """One parked question, by correlation id.

        `state` is what the workflow should branch on: `open` means still
        waiting; `answered` carries the owner's decision in `answer`;
        `ignored` and `expired` mean give up on it.
        """
        denied = _require_n8n_token(request)
        if denied is not None:
            return denied
        pending_store.sweep()
        item = pending_store.get(str(correlation_id or "").strip())
        if item is None:
            return JSONResponse(
                {"ok": False, "error": "unknown correlation id"},
                status_code=404)
        return JSONResponse({"ok": True, **item.public_view()})

    # -- ROUND ZB-SC: the screen companion, reached over HTTP ---------------
    #
    # WHY ROUTES AND NOT A FUNCTION. The OI kernel runs in a separate
    # `ipykernel_launcher` process, so `import source.server.screen_companion`
    # inside authored code yields a DIFFERENT module instance with its own
    # empty singleton - `get_companion()` there builds an UNWIRED companion
    # whose speaker is missing, and every tip goes nowhere. This is the same
    # defect class already solved for `/screen/history` (see the L3-1 comment),
    # so it gets the same cure: the wired instance lives in THIS process, and
    # the sandbox reaches it over localhost.
    @interpreter.server.app.post("/companion/start")
    async def companion_start(request: Request):
        """Start watching. Returns the status either way - never raises."""
        denied = _guard(request)
        if denied is not None:
            return denied
        try:
            raw = (await request.body()).decode("utf-8", errors="replace")
            options = json.loads(raw) if raw.strip().startswith("{") else {}
            if not isinstance(options, dict):
                options = {}
        except Exception:
            options = {}
        companion = getattr(interpreter, "screen_companion", None)
        if companion is None:
            return JSONResponse(
                {"ok": False, "running": False,
                 "last_error": "screen companion is not wired on this server"},
                status_code=503)
        try:
            started = companion.start(
                str(options.get("brief") or ""),
                duration=options.get("duration"),
                interval=options.get("interval"),
            )
        except Exception as error:
            return JSONResponse(
                {"ok": False, "running": False,
                 "last_error": type(error).__name__},
                status_code=500)
        return JSONResponse({"ok": bool(started), **companion.status()})

    @interpreter.server.app.post("/companion/stop")
    async def companion_stop(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        companion = getattr(interpreter, "screen_companion", None)
        if companion is None:
            return JSONResponse(
                {"ok": False, "running": False, "last_error": "not wired"},
                status_code=503)
        try:
            reason = ""
            raw = (await request.body()).decode("utf-8", errors="replace")
            if raw.strip().startswith("{"):
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    reason = str(parsed.get("reason") or "")
            companion.stop(reason)
        except Exception as error:
            return JSONResponse(
                {"ok": False, "running": False,
                 "last_error": type(error).__name__},
                status_code=500)
        return JSONResponse({"ok": True, **companion.status()})

    @interpreter.server.app.get("/companion/status")
    async def companion_status(request: Request):
        """Read-only, but still guarded: it reports what is on screen."""
        denied = _guard(request)
        if denied is not None:
            return denied
        companion = getattr(interpreter, "screen_companion", None)
        if companion is None:
            return JSONResponse(
                {"ok": False, "running": False, "last_error": "not wired"},
                status_code=503)
        try:
            return JSONResponse({"ok": True, **companion.status()})
        except Exception as error:
            return JSONResponse(
                {"ok": False, "running": False, "last_error": type(error).__name__},
                status_code=500)

    # -- ROUND ZB-OV: display-only on-screen annotation overlay -------------
    #
    # WHY ROUTES, AGAIN. The OI kernel is a separate process (see the /companion
    # comment above), so `import source.server.screen_overlay` from authored
    # code would build a SECOND module instance with its own empty window
    # singleton. The wired instance lives here; the sandbox reaches it over
    # localhost exactly like the screen companion does.
    #
    # DISPLAY ONLY. The window is WS_EX_TRANSPARENT (mouse passes through) and
    # WS_EX_NOACTIVATE (it can never take focus), and it carries
    # WDA_EXCLUDEFROMCAPTURE so FRIDAY cannot screenshot her own drawing. These
    # routes therefore annotate and nothing else - there is deliberately no
    # "click at this rect" sibling.
    @interpreter.server.app.post("/overlay/annotate")
    async def overlay_annotate(request: Request):
        """Draw one annotation. Refusals are DATA (200 + ok=False), not faults."""
        denied = _guard(request)
        if denied is not None:
            return denied
        try:
            from . import screen_overlay
        except Exception as error:
            return JSONResponse(
                {"ok": False, "error": type(error).__name__}, status_code=503)
        # The window is created on THIS loop thread, and ShowWindow from any
        # other thread is a measured no-op - so give the module a way back onto
        # the loop for the TTL watcher's hide. Installed once, harmlessly.
        if getattr(screen_overlay, "_window_executor", None) is None:
            _overlay_loop = server_state.get("event_loop")
            if _overlay_loop is not None:
                try:
                    screen_overlay.set_window_executor(
                        _overlay_loop.call_soon_threadsafe)
                except Exception:
                    pass
        try:
            raw = (await request.body()).decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip().startswith("{") else {}
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        try:
            result = screen_overlay.annotate(
                str(payload.get("query") or ""),
                shape=str(payload.get("shape") or "rect"),
                rect=payload.get("rect"),
                points=payload.get("points"),
                label=str(payload.get("label") or ""),
                color=payload.get("color"),
                fill=bool(payload.get("fill")),
                ttl=payload.get("ttl"),
                confirmation=payload.get("confirmation"),
                known_names=tuple(payload.get("known_names") or ()),
                command_text=str(payload.get("command_text") or ""),
                actor=str(payload.get("actor") or "brain"),
            )
        except Exception as error:
            return JSONResponse(
                {"ok": False, "error": type(error).__name__}, status_code=500)
        if not isinstance(result, dict):
            result = {"ok": False, "reason": "overlay returned no result"}
        return JSONResponse(result)

    @interpreter.server.app.post("/overlay/clear")
    async def overlay_clear(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        try:
            from . import screen_overlay

            _loop = server_state.get("event_loop")
            if (_loop is not None
                    and getattr(screen_overlay, "_window_executor", None) is None):
                try:
                    screen_overlay.set_window_executor(
                        _loop.call_soon_threadsafe)
                except Exception:
                    pass
            reason = ""
            raw = (await request.body()).decode("utf-8", errors="replace")
            if raw.strip().startswith("{"):
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    reason = str(parsed.get("reason") or "")
            return JSONResponse({"ok": True,
                                 **screen_overlay.clear(reason or "manual")})
        except Exception as error:
            return JSONResponse(
                {"ok": False, "error": type(error).__name__}, status_code=500)

    @interpreter.server.app.get("/overlay/status")
    async def overlay_status(request: Request):
        """Read-only, but guarded: it reports what is on screen."""
        denied = _guard(request)
        if denied is not None:
            return denied
        try:
            from . import screen_overlay

            return JSONResponse({"ok": True, **screen_overlay.status()})
        except Exception as error:
            return JSONResponse(
                {"ok": False, "error": type(error).__name__}, status_code=500)

    # -- ROUND ZB-VP: read the voice pipeline trace -------------------------
    #
    # The log only said WHEN, never HOW LONG. This reports per-turn stage
    # timings and diagnoses the six failure modes Sir named, so a slow first
    # turn, a silent TTS or a mid-sentence cutoff is answerable from evidence
    # rather than from memory. Guarded: it reveals turn timing.
    @interpreter.server.app.get("/voice/trace")
    async def voice_trace_report(request: Request):
        denied = _guard(request)
        if denied is not None:
            return denied
        recorder = _voice_trace_get()
        try:
            return JSONResponse({
                "ok": True,
                "turns": recorder.turn_count(),
                "summary": recorder.summary(),
                # Wall-clock stamps per event, in friday.log's own format, so a
                # turn can be correlated with specific log lines.
                "timeline": recorder.timeline(),
                # ROUND ZB-CH: queued->synthesized lag, backlog depth and
                # supersession counters. Never claims audible completion.
                "speech": speech_snapshot(server_state),
                "voice_input": audio_link.voice_snapshot(),
                # ROUND ZB-LAT: what the prompt render actually cost. It has no
                # trace stage of its own (it runs inside OI's respond loop,
                # between `brain select` and `llm_request_start`), so this is
                # the only place the ~7s every turn spends there is visible.
                "prompt_render": _prompt_render_stats(),
                "diagnosis": recorder.diagnose(),
            })
        except Exception as error:
            return JSONResponse(
                {"ok": False, "error": type(error).__name__},
                status_code=500)

    def terminal_input_thread():
        while True:
            try:
                input() 
                if server_state["is_paused"]:
                    if not _set_voice_input(False):
                        continue
                    print("\r🟢 Voice mode activated! Listening...")

                    # ROUND ZB-REC: the line above is the truthful signal; only
                    # withhold the SPOKEN copy when no engine can produce audio.
                    resume_owned_voice()

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
            or _pending_has(server_state)
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

    def _stop_active_turn(reason):
        """Actually cancel the running turn. ROUND W (2026-10-04).

        The old code called `getattr(interpreter, "stop", None)`. That attribute
        DOES NOT EXIST - verified: `hasattr(OpenInterpreter, "stop")` and
        `hasattr(AsyncInterpreter, "stop")` are both False, and neither class
        exposes any stop/kill/terminate/abort/cancel member. So `callable()` was
        always False and the watchdog's cancellation branch never ran. It only
        published a status string and pushed a synthetic end frame; a wedged turn
        kept running. MEMORY.md previously described it as calling `stop()`,
        which was wrong.

        The mechanism that genuinely works is `interpreter.stop_event`, which is
        what the HARD_TOOL_CAP and runaway-script guards already use. OI polls it
        per chunk (async_core.py:130-132, 163-168) and, for a Jupyter kernel,
        calls `km.interrupt_kernel()` (jupyter_language.py:144-150), so the
        runaway script is interrupted at ~50 ms rather than merely labelled.
        """
        stopped = False
        try:
            event = getattr(interpreter, "stop_event", None)
            if event is not None and hasattr(event, "set"):
                event.set()
                stopped = True
        except Exception as error:
            _flog.warning("watchdog stop_event.set() raised: %s", error)
        # Deliberately NOT `interpreter.stop()`: it does not exist, and calling
        # it is the reason this watchdog was inert for its whole life.
        _flog.warning(
            "watchdog cancelled active turn via stop_event (%s) interrupted=%s",
            reason, stopped,
        )
        return stopped

    def _turn_budget_check():
        """Enforce the TURN wall-clock budget and the SILENT-HANG budget.

        ROUND W (2026-10-04). This runs on every watchdog tick, so it is
        completely independent of whether the turn printed anything - which was
        the whole point. The previous check lived inside the console-output
        handler, so a turn that hung silently was never examined at all, while a
        healthy turn that merely took its time was killed.

        Two distinct conditions, deliberately kept apart:

        * TURN BUDGET (TURN_WALL_CLOCK_LIMIT, default 600s) - the overall ceiling
          for one turn. Exceeding it ends the turn and tells the truth.
        * SILENT HANG (SILENT_HANG_LIMIT, default 300s) - the turn has produced
          NO interpreter output at all for this long, while being ACTIVE.

        The silent-hang limit is deliberately SHORTER than the turn budget and
        is checked FIRST, because its job is to end a wedged turn PROMPTLY
        instead of making Sir wait out the whole budget for silence. A limit
        LONGER than the turn budget would be unreachable dead code: the turn
        budget would always fire first. That was the first version of this code
        and it had the bug.

        Once ANY interpreter output has been seen, silent hang can never fire
        (`turn_progress_observed`), so a slow-but-working turn is governed by the
        turn budget alone and keeps its full allowance.

        Returns a reason string when it acts, else None.
        """
        if not response_is_active():
            return None
        # Enforced ONCE per turn. The watchdog ticks every second, but the
        # interpreter thread can take a moment to unwind after stop_event; without
        # this latch every one of those seconds would re-run
        # _finish_budgeted_turn and re-speak "I stopped that one", producing the
        # exact duplicate-spam the flood guard exists to prevent.
        if server_state.get("turn_budget_enforced"):
            return None
        started = server_state.get("turn_started_at")
        if started is None:
            # Turn origin unknown (e.g. a turn begun before this process state
            # existed). Fall back to the progress clock rather than guessing.
            started = server_state.get("last_progress")
        if started is None:
            return None
        # Measured from the TURN START, never from before it. Using
        # `last_progress` directly here would count pre-turn idle time as
        # silence, so a brand-new turn that had not yet produced anything would
        # instantly look like a hang - the same class of false positive the
        # watchdog already suffered once (measured: "no progress for 1275s"
        # one second into a healthy turn).
        progress = server_state.get("last_progress") or started
        silence_origin = max(progress, started)
        silent_for = time.monotonic() - silence_origin
        elapsed = time.monotonic() - started

        # SILENT HANG first: it is the stricter limit and would otherwise be
        # unreachable behind the turn budget.
        if (
            not server_state.get("turn_progress_observed")
            and silent_for > SILENT_HANG_LIMIT
        ):
            return "no output at all for %.0fs (silent hang)" % silent_for
        if elapsed > TURN_WALL_CLOCK_LIMIT:
            return "turn budget of %ds exceeded (%.0fs elapsed)" % (
                int(TURN_WALL_CLOCK_LIMIT), elapsed)
        return None

    async def _finish_budgeted_turn(reason, owner, attempt, *, stalled=False):
        if (_ownership(server_state) is not owner or server_state.get("turn_attempt") is not attempt
                or server_state.get("turn_budget_enforced") or not response_is_active()):
            return
        server_state["turn_budget_enforced"] = True
        _stop_active_turn(reason)
        # Keep the slot until the interrupted worker finishes. External owners
        # get the budget failure through their own reply channel.
        server_state["tool_cap_terminated"] = True
        _identity.close(server_state, owner)
        server_state["barge_in"] = False
        turn_guard["flood_stopped"] = True
        notify_speech_stop(server_state, "watchdog", queue=interpreter.output_queue)
        if stalled and _tts_recover_latch(getattr(interpreter, "tts", None)):
            _flog.warning("watchdog repaired a stale TTS playback latch")
        _stamp_progress(server_state)
        publish_status(STATUS_IDLE, "Turn ended on the time budget")
        chat_append("tool", "TURN ENDED ON THE TIME BUDGET (%s)" % reason, status="complete")
        await _terminal_notice(owner, "I stopped that one - %s." % reason, outcome="interrupted")

    def watchdog_loop():
        while True:
            time.sleep(1.0)
            try:
                if os.environ.get("FRIDAY_WATCHDOG") == "0":
                    return
                owner = _ownership(server_state)
                attempt = server_state.get("turn_attempt")
                loop = server_state.get("event_loop")
                if owner is LOCAL_OWNERSHIP or loop is None or not loop.is_running():
                    continue
                budget_reason = _turn_budget_check()
                if budget_reason:
                    asyncio.run_coroutine_threadsafe(
                        _finish_budgeted_turn(budget_reason, owner, attempt), loop)
                    continue
                if _watchdog_skip_idle():
                    continue
                stalled = time.monotonic() - server_state.get("last_progress", time.monotonic())
                if stalled >= _watchdog_seconds:
                    asyncio.run_coroutine_threadsafe(
                        _finish_budgeted_turn("stalled %.0fs" % stalled, owner, attempt, stalled=True), loop)
            except Exception as error:
                _flog.warning("watchdog error: %s", error)
                time.sleep(5.0)

    _watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True, name="friday-watchdog")
    _watchdog_thread.start()

    # Keep the local brain RESIDENT. Measured on this machine: a cold qwen3:8b
    # reply takes 49.3s versus 2.2s warm, because Ollama unloads models after
    # 5 minutes by default and FRIDAY idles between turns. That hidden load cost
    # is what made captions come back empty, vision look unreliable, and turns
    # report "every model is unavailable" - the brain was never slow, it was
    # being reloaded inside every timeout window. Warming at boot would slow
    # startup, so this runs in the background instead.
    try:
        from . import brain_keepalive

        brain_keepalive.start(
            model=getattr(getattr(interpreter, "llm", None), "model", None)
        )
        _flog.info("brain keep-alive: %s", brain_keepalive.status())
    except Exception as keepalive_error:
        _flog.info("brain keep-alive unavailable: %s", keepalive_error)

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
        try:
            _activity.stop()
        except Exception:
            pass
