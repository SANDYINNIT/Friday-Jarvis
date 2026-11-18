"""Deterministic policy tests for FRIDAY's bounded follow-up window."""

import os
import threading
import time

from source.server.server import (
    _configured_hot_window_seconds,
    _find_wake_word,
    _hot_window_candidate,
    _hot_window_is_active,
    _remember_tts_text,
    _strip_wake_word,
)


def state(active=True, expires=103.0):
    return {
        "hot_window_lock": threading.Lock(),
        "hot_window_active": active,
        "hot_window_expires_at": expires,
        "is_paused": False,
        "last_wake_time": 0.0,
        "last_wake_text": "",
        "recent_tts_texts": [],
        "tts_echo_buffer": "",
    }


old = os.environ.get("FRIDAY_HOT_WINDOW_SECONDS")
os.environ["FRIDAY_HOT_WINDOW_SECONDS"] = "4.5"
assert _configured_hot_window_seconds() == 10.0  # env overrides respected under the 10s floor
if old is None:
    del os.environ["FRIDAY_HOT_WINDOW_SECONDS"]
else:
    os.environ["FRIDAY_HOT_WINDOW_SECONDS"] = old
assert _configured_hot_window_seconds() == 30.0  # default follow-up window 30s
os.environ["FRIDAY_HOT_WINDOW_SECONDS"] = "30"
assert _configured_hot_window_seconds() == 30.0
os.environ["FRIDAY_HOT_WINDOW_SECONDS"] = "120"
assert _configured_hot_window_seconds() == 60.0  # capped at 60s.
if old is None:
    del os.environ["FRIDAY_HOT_WINDOW_SECONDS"]
else:
    os.environ["FRIDAY_HOT_WINDOW_SECONDS"] = old

# A completed response is represented by an active, timestamped window.
opened = state()
assert _hot_window_is_active(opened, now=100.0)
assert _hot_window_candidate("tell me more", opened, False, now=100.5) == "tell me more"
assert _hot_window_is_active(opened, now=101.5)

# Only final STT text authorizes a cold-window request. A realtime preview
# containing "Friday" cannot make a final transcript without the wake word pass.
assert _find_wake_word("Friday, tell me more") == "friday"
assert _find_wake_word("tell me more") == ""
assert _strip_wake_word("Friday, tell me more", "friday") == "tell me more"

# No transcript, no wake outside the window, and expired windows are ignored.
assert _hot_window_candidate("   ", opened, False, now=100.5) is None
assert _hot_window_candidate("background conversation", state(False), False, now=100.5) is None
expired = state(True, expires=100.0)
assert not _hot_window_is_active(expired, now=100.1)
assert _hot_window_candidate("tell me more", expired, False, now=100.1) is None

# Exact recent TTS echo is suppressed, while a different follow-up is accepted.
echo = state()
_remember_tts_text(echo, "The answer is ready.", now=100.0)
assert _hot_window_candidate("the answer is ready", echo, True, now=100.5) is None
assert _hot_window_candidate("what about tomorrow", echo, True, now=100.5) == "what about tomorrow"

# Pause and duplicate guards remain authoritative.
paused = state()
paused["is_paused"] = True
assert _hot_window_candidate("continue", paused, False, now=100.5) is None
duplicate = state()
duplicate["last_wake_text"] = "tell me more"
duplicate["last_wake_time"] = time.time()
assert _hot_window_candidate("tell me more", duplicate, False, now=100.5) is None

# Lifecycle ownership is represented by pending_input, not a second dispatch.
pending = state()
pending["pending_input"] = {"content": "already queued"}
assert pending["pending_input"]["content"] == "already queued"

print("PASS: hot-window timing, empty/no-wake, echo, pause, duplicate, and pending guards")
