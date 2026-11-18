"""Total TTS failure must not poison playback (ROUND ZB-REC, 2026-11-02).

The incident: Gemini returned no audio, the local edge server returned HTTP 500,
and RealtimeTTS's `play()` then re-entered itself from `text_to_stream.py:451`
until `RecursionError`. Because that raise lands OUTSIDE play()'s try/finally,
`is_playing_flag` and `play_lock` were left latched, so every later
`play_async()` was refused for the rest of the process - no `tts_first_audio`,
no `tts_end` - and the 180s watchdog reset the TURN but never the latch.

We cannot patch the wheel, so these tests pin the FRIDAY-side defence:
a bounded circuit breaker at the engine, a latch repair, and a truthful
(non-audible) degradation signal.
"""

import os
import threading
import time
import types

import pytest

from source.server import gemini_tts
from source.server import server as srv

# Resolved from THIS file, not os.getcwd(): another suite in the full run
# chdirs into a temporary skill directory, which made a cwd-based path resolve
# to a nonexistent source/server/server.py and fail only under `pytest -q`.
SERVER_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "source", "server", "server.py")


# ---------------------------------------------------------------------------
# Engine: bounded attempts, failure -> recovery, no-audio honesty
# ---------------------------------------------------------------------------
def _engine(monkeypatch, *, speak=None, local=None):
    """An engine whose cloud/local stages are fully under test control.

    The fake `speak` records LAST_STAGE on the voice instance exactly like the
    real one does (gemini/local/sapi on success, "none" when every stage is
    dead), so the engine's mirroring logic is actually exercised.
    """
    engine = gemini_tts.CloudFirstTTSEngine()
    calls = {"speak": 0, "local": 0}

    def fake_speak(text):
        calls["speak"] += 1
        engine.voice_bridge.LAST_STAGE = "gemini" if speak else "none"
        return speak

    def fake_local(text, allow_start=True):
        calls["local"] += 1
        return local if local is not None else (False, None, "", "local down")

    monkeypatch.setattr(engine.voice_bridge, "speak", fake_speak)
    monkeypatch.setattr(gemini_tts, "_local_pcm", fake_local)
    return engine, calls


class TestTotalFailureIsBounded:
    def test_failure_streak_trips_the_breaker(self, monkeypatch):
        engine, calls = _engine(monkeypatch, speak=None)

        for _ in range(engine.DEGRADE_AFTER):
            assert engine.synthesize("hello") is False

        assert engine.is_degraded() is True, "breaker never tripped"
        status = engine.degraded_status()
        assert status["degraded"] is True
        assert "all TTS engines failed" in status["reason"]
        assert status["retry_in_seconds"] > 0

    def test_while_degraded_no_engine_is_called_again(self, monkeypatch):
        """The bounded part: no cloud+local+SAPI attempt per dead sentence."""
        engine, calls = _engine(monkeypatch, speak=None)
        for _ in range(engine.DEGRADE_AFTER):
            engine.synthesize("hello")
        spent = (calls["speak"], calls["local"])

        for _ in range(20):
            assert engine.synthesize("more text") is False

        assert (calls["speak"], calls["local"]) == spent, (
            "a degraded engine still burned %d extra attempts"
            % (calls["speak"] - spent[0] + calls["local"] - spent[1]))

    def test_failure_queues_nothing(self, monkeypatch):
        engine, _ = _engine(monkeypatch, speak=None)
        engine.synthesize("hello")
        assert engine.queue.empty(), "failure produced audio anyway"

    def test_degraded_status_never_claims_speech_is_possible(self, monkeypatch):
        engine, _ = _engine(monkeypatch, speak=None)
        for _ in range(engine.DEGRADE_AFTER):
            engine.synthesize("hello")
        status = engine.degraded_status()
        assert status["degraded"] is True
        assert status["last_stage"] == "none", status
        # No stage that implies audio was produced.
        assert status["last_stage"] not in ("gemini", "local", "sapi")


class TestFailureThenRecovery:
    def test_a_later_healthy_turn_speaks_again(self, monkeypatch):
        """THE requirement: after the outage, a healthy turn must work."""
        engine, calls = _engine(monkeypatch, speak=None)
        for _ in range(engine.DEGRADE_AFTER):
            engine.synthesize("down")
        assert engine.is_degraded() is True

        # Cooldown elapses (as it would after a 60s quota window).
        engine._degraded_until = time.monotonic() - 1.0
        assert engine.is_degraded() is False, "breaker never released"
        assert engine.degraded_status()["degraded"] is False
        assert engine.degraded_status()["reason"] == ""

        # A healthy engine is now reachable again.
        monkeypatch.setattr(engine.voice_bridge, "speak",
                            lambda text: b"AUDIO" * 8)
        assert engine.synthesize("healthy again") is True
        assert not engine.queue.empty()
        assert engine.is_degraded() is False
        assert engine.degraded_status()["fail_streak"] == 0

    def test_one_success_resets_the_streak_immediately(self, monkeypatch):
        engine, _ = _engine(monkeypatch, speak=None)
        for _ in range(engine.DEGRADE_AFTER - 1):
            engine.synthesize("down")
        assert engine.is_degraded() is False

        monkeypatch.setattr(engine.voice_bridge, "speak",
                            lambda text: b"AUDIO" * 4)
        assert engine.synthesize("ok") is True
        assert engine.degraded_status()["fail_streak"] == 0

        # And it takes a FULL new streak to degrade again.
        monkeypatch.setattr(engine.voice_bridge, "speak", lambda text: None)
        for _ in range(engine.DEGRADE_AFTER - 1):
            assert engine.synthesize("down") is False
        assert engine.is_degraded() is False


class TestStageIsMirroredOntoTheEngine:
    def test_successful_cloud_stage_is_visible_on_the_engine(self, monkeypatch):
        """`speak()` records on the VOICE instance; _tts_stage_now() reads the
        engine - without mirroring, a good utterance reported stage 'unknown'."""
        engine, _ = _engine(monkeypatch, speak=b"AUDIO" * 4)
        assert engine.synthesize("hi") is True
        assert engine.LAST_STAGE == "gemini", engine.LAST_STAGE

    def test_total_failure_is_recorded_as_none(self, monkeypatch):
        engine, _ = _engine(monkeypatch, speak=None)
        engine.synthesize("hi")
        assert engine.LAST_STAGE == "none"


# ---------------------------------------------------------------------------
# Latch repair - the mechanism that actually un-wedged playback
# ---------------------------------------------------------------------------
class _FakeTTS:
    def __init__(self, *, flag=True, playing=False, thread_alive=False, locked=True):
        self.is_playing_flag = flag
        self._playing = playing
        self.play_lock = threading.Lock()
        if locked and self.play_lock.locked() is False:
            self.play_lock.acquire()
        self.play_thread = types.SimpleNamespace(is_alive=lambda: thread_alive)

    def is_playing(self):
        return self._playing


class TestLatchRecovery:
    def test_stale_latch_from_a_dead_play_thread_is_repaired(self):
        tts = _FakeTTS(flag=True, playing=False, thread_alive=False, locked=True)
        assert srv._tts_recover_latch(tts) is True
        assert tts.is_playing_flag is False
        assert tts.play_lock.locked() is False

    def test_a_live_playback_thread_is_never_disturbed(self):
        """Completion/cancellation: a stream that is genuinely playing stays."""
        tts = _FakeTTS(flag=True, playing=True, thread_alive=True, locked=True)
        assert srv._tts_recover_latch(tts) is False
        assert tts.is_playing_flag is True
        assert tts.play_lock.locked() is True

    def test_a_running_thread_alone_blocks_the_repair(self):
        tts = _FakeTTS(flag=True, playing=False, thread_alive=True, locked=True)
        assert srv._tts_recover_latch(tts) is False
        assert tts.is_playing_flag is True

    def test_no_latch_means_nothing_to_do(self):
        tts = _FakeTTS(flag=False, playing=False, thread_alive=False)
        assert srv._tts_recover_latch(tts) is False
        assert tts.is_playing_flag is False

    def test_none_and_foreign_objects_are_safe(self):
        assert srv._tts_recover_latch(None) is False
        assert srv._tts_recover_latch(object()) is False
        assert srv._tts_recover_latch(types.SimpleNamespace()) is False

    def test_a_repair_actually_lets_playback_start_again(self):
        """The real sequence: latched stream -> repair -> flag free."""
        tts = _FakeTTS(flag=True, playing=False, thread_alive=False, locked=True)
        assert tts.is_playing_flag is True          # play_async would refuse
        assert srv._tts_recover_latch(tts) is True
        assert tts.is_playing_flag is False         # play_async will now run


# ---------------------------------------------------------------------------
# Degraded gating at the playback boundary
# ---------------------------------------------------------------------------
class TestDegradedGating:
    def test_no_engine_reports_healthy(self):
        assert srv.tts_is_degraded({}) is False
        assert srv.tts_is_degraded(None) is False

    def test_an_engine_without_a_health_api_never_blocks_speech(self):
        state = {"tts_engine": object()}
        assert srv.tts_is_degraded(state) is False

    def test_status_is_safe_when_there_is_no_engine(self):
        status = srv.tts_degraded_status({})
        assert status["degraded"] is False
        assert status["reason"] == ""

    def test_note_records_a_truthful_signal_without_a_voice(self, monkeypatch):
        engine, _ = _engine(monkeypatch, speak=None)
        for _ in range(engine.DEGRADE_AFTER):
            engine.synthesize("x")
        state = {"tts_engine": engine}

        status = srv.note_tts_degraded(state)
        assert status["degraded"] is True
        assert state["tts_degraded"]["reason"]
        assert srv.tts_is_degraded(state) is True
        # It must be a STATUS, not a promise of audio.
        assert "speak" not in state["tts_degraded"]

    def test_clearing_happens_only_once_audio_flows_again(self, monkeypatch):
        """The REAL on_tts_chunk body must drop the record when audio resumes.

        Executed from source (like test_tts_audio_marker's harness) because
        that function must stay compilable in isolation - so the clear is
        inline in production and cannot be reached through a helper here.
        """
        import ast
        import os

        path = SERVER_PY
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_record_tts_chunk")
        body = "\n".join(ast.unparse(s) for s in fn.body)

        engine, _ = _engine(monkeypatch, speak=None)
        for _ in range(engine.DEGRADE_AFTER):
            engine.synthesize("x")
        state = {"tts_engine": engine}
        srv.note_tts_degraded(state)
        assert state["tts_degraded"] is not None

        events = []
        scope = {
            "server_state": state,
            "trace": lambda name: events.append(name),
            "self": types.SimpleNamespace(
                output_queue=types.SimpleNamespace(
                    sync_q=types.SimpleNamespace(put=lambda *_: None))),
        }
        exec("def on_chunk(chunk):\n"
             + "\n".join("    " + line for line in body.splitlines()),
             scope)
        scope["on_chunk"]("audio-bytes")

        assert state["tts_degraded"] is None, "real handler did not clear the record"
        assert events == ["tts_first_audio"], events

    def test_the_watchdog_repairs_the_latch(self):
        """Source-level: the reset path must not leave playback latched."""
        import ast
        import os

        path = SERVER_PY
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        watchdog = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == "_finish_budgeted_turn"),
            None,
        )
        assert watchdog is not None, "watchdog_loop not found"
        body = ast.unparse(watchdog)
        assert "_tts_recover_latch" in body, (
            "the watchdog resets the turn but never the TTS latch - that is "
            "exactly what left playback dead for the rest of the session")
        assert "_stop_active_turn" in body

    def test_every_playback_site_is_guarded(self):
        """Source-level, matching test_tts_audio_marker's precedent.

        Each site that calls play_async(on_audio_chunk=self.on_tts_chunk) must
        consult the breaker or repair the latch within the preceding lines -
        otherwise a degraded engine would still be fed and a latched stream
        would still be refused.
        """
        import ast
        import os

        path = SERVER_PY
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        tree = ast.parse("\n".join(lines))

        sites = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            try:
                text = ast.unparse(node)
            except Exception:
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "_start_owned_playback":
                sites.append(node.lineno)

        assert len(sites) >= 3, (
            "expected the three known playback sites, found %d" % len(sites))
        for lineno in sites:
            # Window must reach past the ROUND ZB-CH admission block (supersede/
            # preempt) that now sits between the breaker check and play_async,
            # AND past the ROUND ZB-C phrase label that is emitted just before
            # playback so the client can caption/ack it. Widened 48 -> 64 for
            # that block; the invariant is unchanged - every playback site must
            # still have the breaker (or the latch) in reach.
            window = "\n".join(lines[max(0, lineno - 64):lineno])
            assert ("tts_is_degraded" in window) or ("_tts_recover_latch" in window), (
                "unguarded playback site at server.py:%d\n%s" % (lineno, window))
