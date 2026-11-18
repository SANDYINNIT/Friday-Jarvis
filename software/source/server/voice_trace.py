"""Voice pipeline trace: per-turn timings and the six failures that matter.

WHY THIS EXISTS
---------------
Sir reported three symptoms - a slow first reply, voice that never plays, and
voice that starts then cuts off - and the log could not answer any of them,
because `trace()` only wrote debug lines. There was no record of WHEN each stage
ran, so "was it the provider, the TTS, or the queue?" could not be answered from
evidence.

This records the events `server.py` already emits, each with a monotonic
timestamp, into a small ring buffer - then derives stage durations and diagnoses
the six failure modes. Pure bookkeeping: recording never raises, never blocks,
and never grows without bound. It changes nothing about playback.

THE SIX CHECKS
--------------
first turn        - the first turn after boot is slower than later ones
repeated turns    - latency across turns, so a trend is visible
no audio          - tts_start fired but tts_first_audio never did, or a response
                    completed with nothing queued for speech
delay             - a stage exceeded its budget (first token, first audio, total)
mid-sentence cut  - audio started, then the turn stopped short of a normal finish
fallback recovery - which TTS stage actually served the turn (cloud/local/none)
"""

import collections
import datetime
import threading
import time

__all__ = ["VoiceTrace", "get_trace", "BUDGETS", "STAGES"]

MAX_EVENTS = 500
MAX_TURNS = 40

STAGES = (
    ("capture", "stt_start", "stt_stop"),
    ("transcription", "stt_stop", "stt_final"),
    ("admission", "stt_final", "turn_admitted"),
    ("route", "stt_stop", "llm_request_start"),
    ("first_token", "llm_request_start", "llm_first_chunk"),
    ("generate", "llm_first_chunk", "response_complete"),
    # NOTE: there is deliberately no "queue -> speech" stage. RealtimeTTS starts
    # DURING streaming, so `tts_start` precedes `response_complete`, not the
    # other way round - a stage written that way can never satisfy the
    # start<=end guard and would silently vanish from every summary.
    ("tts_first_audio", "tts_start", "tts_first_audio"),
)

BUDGETS = {
    "first_token": 8.0,
    "tts_first_audio": 3.0,
    "duration": 45.0,
    "duration_first_turn": 90.0,
}

END_EVENTS = ("response_complete", "final_response_end")
TURN_STARTERS = ("wake_accepted", "stt_start")

# Stages whose END event must be the FIRST occurrence, not the last.
#
# `_derive_stages` pairs first(start) with last(end). For a latency stage that
# is wrong: a turn with a tool call emits llm_request_start/llm_first_chunk
# more than once, so "first_token" measured first(request) -> LAST(first_chunk)
# and spanned the entire tool execution. Observed 2026-11-01 22:04 turn: it
# reported first_token=23.609s (budget 8s) when the real time-to-first-token
# was 3.0s - the other 20.6s was the tool running and a second LLM call.
# Latency stages must not silently absorb work that happened in between.
STAGE_END_USE_FIRST = frozenset({"route", "first_token", "tts_first_audio"})


def _kind(event):
    """The event's name with any detail suffix removed.

    Several call sites pass a PRE-FORMATED string instead of a name plus
    keyword detail:

        trace("wake_rejected reason=final_text_missing_wake")   server.py:3653
        trace("stt_final text='...'")

    Comparing that whole string against the bare name `"wake_rejected"` is
    always False, so the rejection branch never ran and the turn never closed.
    Observed live 2026-11-01 23:27: a single turn swallowed FIVE rejected
    utterances and grew to 217s while `turn_count` stayed at 1. Tests passed
    because they called `record("wake_rejected", reason=...)` - the API the
    production code does not actually use.
    """
    return str(event or "").split(" ", 1)[0]


def _has(events, name):
    """Membership for a list of raw events, tolerant of detail suffixes."""
    return any(_kind(item) == name for item in events)


class VoiceTrace:
    """Ring-buffered per-turn record of the voice pipeline."""

    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._events = collections.deque(maxlen=MAX_EVENTS)
        self._turns = collections.deque(maxlen=MAX_TURNS)
        self._turn = None
        self._turn_count = 0

    def record(self, event, detail=None, **extra):
        """Append one event. Never raises: a diagnostic must not break speech.

        `detail` may be a mapping (how server.py calls it) or keyword args
        (how the tests call it); both are merged.
        """
        try:
            if extra:
                detail = {**(detail or {}), **extra}
            event = str(event or "").strip()
            if not event:
                return
            # Structural checks use the NAME only: call sites pass pre-formated
            # strings like "wake_rejected reason=...", which an exact compare
            # against the bare name can never match.
            kind = _kind(event)
            now_mono = self._clock()
            now_wall = self._wall()
            with self._lock:
                if kind == "wake_accepted" and self._turn is not None:
                    self._close_turn(now_mono)
                if self._turn is None and kind in TURN_STARTERS:
                    self._start_turn(now_mono, now_wall)
                # Bookkeeping is isolated from the boundary decisions below.
                # record() must never raise, so its blanket except previously
                # turned ANY failure inside here into "the turn never closes":
                # observed live, one turn swallowed FIVE wake_rejections and
                # grew to 217s because the close branch never ran.
                try:
                    self._events.append((now_wall, now_mono, event, detail))
                    if self._turn is not None:
                        self._turn["events"].append(event)
                        # Wall-clock twin of every event, so a /voice/trace turn
                        # can be matched line-for-line against friday.log.
                        self._turn.setdefault("event_wall", []).append(now_wall)
                except Exception:
                    pass

                if kind in END_EVENTS:
                    # Do not close while audio is still playing. RealtimeTTS
                    # emits tts_end AFTER response_complete (observed
                    # 2026-11-01 22:04:42 vs 22:04:48), so closing here pushed
                    # BOTH response_complete and tts_end outside the turn -
                    # which silently dropped the `generate` stage and made
                    # "did playback finish?" unanswerable for the turn the
                    # evidence belonged to.
                    if not self._audio_outstanding():
                        self._close_turn(now_mono)
                elif kind == "tts_end" and self._turn is not None:
                    # Playback finished: this, not the text, is the real end
                    # of a spoken turn.
                    self._close_turn(now_mono)
                elif kind == "wake_rejected":
                    # A rejected utterance is not a turn. `stt_start` opened it,
                    # nothing will ever complete it, and keeping it reports the
                    # wall-clock span of every ignored phrase as a stage
                    # duration - observed live: one phantom turn of 2128s built
                    # from 35 minutes of rejected wake words, which produced
                    # three false "delay" findings. The rejection itself is
                    # still kept in self._events for evidence.
                    #
                    # NOTE: matched by KIND, because production passes
                    # `trace("wake_rejected reason=...")` - an exact compare
                    # against "wake_rejected" silently never fired, which is
                    # how one live turn swallowed five rejections and reached
                    # 217s while turn_count stayed at 1.
                    self._close_turn(now_mono, discard=True)
        except Exception:
            pass

    def _audio_outstanding(self):
        """True when this turn began speaking but has not reported the end."""
        events = self._turn.get("events") if self._turn else None
        if not events:
            return False
        return _has(events, "tts_start") and not _has(events, "tts_end")

    def _start_turn(self, mono, wall_stamp):
        self._turn_count += 1
        self._turn = {
            "index": self._turn_count,
            "started_mono": mono,
            "started_wall": wall_stamp,
            "events": [],
            "event_wall": [],
            "stages": {},
            "detail": {},
        }

    def _close_turn(self, mono, discard=False):
        if self._turn is None:
            return
        turn = self._turn
        # Clear the LIVE pointer FIRST. It used to be the last statement, so if
        # deriving stages raised, the blanket except in record() swallowed it
        # and the turn stayed live - accumulating every later event instead of
        # ending. A closed turn must be closed even when its maths fails.
        self._turn = None
        try:
            turn["duration"] = round(mono - turn["started_mono"], 3)
            self._derive_stages(turn)
        except Exception:
            turn.setdefault("stages", {})
        if not discard:
            try:
                self._turns.append(turn)
            except Exception:
                pass

    def _derive_stages(self, turn):
        """Event list -> stage durations, using only this turn's own window."""
        window = [
            (mono, event, detail) for _wall, mono, event, detail in list(self._events)
            if mono >= turn["started_mono"]
        ]
        first, last, detail = {}, {}, {}
        for mono, event, evt_detail in window:
            first.setdefault(event, mono)
            last[event] = mono
            if evt_detail:
                detail.setdefault(event, evt_detail)
        for name, start_event, end_event in STAGES:
            # Keyed by EVENT name, not by stage name - `first`/`last` are built
            # from the event stream above.
            if start_event not in first or end_event not in last:
                continue
            # Latency stages stop at the FIRST end event so a tool call in the
            # middle cannot stretch them; duration stages keep the LAST.
            end = first[end_event] if name in STAGE_END_USE_FIRST else last[end_event]
            if end >= first[start_event]:
                turn["stages"][name] = round(end - first[start_event], 3)
        if "stt_start" in first:
            end = last.get("response_complete") or last.get("stt_stop")
            if end is not None and end >= first["stt_start"]:
                turn["stages"]["duration"] = round(end - first["stt_start"], 3)
        turn["detail"] = detail

    def recent(self, n=5):
        with self._lock:
            return [dict(t) for t in list(self._turns)[-n:]]

    def last(self):
        turns = self.recent(1)
        return turns[0] if turns else None

    def turn_count(self):
        with self._lock:
            return self._turn_count

    def diagnose(self):
        """Findings for the last turn, covering the six checks Sir named.

        Includes the LIVE (still open) turn: a turn that is mid-flight, or that
        died without ever completing, is exactly the case worth diagnosing - and
        it would otherwise be invisible because it never reaches `_close_turn`.
        """
        with self._lock:
            turns = [dict(t) for t in list(self._turns)]
            events = [e for _w, _m, e, _d in self._events]
            if self._turn is not None:
                live = dict(self._turn)
                # Shallow copy would share the stage dict with the live turn;
                # copy it so deriving stages cannot corrupt the real record.
                live["stages"] = dict(live.get("stages") or {})
                live["detail"] = dict(live.get("detail") or {})
                live["duration"] = round(self._clock() - live["started_mono"], 3)
                self._derive_stages(live)
                turns.append(live)
        if not turns:
            return {"turns": 0, "findings": ["no completed turns yet"]}
        last_turn = turns[-1]
        stages = last_turn.get("stages") or {}
        findings = []

        first_turn = turns[0]
        first_d = first_turn.get("duration")
        last_d = last_turn.get("duration")
        findings.append({
            "check": "first_turn",
            "turns_total": len(turns),
            "first_turn_seconds": first_d,
            "latest_turn_seconds": last_d,
            "slower_than_latest": bool(first_d and last_d and first_d > last_d * 1.5),
        })

        # Per-TURN event membership. This used to read the process-wide event
        # ring, so a tts_end from any earlier turn masked a genuine cutoff in
        # this one - the check could only ever agree with history, never with
        # the turn it was describing. Compared by KIND, because some call sites
        # pass pre-formated strings ("wake_rejected reason=...").
        events = list(last_turn.get("events") or [])
        has_tts_start = _has(events, "tts_start")
        has_first_audio = _has(events, "tts_first_audio")
        # RealtimeTTS fires on_audio_stream_stop -> trace("tts_end") when the
        # utterance actually ends. Its absence after audio began is the direct
        # evidence of a mid-sentence cutoff, and needs no inference.
        has_tts_end = _has(events, "tts_end")
        has_response = _has(events, "response_complete")
        if has_tts_start and not has_first_audio:
            findings.append({"check": "no_audio",
                             "detail": "tts_start seen, tts_first_audio never fired"})
        if has_response and not has_tts_start:
            findings.append({"check": "no_audio",
                             "detail": "response completed, nothing queued for speech"})

        for stage, budget in BUDGETS.items():
            value = stages.get(stage) or last_d
            if value is not None and value > budget:
                findings.append({"check": "delay", "stage": stage,
                                 "seconds": value, "budget": budget})

        if has_first_audio:
            if _has(events, "interrupted"):
                findings.append({"check": "mid_sentence_cutoff",
                                 "detail": "playback was interrupted"})
            elif not has_tts_end:
                findings.append({"check": "mid_sentence_cutoff",
                                 "detail": "audio started but playback never "
                                           "reported completion (no tts_end)"})
            elif not (has_response or _has(events, "final_response_end")):
                findings.append({"check": "mid_sentence_cutoff",
                                 "detail": "audio played to the end but the "
                                           "turn never finished"})

        detail = last_turn.get("detail") or {}
        stage = detail.get("tts_stage")
        if not stage:
            # The detail is nested under the event that carried it.
            for key in ("tts_start", "tts_first_audio", "response_complete"):
                nested = detail.get(key)
                if isinstance(nested, dict) and nested.get("tts_stage"):
                    stage = nested["tts_stage"]
                    break
        stage = stage or "unknown"
        findings.append({"check": "fallback", "tts_stage": stage})
        if stage in ("none", ""):
            findings.append({"check": "fallback_failed",
                             "detail": "no TTS stage served this turn - silence"})

        return {"turns": len(turns), "last_turn": last_turn, "findings": findings}

    def summary(self, n=5):
        """Human-readable timing table for the last few turns."""
        turns = self.recent(n)
        if not turns:
            return "No voice turns recorded yet."
        lines = ["last %d turn(s):" % len(turns)]
        for turn in turns:
            stages = turn.get("stages") or {}
            parts = ["#%s %.2fs" % (turn.get("index"), turn.get("duration") or 0)]
            for name, _s, _e in STAGES:
                if name in stages:
                    parts.append("%s=%.2f" % (name, stages[name]))
            lines.append("  " + "  ".join(parts))
        return "\n".join(lines)

    def timeline(self, turn=None):
        """Per-event wall-clock stamps in friday.log's own date-time format.

        Without this, "inspect that turn" is ambiguous: a session has several
        turns and /voice/trace only exposed the turn's start. The stamps are
        formatted exactly as friday.log writes them, so each entry can be
        matched line-for-line (e.g. `2026-11-01 22:04:10 DEBUG
        llm_request_start`) instead of guessing.
        """
        if turn is None:
            # `last()` only sees CLOSED turns, but the most interesting turn is
            # often still in flight (or never emitted an END_EVENT at all).
            with self._lock:
                turn = dict(self._turn) if self._turn is not None else None
                if turn is not None:
                    # Copy the mutable sub-dicts; see diagnose() for why.
                    turn["events"] = list(turn.get("events") or [])
                    turn["event_wall"] = list(turn.get("event_wall") or [])
        if not turn:
            return []
        events = turn.get("events") or []
        walls = turn.get("event_wall") or []
        out = []
        for index, name in enumerate(events):
            stamp = ""
            if index < len(walls):
                try:
                    stamp = datetime.datetime.fromtimestamp(
                        float(walls[index])).strftime("%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError, OSError, OverflowError):
                    stamp = ""
            out.append({"at": stamp, "event": name})
        return out


_TRACE = None


def get_trace(reload=False):
    """The process-wide recorder."""
    global _TRACE
    if _TRACE is None or reload:
        _TRACE = VoiceTrace()
    return _TRACE
