"""Client duration ceilings never authorize partial voice commands."""
import asyncio
from unittest.mock import Mock

import pytest

from test_intent_judge_removed import _VoiceHarness


@pytest.mark.parametrize("transcript", ["Friday delete my files", "Friday hello", ""])
def test_incomplete_capture_is_released_without_stt_or_dispatch(transcript):
    harness = _VoiceHarness(transcript)
    harness.self_obj.audio_chunks = [b"synthetic partial audio"]
    text = Mock(return_value=transcript)
    harness.self_obj.stt.text = text
    asyncio.run(harness.new_input(harness.self_obj, {
        "end": True, "capture_incomplete": True,
    }))
    text.assert_not_called()
    assert harness.self_obj.audio_chunks == []
    assert harness.queue == [{"ignored": True, "end": True, "capture_incomplete": True}]
    for forbidden in ("transcribe_wav", "old_input", "_pending_push",
                      "_begin_single_turn", "handle_special_command"):
        assert forbidden not in harness.names()
    assert harness.first_args("trace") == ("stt_stop",)
    assert any("capture_incomplete" in str(args) for name, args, _ in harness.calls if name == "trace")
    assert any("repeat with a pause" in str(args) for name, args, _ in harness.calls if name == "publish_status")


@pytest.mark.parametrize("marker", [False, None])
def test_ordinary_complete_capture_keeps_existing_dispatch(marker):
    harness = _VoiceHarness("Friday hello")
    text = Mock(return_value="Friday hello")
    harness.self_obj.stt.text = text
    asyncio.run(harness.new_input(harness.self_obj, {"end": True, "capture_incomplete": marker}))
    text.assert_called_once()
    assert "handle_special_command" in harness.names()
    assert not harness.ignored()
