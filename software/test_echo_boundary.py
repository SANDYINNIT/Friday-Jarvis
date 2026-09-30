"""Deterministic tests for the bounded TTS echo boundary."""

from source.server.server import _is_recent_tts_echo, _remember_tts_text


def _state():
    return {"recent_tts_texts": [], "tts_echo_buffer": ""}


state = _state()
_remember_tts_text(state, "Friday, set a timer for five minutes.", now=100.0)
assert _is_recent_tts_echo(
    "friday set a timer for five minutes", state, tts_is_playing=True, now=100.5
)

state = _state()
_remember_tts_text(state, "Friday, set a timer for five minutes.", now=100.0)
assert not _is_recent_tts_echo(
    "friday open notepad", state, tts_is_playing=True, now=100.5
)

state = _state()
_remember_tts_text(state, "Friday, set a timer for five minutes.", now=100.0)
assert not _is_recent_tts_echo(
    "friday set a timer for five minutes", state, tts_is_playing=False, now=103.0
)

print("PASS: exact recent echo suppressed; different and stale speech accepted")
