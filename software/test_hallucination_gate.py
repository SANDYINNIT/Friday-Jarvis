"""WP1: whisper hallucination / no-speech gate tests."""
from source.server.speech_filters import reject_reason


def test_empty_is_rejected():
    assert reject_reason("   ") == "empty"


def test_caption_boilerplate_is_rejected():
    assert reject_reason("Thanks for watching!") is not None
    assert reject_reason("Please like and subscribe for more.") is not None
    assert reject_reason("Subtitles by the Amara.org community") is not None
    assert reject_reason("[music]") is not None


def test_repetitive_no_speech_is_rejected():
    assert reject_reason("ha ha ha ha ha ha ha ha ha ha") is not None
    assert reject_reason("the the the the the the the the the") is not None


def test_normal_commands_pass():
    assert reject_reason("friday what time is it") is None
    assert reject_reason("open notepad") is None
    assert reject_reason("remember my birthday is tomorrow") is None