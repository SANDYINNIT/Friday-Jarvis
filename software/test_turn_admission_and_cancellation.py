"""Admission, cancellation and tool bounding (ROUND ZB-P1).

Reconstructed from the 2026-11-02 incident:
  * an accepted stop request produced NO log line at all - no `brain select`,
    no `llm_request_start` - and only the 180s watchdog revealed it;
  * the deferred utterance re-armed the RUNNING turn's watchdog, pushing stall
    detection from ~14:29:06 to 14:31:38 and resetting the 600s budget;
  * a stop phrase silenced TTS only, so the tool kept running;
  * no per-tool wall-clock bound existed at all (one script held a turn ~9.4
    minutes while console output kept arriving).
"""

import ast
import os
import re
import time
import types

import pytest

from source.server import server as srv
from source.server.server import (
    _pending_count,
    _pending_push,
    _pending_queue,
    enforce_tool_timeout,
    restore_turn_baseline,
    snapshot_turn_baseline,
)
from test_intent_judge_removed import _VoiceHarness

SERVER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "source", "server", "server.py")


def _source():
    with open(SERVER_PY, encoding="utf-8") as handle:
        return handle.read()


def _state():
    return {"pending_turns": __import__("collections").deque()}


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    warning = error = debug = info


# ---------------------------------------------------------------------------
# Deferral must be observable (it was completely silent)
# ---------------------------------------------------------------------------
class TestDeferralIsObservable:
    def test_a_deferred_request_is_logged_with_source_and_text(self, monkeypatch):
        log = _Log()
        monkeypatch.setattr(srv, "_turn_log", log)
        state = _state()

        _pending_push(state, user_text="please stop", source="voice")

        assert len(log.lines) == 1, log.lines
        line = log.lines[0]
        assert "admission deferred" in line
        assert "voice" in line
        assert "please stop" in line

    def test_concurrent_utterances_each_produce_their_own_line(self, monkeypatch):
        """Two requests arriving together are both visible, not merged."""
        log = _Log()
        monkeypatch.setattr(srv, "_turn_log", log)
        state = _state()

        _pending_push(state, user_text="first", source="voice")
        _pending_push(state, user_text="second", source="text")

        assert len(log.lines) == 2, log.lines
        assert "first" in log.lines[0] and "voice" in log.lines[0]
        assert "second" in log.lines[1] and "text" in log.lines[1]
        assert _pending_count(state) == 2

    def test_the_deferral_is_also_traced_on_the_voice_path(self):
        """The voice gate must leave a trace, not only the shared log line."""
        tree = ast.parse(_source())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "dispatch_text")
        source = ast.unparse(fn)
        assert re.search(r"trace\('turn_deferred'", source), (
            "the voice admission gate does not trace its deferral")
        # ...and the trace must sit alongside a real push, not somewhere random.
        assert source.index("_pending_push") < source.index("turn_deferred")


# ---------------------------------------------------------------------------
# A deferred request must not extend the turn it waits behind (watchdog race)
# ---------------------------------------------------------------------------
class TestWatchdogBaselineIsProtected:
    def test_restore_returns_every_field_the_stamp_overwrote(self):
        state = {
            "last_progress": 100.0,
            "turn_started_at": 90.0,
            "turn_progress_observed": True,
            "turn_budget_enforced": True,
            "llm_response_shape": {"kept": True},
        }
        before = snapshot_turn_baseline(state)

        srv._stamp_progress(state, turn_started=True)   # the admission stamp
        assert state["turn_started_at"] != 90.0, "stamp did not move the clock"

        restore_turn_baseline(state, before)
        assert state["last_progress"] == 100.0
        assert state["turn_started_at"] == 90.0
        assert state["turn_progress_observed"] is True
        assert state["turn_budget_enforced"] is True
        assert state["llm_response_shape"] == {"kept": True}

    def test_a_deferred_request_cannot_reset_the_running_turn_budget(self):
        """The incident: a queued wake re-stamped `turn_started_at`, so the
        running turn's 600s budget restarted and could never fire."""
        state = {"last_progress": 100.0, "turn_started_at": 90.0,
                 "turn_progress_observed": True, "turn_budget_enforced": True}
        baseline = snapshot_turn_baseline(state)
        srv._stamp_progress(state, turn_started=True)
        # ...the request is then DEFERRED, not dispatched:
        restore_turn_baseline(state, baseline)

        assert state["turn_started_at"] == 90.0, (
            "a deferred request moved the running turn's budget origin")

    def test_the_snapshot_covers_exactly_what_the_stamp_writes(self):
        """Guard against the two lists drifting apart."""
        assert set(srv._WATCHDOG_TURN_FIELDS) == {
            "last_progress", "turn_started_at", "turn_progress_observed",
            "turn_budget_enforced", "llm_response_shape",
        }
        tree = ast.parse(_source())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_stamp_progress")
        body = ast.unparse(fn)
        for field in srv._WATCHDOG_TURN_FIELDS:
            if field == "llm_response_shape":
                assert "_reset_llm_response_shape" in body
            else:
                assert field in body, "field %s is not written by the stamp" % field

    def test_both_admission_paths_snapshot_before_stamping(self):
        tree = ast.parse(_source())
        for name in ("new_input",):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == name)
            body = ast.unparse(fn)
            assert "snapshot_turn_baseline" in body, "%s never snapshots" % name
            assert "restore_turn_baseline" in body, "%s never restores" % name
            # Snapshot must precede the turn-start stamp it protects.
            assert (body.index("snapshot_turn_baseline")
                    < body.index("_stamp_progress(server_state, turn_started=True)")), (
                "%s stamps before it snapshots - the baseline is already lost" % name)
        dispatch = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == "dispatch_text")
        assert "_stamp_progress" not in ast.unparse(dispatch)


# ---------------------------------------------------------------------------
# A spoken stop must cancel the WORK and still be acknowledged
# ---------------------------------------------------------------------------
def _stop_branch():
    tree = ast.parse(_source())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "new_input")
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "_STOP_PHRASE_RE" in ast.unparse(node.test):
            return node
    raise AssertionError("stop-phrase branch not found")


class TestStopCancelsAndAcknowledges:
    def test_it_cancels_the_active_turn_not_only_the_audio(self):
        """The old branch only called tts.stop(), so a long tool kept running."""
        assert "_cancel_turn(" in ast.unparse(_stop_branch())
        tree = ast.parse(_source())
        cancel = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "_cancel_turn")
        assert "interpreter.stop_event.set()" in ast.unparse(cancel)

    def test_it_does_not_swallow_the_utterance(self):
        """No `return`: the words must still be admitted and answered.

        On 2026-11-02 the stop request was accepted and then never answered
        for 3m17s. Swallowing it here would reproduce that exactly.
        """
        branch = _stop_branch()
        last = branch.body[-1]
        assert not isinstance(last, ast.Return), (
            "the stop branch still returns early, so the request is swallowed "
            "instead of acknowledged:\n%s" % ast.unparse(last)[:300])

    def test_it_traces_the_cancellation(self):
        body = ast.unparse(_stop_branch())
        assert re.search(r"trace\('stop_honored'", body), (
            "cancellation is not traced")

    def test_it_still_silences_audio_that_is_already_playing(self):
        """Barge-in must survive: the condition still covers live playback."""
        branch = _stop_branch()
        test = ast.unparse(branch.test)
        assert "self.tts.is_playing()" in test
        assert "turn_is_busy()" in test

    def test_the_cancellation_uses_the_documented_mechanism(self):
        """`stop_event` is what the watchdog and HARD tool cap use."""
        tree = ast.parse(_source())
        watchdog = next(n for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name == "_finish_budgeted_turn")
        assert "_stop_active_turn" in ast.unparse(watchdog), (
            "the watchdog no longer cancels through _stop_active_turn")


# ---------------------------------------------------------------------------
# Slow tools: a per-tool wall-clock bound
# ---------------------------------------------------------------------------
class TestToolTimeout:
    def test_a_slow_tool_is_cancelled_at_the_bound(self):
        timing = {"started": time.monotonic() - 999}
        stop = types.SimpleNamespace(calls=0)
        stop.set = lambda: setattr(stop, "calls", stop.calls + 1)
        interp = types.SimpleNamespace(stop_event=stop)

        fired = enforce_tool_timeout(timing, interp, limit=60)

        assert fired is not None and fired >= 60
        assert stop.calls == 1, "the turn was not cancelled"
        assert timing["started"] is None, "the timer was not cleared"

    def test_a_tool_inside_its_budget_is_left_alone(self):
        timing = {"started": time.monotonic()}
        stop = types.SimpleNamespace(calls=0)
        stop.set = lambda: setattr(stop, "calls", stop.calls + 1)

        assert enforce_tool_timeout(timing, types.SimpleNamespace(stop_event=stop),
                                    limit=60) is None
        assert stop.calls == 0
        assert timing["started"] is not None

    def test_no_running_tool_is_a_noop(self):
        assert enforce_tool_timeout({}, types.SimpleNamespace()) is None
        assert enforce_tool_timeout({"started": None}, types.SimpleNamespace()) is None

    def test_a_missing_interpreter_cannot_break_the_loop(self):
        timing = {"started": time.monotonic() - 999}
        assert enforce_tool_timeout(timing, object(), limit=1) is not None

    def test_the_bound_is_configurable(self):
        timing = {"started": time.monotonic() - 5}
        assert enforce_tool_timeout(timing, types.SimpleNamespace(), limit=60) is None
        assert enforce_tool_timeout(timing, types.SimpleNamespace(), limit=1) is not None

    def test_the_output_loop_actually_enforces_it(self):
        tree = ast.parse(_source())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "new_output")
        body = ast.unparse(fn)
        assert "enforce_tool_timeout" in body, (
            "the output loop never bounds tool runtime")
        assert re.search(r"trace\('tool_timeout'", body)

    def test_the_default_limit_is_a_sane_bounded_value(self):
        assert 10.0 <= srv.TOOL_TIMEOUT_SECONDS <= 900.0


# ---------------------------------------------------------------------------
# Rejected background audio must never reach admission
# ---------------------------------------------------------------------------
class TestRejectedAudioNeverAdmits:
    def test_a_rejected_utterance_reaches_nothing(self):
        """Background speech with no wake word must not queue or dispatch."""
        harness = _VoiceHarness("it was cold outside tonight")
        harness.run()
        names = harness.names()
        assert "_pending_push" not in names, (
            "rejected background audio was queued: %s" % names)
        assert "_begin_single_turn" not in names, (
            "rejected background audio started a turn")
        assert "_mark_turn_dispatch" not in names
        assert "dispatch_text" not in names
        assert "wake_accepted" not in names

    def test_an_accepted_utterance_does_reach_admission(self):
        """Negative control: the same harness admits real wake-word speech."""
        harness = _VoiceHarness("friday what is 17 times 23")
        harness.run()
        assert "dispatch_text" in harness.names()
