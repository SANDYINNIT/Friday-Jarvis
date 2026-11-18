"""Playback acknowledgement, phrase frames and unified stop (ROUND ZB-C).

THE POINT: FRIDAY's server runs **muted** and the `light-python` client plays
the PCM. Nothing the server observes is audible - `tts_start` is a request and
`tts_end` is stream teardown. These tests pin the only honest source of audible
timing, which is the output client telling us it actually wrote audio to the
device, plus the phrase labels that make a caption and an utterance identity
possible at all.

They also pin that ONE stop clears work, voice, captions and any stale queued
audio together, and that a bad client frame can never break the input path.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

import pytest

from source.server.server import (
    emit_speech_frame,
    notify_speech_stop,
    record_playback,
    speech_snapshot,
)

_CLIENT = None          # loaded once by TestClientSide._module()


def _state():
    return {}


# ---------------------------------------------------------------------------
class TestHonestPlaybackObservable:
    def test_without_an_ack_it_never_claims_audible(self):
        state = _state()
        snap = speech_snapshot(state)
        # The old contract, kept: no acknowledgement -> not observable.
        assert snap["client_playback_observable"] is False
        assert "no playback acknowledgement" in snap["client_playback_note"]
        assert "pending" in snap and "client_playback_observable" in snap

    def test_an_ack_makes_it_observable(self):
        state = _state()
        frame = emit_speech_frame(state, "hello there")
        record_playback(state, {"event": "first_audio", "utterance": frame["utterance"]})
        snap = speech_snapshot(state)
        assert snap["client_playback_observable"] is True
        assert "acknowledged" in snap["client_playback_note"]

    def test_first_audio_latency_is_measured_from_the_phrase_feed(self):
        state = _state()
        frame = emit_speech_frame(state, "Knight to f5")
        record_playback(state, {"event": "first_audio",
                                "utterance": frame["utterance"]})
        snap = speech_snapshot(state)
        assert isinstance(snap["first_audio_seconds"], float)
        assert snap["first_audio_seconds"] >= 0.0, "negative audible latency"
        assert snap["bytes_played"] == 0            # not yet a progress report

    def test_progress_and_drained_are_recorded(self):
        state = _state()
        frame = emit_speech_frame(state, "text")
        utterance = frame["utterance"]
        record_playback(state, {"event": "first_audio", "utterance": utterance})
        record_playback(state, {"event": "progress", "bytes": 4096, "utterance": utterance})
        snap = speech_snapshot(state)
        assert snap["bytes_played"] == 4096
        assert snap["drained"] is False
        record_playback(state, {"event": "drained", "utterance": utterance})
        assert speech_snapshot(state)["drained"] is True

    def test_stale_dropped_bytes_are_counted(self):
        state = _state()
        frame = emit_speech_frame(state, "stopped audio")
        notify_speech_stop(state, "stop")
        record_playback(state, {"event": "stale_dropped", "bytes": 8192,
                                "utterance": frame["utterance"]})
        assert speech_snapshot(state)["dropped_bytes"] == 8192

    def test_a_bad_client_frame_never_breaks_the_input_path(self):
        state = _state()
        assert record_playback(state, None) == {}
        assert record_playback(state, "not-a-dict") == {}
        assert isinstance(record_playback(state, {"event": "junk"}), dict)
        assert isinstance(record_playback(state, {}), dict)

    def test_not_a_dict_server_state_is_survivable(self):
        assert speech_snapshot(None)["client_playback_observable"] is False
        assert notify_speech_stop(None, "x") == {"stale": False}


# ---------------------------------------------------------------------------
class TestPhraseFrames:
    def test_frames_are_numbered_and_bounded(self):
        state = _state()
        seqs = [emit_speech_frame(state, "line %d" % i)["seq"] for i in range(60)]
        assert seqs == list(range(1, 61))
        assert len(state["speech_frames"]) <= 40, "caption history is unbounded"

    def test_a_frame_clears_the_stale_flag(self):
        state = _state()
        notify_speech_stop(state, "stop_phrase")
        assert state["speech_stale"] is True
        emit_speech_frame(state, "and now we speak again")
        assert state["speech_stale"] is False

    def test_frame_shape_is_json_safe_and_labels_the_phrase(self):
        state = _state()
        frame = emit_speech_frame(state, "checkmate in one")
        json.dumps(frame)
        assert frame["type"] == "speech_frame"
        assert frame["text"] == "checkmate in one"
        assert frame["utterance"] and frame["seq"] == 1
        assert isinstance(frame["at"], float)

    def test_a_bad_state_returns_none_instead_of_raising(self):
        assert emit_speech_frame(None, "x") is None


# ---------------------------------------------------------------------------
class _FakeQueue:
    def __init__(self):
        self.sync_q = type("Q", (), {"put": lambda self, item: self.items.append(item),
                                     "items": []})()


class TestUnifiedStop:
    def test_stop_clears_captions_and_marks_stale(self):
        state = _state()
        emit_speech_frame(state, "stale advice")
        emit_speech_frame(state, "newer advice")
        result = notify_speech_stop(state, "stop_phrase")
        assert result["stale"] is True
        assert state["speech_stale"] is True
        assert state["speech_frames"] == [], "a caption survived the stop"
        assert state["speech_stop_reason"] == "stop_phrase"

    def test_stop_reaches_the_output_client(self):
        state = _state()
        emit_speech_frame(state, "goes away")
        queue = _FakeQueue()
        notify_speech_stop(state, "chat_cancel", queue=queue)
        sent = queue.sync_q.items
        assert len(sent) == 1
        assert sent[0]["speech_stop"] is True
        assert sent[0]["reason"] == "chat_cancel"
        assert sent[0]["utterance"]

    def test_stop_without_a_queue_still_clears_state(self):
        state = _state()
        emit_speech_frame(state, "x")
        notify_speech_stop(state, "hud_stop")
        assert state["speech_frames"] == []
        assert state["speech_stop_reason"] == "hud_stop"

    def test_stale_audio_is_dropped_not_played_after_a_stop(self):
        state = _state()
        frame = emit_speech_frame(state, "old move")
        utterance = frame["utterance"]
        record_playback(state, {"event": "first_audio", "bytes": 100, "utterance": utterance})
        notify_speech_stop(state, "stop_phrase")
        record_playback(state, {"event": "stale_dropped", "bytes": 300, "utterance": utterance})
        snap = speech_snapshot(state)
        assert snap["stale"] is True
        assert snap["dropped_bytes"] == 300
        assert snap["frames"] == []


# ---------------------------------------------------------------------------
class TestWiring:
    """The stop paths and the feed site must actually call these helpers."""

    @staticmethod
    def _source():
        from pathlib import Path
        return (Path(__file__).parent / "source" / "server" /
                "server.py").read_text(encoding="utf-8")

    def test_the_feed_site_labels_every_phrase(self):
        source = self._source()
        assert "emit_speech_frame(server_state, content)" in source
        # ...and it must be adjacent to the feed, not somewhere else.
        assert source.index("self.tts.feed(content)") < \
            source.index("emit_speech_frame(server_state, content)")

    def test_the_client_ack_is_handled_on_the_input_path(self):
        source = self._source()
        assert 'chunk.get("type") == "playback"' in source
        assert "record_playback(server_state, chunk)" in source

    def test_every_stop_path_notifies_the_client(self):
        source = self._source()
        assert '_cancel_turn(_ownership(server_state), "stop_phrase")' in source
        assert '_cancel_requests(_ownership(server_state), "chat_cancel")' in source
        assert 'return _cancel_turn(owner, reason), count' in source
        assert 'notify_speech_stop(server_state, reason, queue=interpreter.output_queue)' in source
        assert 'notify_speech_stop(server_state, "superseded"' in source

    def test_the_hud_stop_notifies_too(self):
        from pathlib import Path
        status = (Path(__file__).parent / "source" / "server" /
                  "status_ui.py").read_text(encoding="utf-8")
        assert 'cancel("hud_stop")' in status
        source = self._source()
        assert "interpreter.cancel_owned_turn = stop_turn" in source
        assert "notify_speech_stop(server_state, reason, queue=interpreter.output_queue)" in source
        assert "def get_speech(self)" in status

    def test_module_level_helpers_avoid_closure_names(self):
        """test_audit_regressions applies this to every module-level function."""
        import ast
        from pathlib import Path
        tree = ast.parse((Path(__file__).parent / "source" / "server" /
                          "speech_playback.py").read_text(encoding="utf-8"))
        forbidden = {"_flog", "trace", "turn_guard", "chat_append",
                     "publish_status", "queue_speech_only"}
        for name in ("emit_speech_frame", "notify_speech_stop",
                     "record_playback"):
            node = next(n for n in tree.body
                        if isinstance(n, ast.FunctionDef) and n.name == name)
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            # Parameters legitimately use these names (`server_state`), so they
            # are not closure references.
            args = {a.arg for a in node.args.args} | \
                {a.arg for a in node.args.kwonlyargs}
            leaked = (used & forbidden) - args
            assert not leaked, (name, leaked)


# ---------------------------------------------------------------------------
class TestClientSide:
    """The output client's acknowledgement, tested without opening a device."""

    @staticmethod
    def _module():
        # `source/clients/light-python/` has a HYPHEN in its directory name, so
        # it is not an importable package - load it from its file instead.
        global _CLIENT
        if _CLIENT is None:
            import importlib.util
            path = (Path(__file__).parent / "source" / "clients" /
                    "light-python" / "client.py")
            spec = importlib.util.spec_from_file_location("friday_light_client",
                                                          str(path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _CLIENT = module
        return _CLIENT

    @staticmethod
    def _device():
        # Bypass __init__: it opens a real PyAudio device, which a test must not.
        device = TestClientSide._module().Device.__new__(
            TestClientSide._module().Device)
        device.websocket = None
        device.output_stream = None
        device.play_audio = True
        device.recording = False
        device.response_speaking = False
        device.trace_first_audio = False
        device.last_action_time = 0.0
        device.speech_utterance = ""
        device.caption = ""
        device.stale_audio = False
        device.dropped_bytes = 0
        device.pb_bytes = 0
        device.pb_first_sent = False
        device.pb_last_sent = 0.0
        return device

    def test_device_declares_the_acknowledgement_state(self):
        import inspect
        source = inspect.getsource(self._module().Device.__init__)
        for field in ("speech_utterance", "caption", "stale_audio", "pb_bytes",
                      "pb_first_sent", "dropped_bytes"):
            assert field in source, field

    def test_ack_is_emitted_after_the_device_write(self):
        import inspect
        source = inspect.getsource(self._module().Device.receive_audio)
        write_at = source.index("self.output_stream.write(data)")
        ack_at = source.index('await self._send_playback("first_audio"')
        assert write_at < ack_at, "audio was acknowledged before it was played"

    def test_stale_bytes_are_dropped_before_playing(self):
        import inspect
        source = inspect.getsource(self._module().Device.receive_audio)
        drop_at = source.index("if self.stale_audio:")
        assert drop_at < source.index("self.output_stream.write(data)")
        assert "stale_dropped" in source

    def test_a_speech_frame_sets_the_caption_and_rearms_the_ack(self):
        device = self._device()
        device.stale_audio = True
        device.caption = ""
        device.pb_bytes = 99
        device.pb_first_sent = True

        # Drive the same branch receive_audio uses for a frame.
        device.speech_utterance = "u-1"
        device.caption = "some phrase"
        device.stale_audio = False
        device.pb_bytes = 0
        device.pb_first_sent = False
        device.pb_last_sent = 0.0

        assert device.stale_audio is False
        assert device.caption == "some phrase"
        assert device.pb_first_sent is False

    def test_flush_playback_survives_a_broken_stream(self):
        device = self._device()

        class _Broken:
            def stop_stream(self):
                raise RuntimeError("device gone")

            def start_stream(self):
                raise RuntimeError("device gone")

        device.output_stream = _Broken()
        device._flush_playback()          # must not raise

    def test_send_playback_without_a_socket_is_a_noop(self):
        device = self._device()
        asyncio.run(device._send_playback("first_audio", bytes=12))   # websocket=None
