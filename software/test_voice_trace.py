"""Round ZB-VP: voice pipeline trace and its six diagnoses.

Sir's report had three symptoms - slow first reply, voice that never plays,
voice that starts then cuts off - and the log could not answer any of them
because `trace()` only wrote debug lines with no durations. This module adds the
timestamps; these tests pin the arithmetic and the six named diagnoses.

Each test drives a realistic event sequence through an injected clock, so no
real turn, provider or speaker is involved.
"""

import ast
import os
import unittest

import pytest

from source.server.voice_trace import (  # noqa: E402
    BUDGETS,
    STAGES,
    VoiceTrace,
    get_trace,
)

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")


class _Clock:
    """Deterministic monotonic/wall clock pair."""

    def __init__(self):
        self.value = 1000.0

    def mono(self):
        return self.value

    def wall(self):
        return 1_700_000_000.0 + (self.value - 1000.0)

    def advance(self, seconds):
        self.value += seconds


def _new(clock=None):
    clock = clock or _Clock()
    return VoiceTrace(clock=clock.mono, wall=clock.wall), clock


def _full_turn(trace, clock, *, capture=0.4, first_token=1.5, generate=6.0,
               tts=1.0, first_audio=0.5, slow=0.0):
    """Drive one complete, healthy turn, in the order the pipeline really runs.

    RealtimeTTS starts DURING streaming, so tts_start precedes response_complete
    (which is what closes the turn). Getting this backwards makes the stage
    arithmetic silently produce nothing.
    """
    trace.record("wake_accepted")
    clock.advance(capture); trace.record("stt_start")
    clock.advance(capture); trace.record("stt_stop")
    trace.record("stt_final")
    trace.record("turn_admitted")
    clock.advance(first_token); trace.record("llm_request_start")
    clock.advance(first_token); trace.record("llm_first_chunk")
    clock.advance(generate); trace.record("llm_thinking")
    clock.advance(tts); trace.record("tts_start", tts_stage="gemini")
    clock.advance(first_audio); trace.record("tts_first_audio")
    clock.advance(generate + slow); trace.record("response_complete",
                                                 tts_stage="gemini")
    # Playback runs past the text: RealtimeTTS calls on_audio_stream_stop last,
    # which is what proves the utterance reached the end instead of being cut.
    clock.advance(tts); trace.record("tts_end", tts_stage="gemini")
    return trace.last()


class TestStageDerivation(unittest.TestCase):
    def test_a_healthy_turn_reports_every_stage(self):
        trace, clock = _new()
        _full_turn(trace, clock)
        stages = trace.last()["stages"]
        for name, _s, _e in STAGES:
            assert name in stages, "missing stage %s: %s" % (name, sorted(stages))
        assert stages["capture"] > 0
        assert stages["first_token"] > 0
        assert stages["tts_first_audio"] > 0

    def test_a_turn_that_dies_early_reports_what_it_reached(self):
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); trace.record("stt_stop")
        clock.advance(3.0); trace.record("llm_request_start")   # no answer
        clock.advance(30.0); trace.record("response_complete", tts_stage="none")
        stages = trace.last()["stages"]
        assert "capture" in stages
        assert "generate" not in stages, "no generation happened"
        assert trace.last()["duration"] > 0

    def test_stage_windows_are_scoped_to_their_own_turn(self):
        """A stale event from a previous turn must not be attributed to this one."""
        trace, clock = _new()
        _full_turn(trace, clock)
        first = trace.last()["stages"]["capture"]
        trace.record("wake_accepted")               # new turn
        clock.advance(100.0)
        trace.record("stt_start"); clock.advance(0.2); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(0.1)
        trace.record("response_complete")           # closes the new turn
        second = trace.last()["stages"]["capture"]
        assert second < first, (
            "the new turn inherited the old turn's capture time (%s vs %s)"
            % (second, first))


class TestTheSixChecks(unittest.TestCase):
    def test_first_turn_is_reported_as_such(self):
        trace, clock = _new()
        _full_turn(trace, clock, slow=48.0)            # a slow cold turn
        clock.advance(5.0)
        _full_turn(trace, clock)                       # a fast warm turn
        diag = trace.diagnose()
        assert diag["turns"] == 2
        first = next(f for f in diag["findings"] if f["check"] == "first_turn")
        assert first["turns_total"] == 2
        assert first["slower_than_latest"] is True, (
            "a 60s first turn vs a 10s second must be flagged slower")

    def test_a_normal_first_turn_is_not_flagged(self):
        trace, clock = _new()
        _full_turn(trace, clock)
        clock.advance(5.0)
        _full_turn(trace, clock)
        first = next(f for f in trace.diagnose()["findings"]
                     if f["check"] == "first_turn")
        assert first["slower_than_latest"] is False

    def test_repeated_turns_are_counted_and_timed(self):
        trace, clock = _new()
        for _ in range(4):
            _full_turn(trace, clock)
            clock.advance(3.0)
        assert trace.turn_count() == 4
        diag = trace.diagnose()
        assert diag["turns"] == 4
        assert diag["last_turn"]["duration"] is not None

    def test_a_rejected_wake_word_does_not_create_a_turn(self):
        """`stt_start` opens a turn that `wake_rejected` must not leave open.

        Observed live: rejected phrases piled into a single phantom turn
        spanning 2128s, which then reported three false `delay` findings -
        first_token 2128s against an 8s budget, from nothing but background
        audio the wake gate correctly refused.
        """
        trace, clock = _new()
        for _ in range(5):
            trace.record("stt_start")
            clock.advance(0.4); trace.record("stt_stop")
            clock.advance(0.3)
            trace.record("wake_rejected", reason="final_text_missing_wake")
            clock.advance(300.0)              # long gap between phrases
        assert trace.recent(10) == [], "a rejected phrase was kept as a turn"
        # turn_count() counts STARTED phrases (it is also the turn index), so it
        # legitimately advances here; what must stay empty is the stored list.
        assert trace.turn_count() == 5
        diag = trace.diagnose()
        assert diag["turns"] == 0
        # With no turns, findings is a plain string list - so guard the type
        # rather than indexing findings as dicts.
        assert not [f for f in diag["findings"]
                    if isinstance(f, dict) and f.get("check") == "delay"]

    def test_a_real_turn_still_survives_rejections_before_it(self):
        """Rejections around a genuine turn must not swallow that turn."""
        trace, clock = _new()
        trace.record("stt_start"); clock.advance(0.2)
        trace.record("wake_rejected", reason="final_text_missing_wake")
        assert trace.recent(5) == []
        _full_turn(trace, clock)
        assert trace.diagnose()["turns"] == 1
        assert trace.last()["duration"] is not None

    def test_tts_start_without_first_audio_is_no_audio(self):
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); clock.advance(0.4); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(1.0)
        trace.record("llm_first_chunk"); clock.advance(2.0)
        # Order matters: RealtimeTTS starts during streaming, so tts_start
        # precedes response_complete. Completing the text first would mean
        # nothing was ever queued for speech, which is a DIFFERENT finding.
        trace.record("tts_start", tts_stage="gemini")   # audio never arrives
        trace.record("response_complete", tts_stage="gemini")
        clock.advance(30.0)
        hits = [f for f in trace.diagnose()["findings"] if f["check"] == "no_audio"]
        assert hits, trace.diagnose()["findings"]
        assert "tts_first_audio never fired" in hits[0]["detail"]

    def test_a_response_with_nothing_queued_is_no_audio(self):
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); clock.advance(0.4); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(1.0)
        trace.record("llm_first_chunk"); clock.advance(2.0)
        trace.record("response_complete")
        clock.advance(5.0)
        findings = trace.diagnose()["findings"]
        assert any(f["check"] == "no_audio" and "nothing queued" in f["detail"]
                   for f in findings), findings

    def test_a_slow_tts_is_flagged_as_delay(self):
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); clock.advance(0.4); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(1.0)
        trace.record("llm_first_chunk"); clock.advance(1.0)
        trace.record("tts_start", tts_stage="gemini")
        clock.advance(BUDGETS["tts_first_audio"] + 5.0)
        trace.record("tts_first_audio")
        trace.record("response_complete", tts_stage="gemini")
        delay = [f for f in trace.diagnose()["findings"] if f["check"] == "delay"]
        assert delay, "a slow first-audio was not flagged"
        assert any(f["stage"] == "tts_first_audio" for f in delay), delay

    def test_audio_that_never_finishes_is_a_cutoff(self):
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); clock.advance(0.4); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(1.0)
        trace.record("llm_first_chunk"); clock.advance(2.0)
        trace.record("tts_start", tts_stage="gemini")
        trace.record("tts_first_audio")             # it DID start speaking
        clock.advance(20.0)
        # The turn dies here - no response_complete, no final_response_end.
        hits = [f for f in trace.diagnose()["findings"]
                if f["check"] == "mid_sentence_cutoff"]
        assert hits, "audio started but the turn never finished - undetected"

    def test_audio_without_a_completion_event_is_a_cutoff(self):
        """A finished turn whose playback never reported completion.

        Before tts_end existed this case was invisible: the turn had
        response_complete, so the old heuristic called it healthy even though
        playback silently died mid-utterance.
        """
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); clock.advance(0.4); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(1.0)
        trace.record("llm_first_chunk"); clock.advance(2.0)
        trace.record("tts_start", tts_stage="gemini")
        trace.record("tts_first_audio")             # it DID start speaking
        trace.record("response_complete", tts_stage="gemini")
        clock.advance(20.0)
        # No tts_end - the audio stream never signalled it finished.
        hits = [f for f in trace.diagnose()["findings"]
                if f["check"] == "mid_sentence_cutoff"]
        assert hits, "playback never completed but the turn looked healthy"

    def test_a_turn_that_reports_completion_is_not_a_cutoff(self):
        """The same shape, but with tts_end - healthy."""
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); clock.advance(0.4); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(1.0)
        trace.record("llm_first_chunk"); clock.advance(2.0)
        trace.record("tts_start", tts_stage="gemini")
        trace.record("tts_first_audio")
        trace.record("response_complete", tts_stage="gemini")
        trace.record("tts_end", tts_stage="gemini")
        hits = [f for f in trace.diagnose()["findings"]
                if f["check"] == "mid_sentence_cutoff"]
        assert hits == [], hits

    def test_interrupted_playback_is_a_cutoff(self):
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); clock.advance(0.4); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(1.0)
        trace.record("llm_first_chunk"); clock.advance(2.0)
        # RealtimeTTS starts while streaming: tts_start before response_complete,
        # or the turn is closed as "nothing queued for speech" and the
        # interruption lands outside it.
        trace.record("tts_start", tts_stage="gemini")
        trace.record("tts_first_audio")
        trace.record("interrupted", reason="stop_phrase")
        trace.record("response_complete", tts_stage="gemini")
        hits = [f for f in trace.diagnose()["findings"]
                if f["check"] == "mid_sentence_cutoff"]
        assert hits, "a playback interruption was not detected"

    def test_first_token_does_not_span_a_tool_call(self):
        """Regression from the live 2026-11-01 22:04 turn.

        It reported first_token=23.609s (budget 8s) when time-to-first-token
        was really 3.0s: the stage paired the FIRST llm_request_start with the
        LAST llm_first_chunk, so the tool execution and the second LLM call
        were both swallowed into a latency number.
        """
        trace, clock = _new()
        trace.record("wake_accepted")
        clock.advance(0.4); trace.record("llm_request_start")
        clock.advance(3.0); trace.record("llm_first_chunk")      # real latency
        clock.advance(4.0); trace.record("tool_start")
        clock.advance(6.0); trace.record("tool_complete")
        clock.advance(14.0); trace.record("llm_request_start")   # 2nd call
        clock.advance(2.0); trace.record("llm_first_chunk")
        clock.advance(1.0); trace.record("tts_start", tts_stage="gemini")
        clock.advance(0.5); trace.record("tts_first_audio")
        clock.advance(6.0); trace.record("response_complete", tts_stage="gemini")
        clock.advance(5.0); trace.record("tts_end", tts_stage="gemini")

        stages = trace.last()["stages"]
        assert stages["first_token"] == pytest.approx(3.0, abs=0.001), stages
        assert stages["first_token"] < 8.0, "tool time leaked into first_token"

    def test_tts_end_closes_the_turn_and_keeps_the_generate_stage(self):
        """response_complete arrives while audio is playing; tts_end must still
        land inside this turn, or `generate` is silently lost."""
        trace, clock = _new()
        trace.record("wake_accepted")
        clock.advance(0.4); trace.record("stt_start")
        clock.advance(0.4); trace.record("stt_stop")
        clock.advance(1.0); trace.record("llm_request_start")
        clock.advance(1.0); trace.record("llm_first_chunk")
        clock.advance(1.0); trace.record("tts_start", tts_stage="gemini")
        clock.advance(0.5); trace.record("tts_first_audio")
        clock.advance(6.0); trace.record("response_complete", tts_stage="gemini")
        assert trace.last() is None, "closed on response_complete with audio playing"
        clock.advance(5.0); trace.record("tts_end", tts_stage="gemini")

        turn = trace.last()
        assert turn is not None, "tts_end never closed the turn"
        assert "tts_end" in turn["events"]
        assert "response_complete" in turn["events"]
        assert "generate" in turn["stages"], turn["stages"]
        assert "capture" in turn["stages"], turn["stages"]

    def test_the_timeline_correlates_events_to_log_lines(self):
        """Each event must carry a stamp in friday.log's own format.

        Without it, "inspect that turn" is ambiguous across a session.
        """
        trace, clock = _new()
        trace.record("wake_accepted")
        clock.advance(1.0); trace.record("llm_request_start")
        clock.advance(3.0); trace.record("llm_first_chunk")

        timeline = trace.timeline()
        assert len(timeline) == 3
        assert [t["event"] for t in timeline] == [
            "wake_accepted", "llm_request_start", "llm_first_chunk"]
        for entry in timeline:
            # YYYY-MM-DD HH:MM:SS, exactly what friday.log prints.
            assert len(entry["at"]) == 19 and entry["at"][4] == "-" \
                and entry["at"][13] == ":", entry
        # Ordering must be monotonic in wall time.
        stamps = [t["at"] for t in timeline]
        assert stamps == sorted(stamps), stamps

    def test_a_preformatted_rejection_still_closes_the_turn(self):
        """THE live defect, reproduced with production's real call shape.

        server.py calls `trace("wake_rejected reason=final_text_missing_wake")`
        - a single pre-formated string - not `record("wake_rejected",
        reason=...)`. Comparing that against the bare name is always False, so
        the rejection branch never ran: observed live, one turn swallowed FIVE
        rejected utterances, grew to 217s, and turn_count stayed at 1 because
        the turn never closed so no new one could open.

        The earlier tests passed only because they used the idealised API the
        production code does not call.
        """
        trace, clock = _new()
        trace.record("stt_start")
        trace.record("stt_final text='It was cold.'")
        trace.record("wake_filter final='it was cold.' matched=none")
        trace.record("wake_rejected reason=final_text_missing_wake")

        assert trace._turn is None, "the detail-suffixed rejection never matched"
        assert trace.recent(10) == [], "a rejected utterance was kept as a turn"

        # The full string is preserved for friday.log correlation.
        stored = [e for _w, _m, e, _d in trace._events]
        assert any(e.startswith("wake_rejected reason=") for e in stored), stored

        # And the next stt_start can open a fresh turn (this is what failed
        # live: turn_count stayed at 1 across five starts).
        trace.record("stt_start")
        assert trace.turn_count() == 2, "the next utterance could not start a turn"

    def test_a_bookkeeping_failure_cannot_strand_the_live_turn(self):
        """Reproduces the live defect: one turn swallowed FIVE wake_rejections.

        record() must never raise, so its blanket `except: pass` used to sit
        over BOTH the append and the turn-boundary decision - any failure in
        the append skipped the close, the turn stayed open, and it went on
        accumulating every later utterance (observed: 217s, turn_count stuck
        at 1 across five stt_starts).
        """
        trace, clock = _new()
        trace.record("stt_start")
        # A bookkeeping step that genuinely raises: `events` is no longer a
        # list, so the append inside record() throws before the close branch.
        trace._turn["events"] = None

        trace.record("wake_rejected", reason="final_text_missing_wake")

        assert trace._turn is None, "the live turn was stranded open"
        assert trace.recent(10) == [], "a rejected utterance was kept as a turn"
        # The rejection itself must still be evidence.
        assert any(e[2] == "wake_rejected" for e in trace._events)

    def test_a_failing_stage_derivation_cannot_strand_the_live_turn(self):
        """`_turn = None` must not depend on the stage maths succeeding."""
        trace, clock = _new()
        trace.record("wake_accepted")

        def boom(_turn):
            raise RuntimeError("derive failed")

        trace._derive_stages = boom
        trace.record("response_complete", tts_stage="gemini")

        assert trace._turn is None, "_turn = None only ran after _derive_stages"
        assert len(trace.recent(10)) == 1, "the turn was lost instead of stored"

    def test_a_complete_turn_is_not_a_cutoff(self):
        trace, clock = _new()
        _full_turn(trace, clock)
        hits = [f for f in trace.diagnose()["findings"]
                if f["check"] == "mid_sentence_cutoff"]
        assert hits == [], hits

    def test_the_serving_stage_is_reported(self):
        trace, clock = _new()
        _full_turn(trace, clock)
        findings = trace.diagnose()["findings"]
        fallback = next(f for f in findings if f["check"] == "fallback")
        assert fallback["tts_stage"] == "gemini"

    def test_a_turn_with_no_tts_stage_is_a_failed_fallback(self):
        trace, clock = _new()
        trace.record("wake_accepted")
        trace.record("stt_start"); clock.advance(0.4); trace.record("stt_stop")
        trace.record("llm_request_start"); clock.advance(1.0)
        trace.record("llm_first_chunk"); clock.advance(2.0)
        trace.record("response_complete", tts_stage="none")
        trace.record("tts_start", tts_stage="none")
        findings = trace.diagnose()["findings"]
        assert any(f["check"] == "fallback_failed" for f in findings), (
            "a turn served by no TTS stage was not reported as a failure")


class TestRecorderSafety(unittest.TestCase):
    def test_recording_never_raises(self):
        trace, _clock = _new()
        trace.record(None)
        trace.record("")
        trace.record("   ")
        trace.record(12345)
        assert trace.turn_count() == 0

    def test_the_buffers_are_bounded(self):
        trace, clock = _new()
        for _ in range(3000):
            trace.record("stt_start")
        assert len(trace._events) <= 500
        for _ in range(200):
            trace.record("wake_accepted")
        assert len(trace._turns) <= 40

    def test_an_empty_trace_says_so(self):
        trace, _clock = _new()
        assert trace.summary() == "No voice turns recorded yet."
        assert trace.diagnose()["turns"] == 0

    def test_the_singleton_is_reused(self):
        assert get_trace() is get_trace()

    def test_the_summary_names_the_stages(self):
        trace, clock = _new()
        _full_turn(trace, clock)
        text = trace.summary()
        assert "last 1 turn(s)" in text
        assert "capture=" in text
        assert "tts_first_audio=" in text


class TestServerWiring(unittest.TestCase):
    def test_the_route_exists_and_is_guarded(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            source = handle.read()
        assert '@interpreter.server.app.get("/voice/trace")' in source
        tree = ast.parse(source)
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "voice_trace_report")
        body = ast.unparse(node)
        assert "_guard(request)" in body
        assert body.index("_guard(request)") < body.index("diagnose")

    def test_trace_records_into_the_recorder(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            source = handle.read()
        assert "_voice_trace_get().record(event" in source

    def test_tts_start_reports_its_stage(self):
        """Without the stage, 'which fallback served this turn?' is unanswerable."""
        with open(SERVER_PY, encoding="utf-8") as handle:
            source = handle.read()
        assert source.count('trace("tts_start", tts_stage=_tts_stage_now())') >= 3
        assert "def _tts_stage_now" in source

    def test_playback_completion_is_traced(self):
        """`tts_end` must be wired at the stream, not assumed elsewhere.

        RealtimeTTS only reports completion through on_audio_stream_stop; with
        no callback installed, tts_start is the last event a turn emits and
        "she stopped mid-sentence" becomes indistinguishable from "she
        finished" - the exact diagnosis that was impossible before.
        """
        with open(SERVER_PY, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "TextToAudioStream"]
        assert calls, "TextToAudioStream construction not found in server.py"
        wired = [c for c in calls
                 if "on_audio_stream_stop" in {kw.arg for kw in c.keywords}]
        assert wired, "TextToAudioStream built without a completion callback"
        # ast.unparse normalises quote style, so match the token not the quotes.
        wiring = " ".join(ast.unparse(c) for c in wired)
        assert "tts_end" in wiring and "trace(" in wiring, wiring


if __name__ == "__main__":
    unittest.main()
