"""Turn/update-aware admission policy for text FRIDAY is about to speak.

WHY THIS EXISTS (2026-11-02 chess session, measured)
-----------------------------------------------------
`new_output` feeds every assistant chunk into RealtimeTTS *before* checking
whether the stream is idle, and nothing ever trimmed what had already been fed.
One audio stream ran **21:36:51 -> 21:38:07 (76 s)** while the screen companion
kept posting new guidance every ~18-21 s, so text appeared on the direct channel
immediately while the voice was still working through an older backlog - up to
~17 s, roughly one full update behind the screen. Wake -> first audio was 25 s.

The fix is to bound and supersede the *speech queue*, never to delay the text:
the UI keeps streaming as fast as the model produces it.

WHAT THIS MODULE DECIDES
------------------------
Nothing here touches RealtimeTTS, the network or the UI. `admit()` answers one
question: *should this text be fed, and must the already-fed backlog be cleared
first?* The caller performs the actual `stop()`/`feed()`.

Two rules:

1. **Supersede** - screen/chess guidance is perishable. When newer guidance
   arrives, older guidance that has not been spoken yet is discarded rather than
   played after it. Conversation is never superseded this way.
2. **Bound** - total unspoken speech must stay inside `HARD_MAX_SECONDS`, or
   the voice drifts arbitrarily far behind the screen. Guidance is dropped
   first; conversation is only dropped as a last resort, loudly.

`SOFT_MAX_SECONDS` only bounds *guidance*, so ordinary conversation is never
chopped up just to catch up.

HONESTY ABOUT MEASUREMENT
--------------------------
`tts_end` marks RealtimeTTS tearing its stream down, **not** the client
finishing playback - FRIDAY's server runs `muted=True` and the client plays the
PCM itself. So this module measures `queued_at -> synthesized_at` (text handed
over -> first audio chunk observed) and explicitly reports
`client_playback_observable = False`. It never claims to know what Sir heard.
"""

from __future__ import annotations

import time

# ~15 characters of spoken English per second - an ESTIMATE used only for
# backlog budgeting, never for anything user-visible.
CHARS_PER_SECOND = 15.0

# Unspoken guidance older than this is dropped when newer guidance arrives.
SOFT_MAX_SECONDS = 10.0
# Absolute ceiling on unspoken speech, whatever its kind. Exceeding this means
# the voice has drifted too far behind to be useful.
HARD_MAX_SECONDS = 20.0

GUIDANCE_MARKERS = ("[vision_modal]",)


def classify(text, source=None):
    """`guidance` (perishable screen/chess update) or `conversation`."""
    if source == "guidance":
        return "guidance"
    body = str(text or "").lstrip()
    for marker in GUIDANCE_MARKERS:
        if body.startswith(marker):
            return "guidance"
    return "conversation"


def _speech_seconds(text):
    return len(str(text or "")) / CHARS_PER_SECOND


class SpeechQueue:
    """FIFO of text handed to the stream but not yet heard producing audio."""

    def __init__(self, soft_max=SOFT_MAX_SECONDS, hard_max=HARD_MAX_SECONDS):
        self.soft_max = float(soft_max)
        self.hard_max = float(hard_max)
        self._pending = []          # [{seq, kind, text, queued_at, synthesized_at}]
        self._seq = 0
        self._last_chunk_at = 0.0
        self._awaiting_audio = False
        self.superseded = 0
        self.preempted = 0
        self.bound_drops = 0
        self.last_preempt_reason = ""

    # -- admission -------------------------------------------------------
    def admit(self, text, *, source=None, now=None):
        """Decide whether to feed `text` and whether the backlog must be cleared.

        Returns {"speak": bool, "preempt": bool, "dropped": [...], "reason": str}.
        `preempt=True` means the caller must `stream.stop()` BEFORE feeding,
        because superseded/stale text is already inside RealtimeTTS.
        """
        now = time.monotonic() if now is None else now
        kind = classify(text, source)
        dropped = []
        preempt = False
        reason = ""
        clear_all = False

        # Rule 1: newer guidance supersedes older unspoken guidance.
        if kind == "guidance":
            stale = [u for u in self._pending if u["kind"] == "guidance"]
            if stale:
                if any(u["kind"] == "conversation" for u in self._pending):
                    # A conversational reply is also in the stream. RealtimeTTS
                    # only offers stop()-all, so clearing to drop the guidance
                    # would cut the reply mid-sentence - which we are told to
                    # preserve. Leave the backlog honest and let the newer
                    # guidance queue behind it instead of pretending to drop it.
                    reason = "deferred_behind_conversation"
                else:
                    for entry in stale:
                        dropped.append({
                            "seq": entry["seq"],
                            "reason": "superseded_by_newer_guidance",
                            "age_seconds": round(now - entry["queued_at"], 2),
                            "text": entry["text"][:120],
                        })
                    self.superseded += len(stale)
                    # Only guidance is in the stream, so clearing it is safe.
                    preempt = True
                    reason = "superseded_by_newer_guidance"
                    self._pending = [u for u in self._pending
                                     if u["kind"] != "guidance"]

        # Rule 2: absolute backlog ceiling. This is the one case where we clear
        # everything regardless of kind - a voice 20s+ behind the screen is
        # worse than a cut sentence, and it is logged loudly.
        if not preempt and self._pending_seconds(now) > self.hard_max:
            victim = next((u for u in self._pending if u["kind"] == "guidance"),
                          self._pending[0] if self._pending else None)
            if victim is not None:
                dropped.append({
                    "seq": victim["seq"],
                    "reason": "backlog_exceeded_%.0fs" % self.hard_max,
                    "age_seconds": round(now - victim["queued_at"], 2),
                    "text": victim["text"][:120],
                })
                self.bound_drops += 1
                self._pending = [u for u in self._pending if u is not victim]
                preempt = True
                clear_all = True
                reason = "backlog_exceeded"

        entry = {
            "seq": self._next_seq(),
            "kind": kind,
            "text": str(text or "")[:200],
            "queued_at": now,
            "synthesized_at": None,
        }
        if clear_all:
            # The stream is about to be cleared wholesale.
            self._pending = []
            self._awaiting_audio = False
        self._pending.append(entry)
        if preempt:
            self.preempted += 1
            self.last_preempt_reason = reason
        return {"speak": True, "preempt": preempt, "dropped": dropped,
                "reason": reason, "seq": entry["seq"], "kind": kind}

    # -- observation -----------------------------------------------------
    def on_chunk(self, now=None):
        """Mark the head of the queue as having produced audio.

        Called from `on_tts_chunk`. Audio arrives as many chunks per utterance,
        so the head is marked only once per feed, on the first chunk after an
        audible gap (or the very first chunk) - that is the honest moment the
        queued text stopped waiting.
        """
        now = time.monotonic() if now is None else now
        gap = (now - self._last_chunk_at) if self._last_chunk_at else None
        self._last_chunk_at = now
        if not self._pending:
            return None
        head = self._pending[0]
        if head["synthesized_at"] is not None:
            return None
        if gap is None or gap >= 0.5 or not self._awaiting_audio:
            head["synthesized_at"] = now
            self._awaiting_audio = True
            return round(now - head["queued_at"], 3)
        return None

    def mark_fed(self):
        """A feed happened: the next audio chunk belongs to it."""
        self._awaiting_audio = False

    def reset(self, reason=""):
        """Drop everything queued - called on stop, barge-in and new turns."""
        dropped = len(self._pending)
        if dropped:
            self.preempted += 1
            self.last_preempt_reason = reason or "reset"
        self._pending = []
        self._awaiting_audio = False
        return dropped

    # -- reporting -------------------------------------------------------
    def _pending_seconds(self, now):
        return sum(_speech_seconds(u["text"]) for u in self._pending
                   if u["synthesized_at"] is None)

    def snapshot(self, now=None):
        """Metrics for /voice/trace. Never claims audible completion."""
        now = time.monotonic() if now is None else now
        pending = [u for u in self._pending if u["synthesized_at"] is None]
        oldest = min((u["queued_at"] for u in pending), default=None)
        # `is not None`, NOT truthiness: a queued_at of exactly 0.0 is a legal
        # timestamp in tests and must not be mistaken for "no queue".
        return {
            "pending": len(pending),
            "pending_speech_seconds": round(self._pending_seconds(now), 2),
            "oldest_wait_seconds": (round(now - oldest, 2)
                                    if oldest is not None else 0.0),
            "superseded": self.superseded,
            "preempted": self.preempted,
            "bound_drops": self.bound_drops,
            "last_preempt_reason": self.last_preempt_reason,
            "hard_max_seconds": self.hard_max,
            "soft_max_seconds": self.soft_max,
            # The server runs muted; the client plays the PCM. tts_end marks
            # stream teardown, NOT the end of what Sir heard.
            "client_playback_observable": False,
            "client_playback_note": (
                "tts_end is server-side stream teardown; audible start/finish "
                "is known only to the client and is not measured here"),
            "recent": [
                {"seq": u["seq"], "kind": u["kind"], "text": u["text"][:80],
                 "waited": round(now - u["queued_at"], 2),
                 "lag_to_audio": (round(u["synthesized_at"] - u["queued_at"], 2)
                                  if u["synthesized_at"] else None)}
                for u in list(self._pending)[-5:]
            ],
        }

    def _next_seq(self):
        self._seq += 1
        return self._seq


def get_queue(server_state):
    """Queue is held on server_state - never a module global (test isolation)."""
    if not isinstance(server_state, dict):
        return SpeechQueue()
    queue = server_state.get("speech_queue")
    if queue is None:
        queue = SpeechQueue()
        server_state["speech_queue"] = queue
    return queue
