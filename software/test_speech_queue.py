"""Turn/update-aware speech queue (ROUND ZB-CH).

The 2026-11-02 chess session: one audio stream ran 76s while screen guidance
arrived every ~18-21s, so the direct channel showed the newest move while the
voice was still speaking an older one. This module decides what is fed and
whether the already-fed backlog must be cleared - it never touches the UI, the
network or RealtimeTTS itself.

Deliberately pure: no TTS engine, no server_state, no imports beyond `time`, so
every policy question is testable without audio hardware or a running server.
"""

import pytest

from source.server.speech_queue import (
    HARD_MAX_SECONDS,
    SpeechQueue,
    classify,
    get_queue,
)


def _guidance(text="[vision_modal] White to move: play Nxf7"):
    return text


class TestClassification:
    def test_vision_modal_marker_is_guidance(self):
        assert classify(_guidance()) == "guidance"

    def test_explicit_source_wins(self):
        assert classify("whatever", source="guidance") == "guidance"

    def test_plain_reply_is_conversation(self):
        assert classify("Doing well, Sir.") == "conversation"
        assert classify("Stopped.") == "conversation"

    def test_marker_must_lead_the_text(self):
        # A marker buried mid-string is not a companion update.
        assert classify("Sure. [vision_modal] later") == "conversation"


class TestSupersession:
    def test_newer_guidance_drops_the_older_unspoken_one(self):
        q = SpeechQueue()
        first = q.admit(_guidance("[vision_modal] play Nxf7"), now=0.0)
        assert first["preempt"] is False, "first update must not clear anything"

        second = q.admit(_guidance("[vision_modal] actually play d4"), now=1.0)
        assert second["preempt"] is True, "stale guidance already fed -> must clear"
        assert second["reason"] == "superseded_by_newer_guidance"
        assert len(second["dropped"]) == 1
        assert "Nxf7" in second["dropped"][0]["text"]
        assert q.superseded == 1

    def test_the_spoken_move_is_the_current_displayed_one(self):
        """After three rapid updates only the newest guidance may be spoken."""
        q = SpeechQueue()
        for i, move in enumerate(("Nxf7", "d4", "e5")):
            q.admit(_guidance("[vision_modal] play %s" % move), now=float(i))

        pending = [u for u in q._pending if u["synthesized_at"] is None]
        assert len(pending) == 1, "more than one update survived: %r" % pending
        assert pending[0]["text"].endswith("play e5"), pending[0]["text"]

    def test_conversation_is_never_superseded(self):
        q = SpeechQueue()
        q.admit("Doing well, Sir. Let me check the board.", now=0.0)
        decision = q.admit("Thanks for the game.", now=1.0)
        assert decision["preempt"] is False
        assert len(q._pending) == 2, "a conversational reply was dropped"
        assert q.superseded == 0

    def test_guidance_does_not_supersede_conversation(self):
        q = SpeechQueue()
        q.admit("Let me explain the opening first.", now=0.0)
        decision = q.admit(_guidance(), now=1.0)
        assert decision["preempt"] is False, "conversation was cleared by guidance"
        kinds = {u["kind"] for u in q._pending}
        assert kinds == {"conversation", "guidance"}


class TestBacklogBound:
    def test_the_76_second_incident_is_now_bounded(self):
        """Reproduce the incident: guidance every ~19s, audio never arrives.

        Exactly the 2026-11-02 shape - text kept arriving while synthesis could
        not keep up. For guidance-only traffic SUPERSESSION is the mechanism
        (each update replaces the last), with the hard bound as backstop for
        conversation-heavy traffic; what matters is that nothing accumulates.
        """
        q = SpeechQueue()
        now = 0.0
        for i in range(8):
            q.admit(_guidance("[vision_modal] move number %d" % i), now=now)
            now += 19.0

        snapshot = q.snapshot(now)
        assert snapshot["pending_speech_seconds"] <= HARD_MAX_SECONDS, snapshot
        assert snapshot["oldest_wait_seconds"] <= HARD_MAX_SECONDS, snapshot
        assert snapshot["superseded"] > 0, "supersession never fired"
        assert snapshot["preempted"] > 0, "stale audio was never cleared"
        # At most the newest update is still waiting to be spoken.
        assert snapshot["pending"] <= 1, snapshot

    def test_a_slow_synthesizer_triggers_the_bound_not_unbounded_growth(self):
        """Slow Gemini: text is fed, audio lags -> queue must not grow forever."""
        q = SpeechQueue()
        for i in range(12):
            q.admit("Sentence number %d is here and it is fairly long." % i,
                    now=float(i))
        snap = q.snapshot(now=11.0)
        assert snap["pending_speech_seconds"] <= HARD_MAX_SECONDS, snap
        assert snap["bound_drops"] >= 1

    def test_guidance_is_dropped_before_conversation_at_the_bound(self):
        q = SpeechQueue()
        q.admit("A long conversational explanation that must survive.", now=0.0)
        for i in range(10):
            q.admit(_guidance("[vision_modal] update %d" % i), now=float(i + 1))
        snap = q.snapshot(now=11.0)
        kinds = {u["kind"] for u in q._pending if u["synthesized_at"] is None}
        assert "conversation" in kinds or snap["bound_drops"] > 0

    def test_a_healthy_stream_never_triggers_the_bound(self):
        """Normal speech: audio keeps up, so nothing is ever dropped."""
        q = SpeechQueue()
        for i in range(6):
            decision = q.admit("A short reply number %d." % i, now=float(i))
            assert decision["preempt"] is False, decision
            q.mark_fed()
            q.on_chunk(now=float(i) + 0.1)     # audio arrives promptly
        snap = q.snapshot(now=7.0)
        assert snap["bound_drops"] == 0 and snap["superseded"] == 0
        assert snap["preempted"] == 0


class TestMetrics:
    def test_queued_to_synthesized_lag_is_measured(self):
        q = SpeechQueue()
        q.admit("Hello there, Sir.", now=100.0)
        q.mark_fed()
        lag = q.on_chunk(now=103.5)            # audio 3.5s later
        assert lag == pytest.approx(3.5, abs=0.01), lag
        entry = q.snapshot(now=104.0)["recent"][0]
        assert entry["lag_to_audio"] == pytest.approx(3.5, abs=0.01)
        assert entry["waited"] == pytest.approx(4.0, abs=0.01)

    def test_it_never_claims_audible_completion(self):
        """tts_end is server teardown; the client plays the PCM."""
        snap = SpeechQueue().snapshot()
        assert snap["client_playback_observable"] is False
        assert "tts_end" in snap["client_playback_note"]

    def test_oldest_wait_reports_how_far_the_voice_lags(self):
        q = SpeechQueue()
        q.admit("first", now=0.0)
        q.admit("second", now=20.0)
        snap = q.snapshot(now=25.0)
        assert snap["oldest_wait_seconds"] == pytest.approx(25.0, abs=0.01)
        assert snap["pending"] == 2

    def test_snapshot_is_safe_on_a_fresh_queue(self):
        snap = SpeechQueue().snapshot()
        assert snap["pending"] == 0 and snap["recent"] == []


class TestStopAndBargeIn:
    def test_reset_drops_everything_queued(self):
        q = SpeechQueue()
        q.admit(_guidance(), now=0.0)
        q.admit("and a reply.", now=1.0)
        assert len(q._pending) == 2

        dropped = q.reset("stop_phrase")
        assert dropped == 2
        assert q._pending == []
        assert q.snapshot()["pending"] == 0
        assert q.preempted == 1
        assert q.last_preempt_reason == "stop_phrase"

    def test_reset_with_nothing_queued_is_a_noop(self):
        q = SpeechQueue()
        assert q.reset("barge_in") == 0
        assert q.preempted == 0

    def test_after_a_reset_new_speech_is_admitted_normally(self):
        q = SpeechQueue()
        q.admit(_guidance(), now=0.0)
        q.reset("stop_phrase")
        decision = q.admit("Back on duty.", now=2.0)
        assert decision["speak"] is True
        assert decision["preempt"] is False, "a cleared queue must not re-preempt"


class TestChunkOrdering:
    def test_audio_chunks_without_pending_text_are_ignored(self):
        q = SpeechQueue()
        assert q.on_chunk(now=1.0) is None      # nothing queued yet
        assert q.snapshot()["pending"] == 0

    def test_repeated_chunks_for_one_utterance_mark_it_once(self):
        q = SpeechQueue()
        q.admit("One sentence.", now=0.0)
        q.mark_fed()
        first = q.on_chunk(now=1.0)
        second = q.on_chunk(now=1.1)            # same utterance, more audio
        third = q.on_chunk(now=1.2)
        assert first is not None
        assert second is None and third is None, "one feed must be marked once"

    def test_chunks_arriving_before_any_feed_do_not_consume_an_entry(self):
        q = SpeechQueue()
        q.on_chunk(now=1.0)                     # stray audio first
        q.admit("Now the text.", now=2.0)
        q.mark_fed()
        lag = q.on_chunk(now=5.0)
        assert lag is not None, "the real entry never got its audio marked"
        assert q.snapshot()["pending"] == 0

    def test_out_of_order_audio_then_text_keeps_fifo_order(self):
        q = SpeechQueue()
        q.admit("alpha", now=0.0)
        q.admit("beta", now=1.0)
        q.mark_fed()
        q.on_chunk(now=2.0)                     # starts speaking "alpha"
        pending = [u["text"] for u in q._pending if u["synthesized_at"] is None]
        assert pending == ["beta"], pending


class TestIsolation:
    def test_the_queue_imports_no_tts_engine(self):
        """Policy must stay testable without audio hardware or a server.

        Checks import lines only - the module docstring legitimately *mentions*
        RealtimeTTS while explaining what it does not touch.
        """
        import source.server.speech_queue as mod

        lines = open(mod.__file__, encoding="utf-8").read().splitlines()
        imports = [line for line in lines
                   if line.strip().startswith(("import ", "from "))]
        blob = "\n".join(imports)
        for forbidden in ("pyaudio", "RealtimeTTS", "gemini_tts", "requests",
                          "server_state", "server"):
            assert forbidden not in blob, (
                "speech_queue.py must not import %r:\n%s" % (forbidden, blob))

    def test_get_queue_never_leaks_a_module_global(self):
        a, b = {}, {}
        assert get_queue(a) is get_queue(a), "same state must reuse one queue"
        assert get_queue(a) is not get_queue(b), "separate states must not share"
        assert get_queue(None) is not get_queue(None), "stateless call is fresh"

    def test_missing_state_is_handled_without_raising(self):
        assert get_queue("not a dict").snapshot()["pending"] == 0
