"""Screen companion: a bounded, always-cheap watcher that speaks useful advice.

WHAT THIS IS FOR
----------------
Sir asked for the thing commercial assistants do: look at the screen while he is
playing, and call out what matters - "enemy on your left", "that door is open",
"you have 3 seconds before the storm closes". Not a description of the screen
every second, and not a chatbot narrating pixels: ONE short, useful line when
there is something worth saying, and silence when there is not.

HOW IT ACTUALLY WORKS
---------------------
A background thread on a fixed cadence:

    capture -> one vision call -> decide -> maybe speak

Each cycle costs exactly one vision call (~2.2s against Modal, measured
2026-10-18), so a 8-second cadence is roughly 25% duty on the vision budget and
costs nothing on the voice path - the companion never touches the turn lock and
only ever *queues* a line to speak.

It is deliberately NOT continuous vision. Continuous would be several requests a
second, which is neither affordable nor useful: the model cannot act on a 4 Hz
description anyway. An 8-second cadence is the honest trade for "live enough to
play with".

WHAT IT IS NOT
--------------
It is not a classifier and not a phrase trigger. The brain decides whether to
start it, based on what Sir asked for. It never performs an action - it only
speaks, so the consent surface is the same as any other reply.

Everything is injectable (capture, describe, speak, clocks) so the whole
decision path can be tested without a screen, a GPU or a provider.
"""

import hashlib
import os
import threading
import time

__all__ = ["ScreenCompanion", "get_companion", "WATCH_PROMPT"]

# One vision call, one question, one answer. "NOTHING" is the model's way of
# saying today is quiet - it costs nothing to send and it is what stops a
# companion from narrating a static screen forever.
WATCH_PROMPT = (
    "You are watching this person's screen to help them. Reply with ONE short "
    "sentence, under 25 words, naming the single most useful thing to tell them "
    "right now - a threat, an opportunity, or one concrete tip. Be specific "
    "about position when you can ('enemy top-left', 'door open on the right'). "
    "If nothing is worth saying, reply with exactly: NOTHING"
)

DEFAULT_DURATION = 600.0
DEFAULT_INTERVAL = 8.0
DEFAULT_MIN_GAP = 15.0
MAX_DURATION = 3600.0
MAX_INTERVAL = 60.0
MIN_INTERVAL = 2.0
# A run stops after this many consecutive failed cycles - a broken capture
# should not spin forever burning provider quota.
MAX_CONSECUTIVE_FAILURES = 3
# Observations this similar to the last spoken one are not worth repeating.
DEDUP_PREFIX = 48


def _normalise(text):
    """Lower-cased, whitespace-collapsed core of a line - for comparison only."""
    return " ".join(str(text or "").lower().split())


def _is_nothing(text):
    """True when the model declined to say anything."""
    normalised = _normalise(text).strip(" .!?,")
    return normalised in ("nothing", "no", "none", "")


class ScreenCompanion:
    """Watches the screen and speaks one useful line at a time."""

    def __init__(self, capture=None, describe=None, speak=None,
                 clock=time.monotonic, environ=None):
        self.environ = os.environ if environ is None else environ
        self._clock = clock
        self._capture = capture
        self._describe = describe
        self._speak = speak
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._active = False
        self._brief = ""
        self._interval = DEFAULT_INTERVAL
        self._min_gap = DEFAULT_MIN_GAP
        self._deadline = 0.0
        self._next_speak_at = 0.0
        self._last_spoken_prefix = ""
        self._last_spoken_at = 0.0
        self._last_observation = ""
        self._last_error = ""
        self._cycles = 0
        self._vision_calls = 0
        self._spoken = 0
        self._suppressed = 0
        self._started_at = 0.0
        self._stop_reason = ""

    # -- configuration ---------------------------------------------------
    def _clamp(self, value, low, high, default):
        try:
            raw = float(value)
        except (TypeError, ValueError):
            return default
        if raw <= 0:
            return default
        return max(low, min(raw, high))

    def _configured_interval(self):
        return self._clamp(self.environ.get("FRIDAY_WATCH_INTERVAL"),
                           MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL)

    def _configured_duration(self):
        return self._clamp(self.environ.get("FRIDAY_WATCH_DURATION"),
                           30.0, MAX_DURATION, DEFAULT_DURATION)

    # -- lifecycle --------------------------------------------------------
    def start(self, brief="", duration=None, interval=None):
        """Begin watching. Returns False when it could not start.

        Idempotent: starting twice keeps the first brief and extends the window
        only if the caller asked for it.
        """
        with self._lock:
            if self._active:
                return False
            if self._capture is None or self._describe is None or self._speak is None:
                self._last_error = "companion not wired (capture/describe/speak)"
                return False
            if self._stop.is_set():
                # A previous run's thread may still be unwinding; a fresh Event
                # is required or the new thread would exit immediately.
                self._stop = threading.Event()
            self._brief = str(brief or "").strip()[:400]
            self._interval = self._clamp(interval, MIN_INTERVAL, MAX_INTERVAL,
                                         self._configured_interval())
            seconds = self._clamp(duration, 30.0, MAX_DURATION,
                                  self._configured_duration())
            now = self._clock()
            self._active = True
            self._started_at = now
            self._deadline = now + seconds
            self._next_speak_at = now          # may speak on the first cycle
            self._last_spoken_prefix = ""
            self._last_spoken_at = 0.0
            self._last_error = ""
            self._cycles = 0
            self._vision_calls = 0
            self._spoken = 0
            self._suppressed = 0
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                           name="friday-screen-companion")
            self._thread.start()
        return True

    def stop(self, reason=""):
        """Stop watching. Returns True if it was running."""
        with self._lock:
            was_active = self._active
            self._active = False
            self._stop.set()
            thread = self._thread
            self._thread = None
            self._stop_reason = str(reason or "")[:120]
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        return was_active

    def status(self):
        """Honest state. `running` is what the brain should trust."""
        with self._lock:
            remaining = max(0.0, self._deadline - self._clock()) if self._active else 0.0
            return {
                "running": bool(self._active),
                "brief": self._brief,
                "remaining_seconds": round(remaining, 1),
                "interval_seconds": self._interval,
                "cycles": self._cycles,
                "vision_calls": self._vision_calls,
                "spoken": self._spoken,
                "suppressed_similar": self._suppressed,
                "last_observation": self._last_observation[:300],
                "last_error": self._last_error,
                "stopped_reason": getattr(self, "_stop_reason", ""),
            }

    # -- the watch loop ---------------------------------------------------
    def _loop(self):
        failures = 0
        while not self._stop.is_set():
            with self._lock:
                if not self._active or self._clock() >= self._deadline:
                    self._active = False
                    self._stop_reason = getattr(self, "_stop_reason", "") or "window elapsed"
                    break
                wait = max(0.0, self._next_speak_at - self._clock())
            # Sleep until the next cycle, but wake instantly on stop so a
            # "stop watching" is immediate rather than one interval late.
            if self._stop.wait(timeout=max(0.0, wait)):
                break
            try:
                line, ok = self._cycle()
            except Exception as error:                 # never kill the watcher
                line, ok = None, False
                self._last_error = type(error).__name__ + ": " + str(error)[:200]
            if not ok:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    with self._lock:
                        self._active = False
                        self._stop_reason = "stopped after %d failed cycles" % failures
                    break
                continue
            failures = 0
            # Speak what the cycle decided on. Discarding this - which the first
            # version did - makes the whole companion silently pointless: every
            # observation is thrown away and Sir hears nothing, ever.
            if line:
                self.speak_now(line)

    def _cycle(self):
        """One capture + one vision call. Returns (text_to_speak_or_None, ok)."""
        brief = self._brief
        question = WATCH_PROMPT
        if brief:
            question = ("%s\n\nThe person asked for help with: %s"
                        % (WATCH_PROMPT, brief))

        jpeg = self._capture()
        if not jpeg:
            self._last_error = "capture returned nothing"
            return None, False
        with self._lock:
            self._vision_calls += 1
        self._last_observation = str(self._describe(jpeg, question) or "").strip()
        self._cycles += 1

        observation = self._last_observation
        if _is_nothing(observation):
            with self._lock:
                self._next_speak_at = self._clock() + self._interval
            return None, True
        if not observation:
            self._last_error = "vision returned nothing"
            return None, False

        with self._lock:
            now = self._clock()
            if now < self._next_speak_at:
                self._suppressed += 1
                return None, True
            # Never repeat ourselves: a static screen produces the same line
            # every cycle, and a companion that says it twice is worse than
            # silent.
            prefix = _normalise(observation)[:DEDUP_PREFIX]
            if prefix and prefix == self._last_spoken_prefix:
                self._suppressed += 1
                self._next_speak_at = now + self._interval
                return None, True
            self._last_spoken_prefix = prefix
            self._last_spoken_at = now
            self._next_speak_at = now + self._min_gap
            self._spoken += 1
        return observation, True

    def speak_now(self, text):
        """Queue one line to be spoken. Never raises."""
        if not str(text or "").strip():
            return False
        try:
            self._speak(str(text).strip())
            return True
        except Exception as error:
            self._last_error = "speak failed: " + type(error).__name__
            return False


_COMPANION = None


def get_companion(speak=None, reload=False):
    """The process-wide companion, wired to the real screen and vision.

    `speak` is supplied by the server (`queue_direct_response`) rather than
    imported here, so this module never depends on server internals and the
    whole decision path stays testable with a recording fake.
    """
    global _COMPANION
    if _COMPANION is not None and not reload:
        if speak is not None and _COMPANION._speak is None:
            _COMPANION._speak = speak
        return _COMPANION

    def _capture():
        from .screen_understanding import capture_screen_jpeg
        return capture_screen_jpeg()

    def _describe(jpeg, question):
        from .screen_understanding import describe_image_bytes
        return describe_image_bytes(jpeg, question)

    _COMPANION = ScreenCompanion(capture=_capture, describe=_describe,
                                 speak=speak)
    return _COMPANION
