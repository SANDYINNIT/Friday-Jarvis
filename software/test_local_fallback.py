"""Local TTS fallback: bounded start, readiness cache, recovery, concurrency.

Sir's directive 2026-11-01: when Gemini TTS is exhausted or unavailable the
LOCAL voice must work rather than returning silence. Two things made that
impossible before:

  1. `ensure_edge_tts_server()` set `_started = True` on EVERY path including
     the failures, so one transient error (missing project, spawn exception,
     startup timeout) muted the local voice for the rest of the session.
  2. The edge-tts server can be perfectly healthy on `/v1/models` and still
     500 on `/v1/audio/speech` - observed as a persistent upstream
     `WSServerHandshakeError: 403` from Microsoft - so "the server is running"
     never implied "FRIDAY can speak".

Nothing here touches the network, spawns a process or needs a real server:
every external edge is replaced with a fake.
"""

import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from source.server import gemini_tts
from source.server import tts_bootstrap as tb

_TEMP_PROJECTS = []


@pytest.fixture(autouse=True)
def _clean_state():
    tb._reset_state()
    yield
    tb._reset_state()
    while _TEMP_PROJECTS:
        shutil.rmtree(_TEMP_PROJECTS.pop(), ignore_errors=True)


class _FakeChild:
    """Stands in for the spawned Popen."""

    def __init__(self, exit_code=None):
        self.returncode = exit_code

    def poll(self):
        return self.returncode


def _stub_project():
    """A directory that satisfies the `app/server.py` layout check."""
    root = tempfile.mkdtemp(prefix="edge_stub_")
    _TEMP_PROJECTS.append(root)
    (Path(root) / "app").mkdir(parents=True, exist_ok=True)
    (Path(root) / "app" / "server.py").write_text("# stub\n", encoding="utf-8")
    return root


def _stub_python():
    """A real file, so `Path(exe).is_file()` passes without spawning anything."""
    handle = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
    handle.close()
    _TEMP_PROJECTS.append(handle.name)
    return handle.name


def _patch(monkeypatch, *, probe_seq=None, spawn=None, project="P",
           python="__auto__", port_open=False, patch_degraded=True):
    """Replace every external edge with a controllable fake.

    `probe_seq` is a list consumed one call at a time; the last value repeats.
    `project=None` simulates "no edge-tts project found"; any other value is
    turned into a real stub directory layout unless it already exists.
    `python=None` simulates "no interpreter"; the default creates a real file.
    """
    calls = {"probe": 0, "spawn": 0, "degraded": []}
    seq = list(probe_seq or [True])
    if python == "__auto__":
        python = _stub_python()

    def fake_probe(base_url=None, timeout=3.0):
        calls["probe"] += 1
        value = seq[min(calls["probe"] - 1, len(seq) - 1)]
        return value

    def fake_spawn(project_dir, python_exe, base_url=None):
        calls["spawn"] += 1
        if isinstance(spawn, Exception):
            raise spawn
        return spawn() if callable(spawn) else (spawn or _FakeChild())

    monkeypatch.setattr(tb, "probe", fake_probe)
    monkeypatch.setattr(tb, "_spawn_server", fake_spawn)
    monkeypatch.setattr(tb, "find_edge_tts_project",
                        lambda: (project if project is None or Path(project).is_dir()
                                 else _stub_project()))
    monkeypatch.setattr(tb, "find_tts_python", lambda: python)
    monkeypatch.setattr(tb, "_port_open", lambda *a, **k: port_open)
    if patch_degraded:
        monkeypatch.setattr(
            tb, "mark_degraded",
            lambda reason, cooldown=None: calls["degraded"].append(str(reason)),
        )
    return calls


# --------------------------------------------------------------------------
# first turn after boot / healthy reuse
# --------------------------------------------------------------------------

def test_first_use_starts_the_server_then_becomes_ready(monkeypatch):
    """The first fallback after boot may pay the spawn; it must then be ready."""
    child = _FakeChild()
    calls = _patch(monkeypatch, probe_seq=[False, True], spawn=child)

    assert tb.ensure_edge_tts_server(timeout=5) is True
    assert calls["spawn"] == 1, "first use must actually spawn the server"
    assert tb.last_failure() == ""


def test_healthy_reuse_pays_no_probe_and_no_spawn(monkeypatch):
    """A ready server must cost one clock read per sentence, not an HTTP call.

    This is the per-sentence penalty Sir asked us to avoid: without the cache
    every utterance would round-trip to localhost before speaking.
    """
    calls = _patch(monkeypatch, probe_seq=[True], spawn=_FakeChild())

    assert tb.ensure_edge_tts_server() is True
    probes_after_first = calls["probe"]

    for _ in range(20):
        assert tb.ensure_edge_tts_server() is True

    assert calls["probe"] == probes_after_first, "cached readiness re-probed"
    assert calls["spawn"] == 0


def test_readiness_expires_and_is_revalidated(monkeypatch):
    """The cache is a TTL, not a latch - a server that dies must be noticed."""
    calls = _patch(monkeypatch, probe_seq=[True, False], spawn=_FakeChild())
    assert tb.ensure_edge_tts_server() is True

    monkeypatch.setattr(tb, "READY_TTL", 0.0)      # expire immediately
    assert tb.ensure_edge_tts_server() is False, "expired cache hid a dead server"
    assert calls["probe"] >= 2, "expiry must trigger a real re-probe"


# --------------------------------------------------------------------------
# failure is a cooldown, never a permanent mute
# --------------------------------------------------------------------------

def test_failure_is_recoverable_not_a_permanent_latch(monkeypatch):
    """The old `_started = True` on failure muted the voice forever."""
    calls = _patch(monkeypatch, probe_seq=[False],
                   spawn=RuntimeError("no interpreter"))
    assert tb.ensure_edge_tts_server() is False
    assert "no interpreter" in tb.last_failure()
    assert calls["spawn"] == 1

    # Still inside the cooldown: cheap, no re-spawn, honest.
    assert tb.ensure_edge_tts_server() is False
    assert calls["spawn"] == 1, "cooldown was ignored"

    # After the cooldown it must genuinely retry and can succeed.
    monkeypatch.setattr(tb, "FAILURE_COOLDOWN", 0.0)
    monkeypatch.setattr(tb, "_retry_after", 0.0)
    seq = calls
    monkeypatch.setattr(tb, "probe", lambda *a, **k: True)
    assert tb.ensure_edge_tts_server() is True, "failure never recovered"
    assert tb.last_failure() == "", "stale failure reason kept after recovery"


def test_missing_project_is_reported_not_silently_swallowed(monkeypatch):
    calls = _patch(monkeypatch, probe_seq=[False], project=None)
    assert tb.ensure_edge_tts_server() is False
    assert "FRIDAY_TTS_SERVER_DIR" in tb.last_failure()
    assert calls["spawn"] == 0, "spawned without a project"


def test_wrong_interpreter_is_reported(monkeypatch):
    _patch(monkeypatch, probe_seq=[False], python=None)
    assert tb.ensure_edge_tts_server() is False
    assert "FRIDAY_TTS_PYTHON" in tb.last_failure()


def test_nonexistent_interpreter_is_reported(monkeypatch):
    _patch(monkeypatch, probe_seq=[False], python=r"C:\nope\python.exe")
    assert tb.ensure_edge_tts_server() is False
    assert "does not exist" in tb.last_failure()


def test_port_owned_by_something_else_does_not_attempt_to_spawn(monkeypatch):
    """Port ownership: something holds 5050 but is not our TTS server."""
    calls = _patch(monkeypatch, probe_seq=[False], port_open=True)
    assert tb.ensure_edge_tts_server() is False
    assert "held by another process" in tb.last_failure()
    assert calls["spawn"] == 0, "raced a foreign process for the port"


# --------------------------------------------------------------------------
# stale / dead process and startup timeout
# --------------------------------------------------------------------------

def test_dead_child_is_detected_and_respawned(monkeypatch):
    # First spawn dies, second one lives - a single fake would stay dead.
    children = [_FakeChild(exit_code=1), _FakeChild()]
    calls = _patch(monkeypatch, probe_seq=[False, False, True],
                   spawn=lambda: children.pop(0) if children else _FakeChild())

    assert tb.ensure_edge_tts_server() is False
    assert "exited" in tb.last_failure()

    # Next attempt: the dead child must not be trusted. Clear the cooldown the
    # first failure armed (it was 30s), otherwise the fast path short-circuits
    # before the dead-child check ever runs.
    monkeypatch.setattr(tb, "FAILURE_COOLDOWN", 0.0)
    monkeypatch.setattr(tb, "_retry_after", 0.0)
    assert tb.ensure_edge_tts_server() is True
    assert calls["spawn"] == 2, "a dead child was never respawned"


def test_child_that_dies_during_startup_reports_its_exit_code(monkeypatch):
    _patch(monkeypatch, probe_seq=[False],
           spawn=_FakeChild(exit_code=3))
    assert tb.ensure_edge_tts_server(timeout=5) is False
    assert "code 3" in tb.last_failure()


def test_startup_timeout_is_bounded(monkeypatch):
    """A server that never answers must give up inside the timeout."""
    _patch(monkeypatch, probe_seq=[False], spawn=_FakeChild())
    started = time.monotonic()
    assert tb.ensure_edge_tts_server(timeout=1.0) is False
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, "startup wait was not bounded (%.1fs)" % elapsed
    assert "did not answer" in tb.last_failure()


# --------------------------------------------------------------------------
# simultaneous requests
# --------------------------------------------------------------------------

def test_concurrent_requests_share_a_single_spawn(monkeypatch):
    """N simultaneous fallbacks must produce ONE server, not N children."""
    release = threading.Event()
    spawn_calls = []

    def slow_probe(base_url=None, timeout=3.0):
        return release.is_set()

    def slow_spawn(project_dir, python_exe, base_url=None):
        spawn_calls.append(project_dir)
        time.sleep(0.30)                 # hold the spawn lock while starting
        release.set()
        return _FakeChild()

    monkeypatch.setattr(tb, "probe", slow_probe)
    monkeypatch.setattr(tb, "_spawn_server", slow_spawn)
    monkeypatch.setattr(tb, "find_edge_tts_project", lambda: _stub_project())
    monkeypatch.setattr(tb, "find_tts_python", lambda: _stub_python())
    monkeypatch.setattr(tb, "_port_open", lambda *a, **k: False)

    results = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait(timeout=10)
        results.append(tb.ensure_edge_tts_server(timeout=5))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 6, "some callers never returned"
    assert all(results), "a concurrent caller was told the server is down"
    assert len(spawn_calls) == 1, "expected 1 spawn, got %d" % len(spawn_calls)


def test_reads_never_block_behind_a_spawn(monkeypatch):
    """last_failure()/is_ready() must not queue behind a slow spawn."""
    release = threading.Event()

    def slow_spawn(project_dir, python_exe, base_url=None):
        release.wait(timeout=5)
        return _FakeChild()

    monkeypatch.setattr(tb, "probe", lambda *a, **k: False)
    monkeypatch.setattr(tb, "_spawn_server", slow_spawn)
    monkeypatch.setattr(tb, "find_edge_tts_project", lambda: _stub_project())
    monkeypatch.setattr(tb, "find_tts_python", lambda: _stub_python())
    monkeypatch.setattr(tb, "_port_open", lambda *a, **k: False)

    spawner = threading.Thread(target=tb.ensure_edge_tts_server, kwargs={"timeout": 5})
    spawner.start()
    time.sleep(0.15)                     # ensure the spawn lock is held

    started = time.monotonic()
    tb.last_failure()
    tb.is_ready()
    elapsed = time.monotonic() - started
    release.set()
    spawner.join(timeout=10)

    assert elapsed < 0.5, "diagnostic reads blocked behind a spawn (%.2fs)" % elapsed


# --------------------------------------------------------------------------
# degradation: healthy probe, broken synthesis
# --------------------------------------------------------------------------

def test_mark_degraded_clears_readiness_and_sets_a_cooldown(monkeypatch):
    # patch_degraded=False: this test asserts on the REAL mark_degraded.
    calls = _patch(monkeypatch, probe_seq=[True], spawn=_FakeChild(),
                   patch_degraded=False)
    assert tb.ensure_edge_tts_server() is True

    tb.mark_degraded("local tts http 500")

    assert tb.last_failure() == "local tts http 500"
    assert tb.ensure_edge_tts_server() is False, "degraded server still claimed good"
    assert calls["probe"] == 1, "cooldown failed to suppress a re-probe"


def test_a_500_from_the_local_server_marks_it_degraded(monkeypatch):
    """`/v1/models` 200 next to `/v1/audio/speech` 500 must not stay 'ready'."""
    class Local:
        status_code = 500
        content = b""
        text = ""

    monkeypatch.setattr(gemini_tts.requests, "post",
                        lambda *a, **k: Local())
    monkeypatch.setattr(tb, "probe", lambda *a, **k: True)
    degraded = []
    monkeypatch.setattr(tb, "mark_degraded",
                        lambda reason, cooldown=None: degraded.append(str(reason)))

    ok, pcm, _q, error = gemini_tts._local_pcm("hello", allow_start=False)

    assert ok is False
    assert error == "local tts http 500", "failure was not surfaced"
    assert degraded, "a 500 did not invalidate readiness"
    assert "500" in degraded[0]


# --------------------------------------------------------------------------
# no silent failure anywhere
# --------------------------------------------------------------------------

def test_every_failure_path_returns_a_reason(monkeypatch):
    """A failure must never be an empty string - that is what hides bugs."""
    cases = [
        dict(spawn=RuntimeError("boom"), project="P", python="py", port_open=False),
        dict(spawn=_FakeChild(exit_code=9), project="P", python="py", port_open=False),
        dict(spawn=None, project=None, python="py", port_open=False),
        dict(spawn=None, project="P", python=None, port_open=False),
        dict(spawn=None, project="P", python="py", port_open=True),
    ]
    for index, kwargs in enumerate(cases):
        tb._reset_state()
        _patch(monkeypatch, probe_seq=[False], **kwargs)
        assert tb.ensure_edge_tts_server(timeout=1) is False, "case %d" % index
        reason = tb.last_failure()
        assert reason and reason.strip(), "case %d failed silently" % index


def test_local_pcm_never_returns_an_empty_error(monkeypatch):
    """_local_pcm must always explain itself on failure."""
    monkeypatch.setattr(tb, "probe", lambda *a, **k: False)

    class Local:
        status_code = 503
        content = b""
        text = ""

    monkeypatch.setattr(gemini_tts.requests, "post", lambda *a, **k: Local())
    ok, pcm, _q, error = gemini_tts._local_pcm("hi", allow_start=False)
    assert ok is False and pcm is None
    assert error and error.strip()


def test_sapi_pcm_returns_a_reason_on_failure(monkeypatch):
    import win32com.client

    def boom(*a, **k):
        raise RuntimeError("no tts engine")

    monkeypatch.setattr(win32com.client, "Dispatch", boom)
    ok, pcm, _q, error = gemini_tts._sapi_pcm("hello")
    assert ok is False and pcm is None
    assert error and "sapi tts failed" in error


# --------------------------------------------------------------------------
# the stage that makes silence impossible
# --------------------------------------------------------------------------

def test_offline_voice_serves_real_audio():
    """End-to-end through the REAL offline engine - no mocks.

    This is the stage that satisfies Sir's directive: it needs no network, no
    key and no quota, so it works when Gemini is exhausted.
    """
    ok, pcm, _q, error = gemini_tts._sapi_pcm(
        "Good evening, Sir. This is the offline fallback voice."
    )
    assert ok, "offline voice failed: %s" % error
    assert pcm and len(pcm) > 8000, "offline audio is suspiciously short"
    # 24 kHz mono 16-bit: >8000 bytes is at least ~0.17s of speech.
    duration = len(pcm) / 48000.0
    assert duration > 0.5, "offline audio too short to be a real utterance"


def test_speak_records_the_offline_stage(monkeypatch):
    _tts_pools = gemini_tts.api_pools.Pool("gemini_tts", ["k"], min_interval=0.0)
    monkeypatch.setattr(gemini_tts.api_pools, "_pools",
                        {"gemini_tts": _tts_pools})

    class Dead:
        status_code = 429
        content = b""
        text = ""

        def json(self):
            return {"error": {"code": 429, "message": "quota"}}

    monkeypatch.setattr(gemini_tts.requests, "post", lambda *a, **k: Dead())
    monkeypatch.setattr(gemini_tts, "_local_pcm",
                        lambda *a, **k: (False, None, "", "local tts http 500"))
    monkeypatch.setattr(gemini_tts, "_sapi_pcm",
                        lambda text: (True, b"X" * 4000, "", ""))

    voice = gemini_tts.CloudFirstTTSVoice()
    pcm = voice.speak("Fallback please.")
    assert pcm == b"X" * 4000
    assert voice.LAST_STAGE == "sapi"
