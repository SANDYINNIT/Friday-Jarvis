"""Regression tests for avoiding invalid punctuation-only TTS requests."""

from source.server.server import _is_tts_text_speakable


assert not _is_tts_text_speakable("?", "")
assert not _is_tts_text_speakable("?", "A complete sentence")
assert _is_tts_text_speakable("Hello", "")
print("PASS: punctuation-only TTS fragments are guarded")
