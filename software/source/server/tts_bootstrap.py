"""Ensure the external Edge-TTS server (localhost:5050) is running.

FRIDAY speaks through an OpenAI-compatible edge-tts server that lives in a
separate project (travisvn/openai-edge-tts, Python 3.12).  This module probes
the port, finds that project + a suitable interpreter, spawns the server when
needed, and waits until it answers.  It is idempotent so the desktop launcher
and the CLI warmup can both call it safely.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


# 127.0.0.1, never `localhost`: this machine's ::1 blackholes SYNs, so a
# `localhost` URL burns requests' connect timeout (measured 2.047s vs 0.000s)
# before IPv4 fallback - on the local speech path that is 2s per utterance.
DEFAULT_BASE_URL = "http://127.0.0.1:5050/v1"
# How long to wait for a freshly spawned local TTS server before giving up.
# Was hard-coded at 45s, which stalled the voice for ~a minute whenever the
# server was not already running (measured 62s between tts_start and first
# audio). Overridable with FRIDAY_TTS_STARTUP_WAIT.
MAX_STARTUP_WAIT_SECONDS = float(os.environ.get("FRIDAY_TTS_STARTUP_WAIT", "12"))


def _known_py312_paths():
    """Candidate Python 3.12 interpreters, built from portable locations.

    This used to hard-code one developer's absolute path, which leaked their
    Windows username and machine name into a public repository. It is now
    derived from %LOCALAPPDATA% / %ProgramFiles% / the `py` launcher, so it
    works on any machine and ships no personal data.
    """
    import glob

    candidates = []
    local = os.environ.get("LOCALAPPDATA") or ""
    program_files = os.environ.get("ProgramFiles") or ""
    for root in (local, program_files):
        if not root:
            continue
        pattern = os.path.join(root, "Programs", "Python", "Python*", "python.exe")
        candidates.extend(glob.glob(pattern))

    def _version(path):
        # "Python312" -> 312, so a plain reverse sort puts 3.13 before 3.12.
        import re as _re

        match = _re.search(r"Python(\d+)", path)
        return int(match.group(1)) if match else 0

    # The edge-tts project needs 3.12; prefer exactly that, then the highest
    # other version. A lexicographic sort once picked Python37 here.
    def _rank(path):
        return (1 if _version(path) == 312 else 0, _version(path))

    candidates.sort(key=_rank, reverse=True)
    # `py -3.12` launcher, resolved without spawning a process.
    for launcher in (r"C:\Windows\py.exe", r"C:\Windows\pyw.exe"):
        if os.path.isfile(launcher):
            try:
                out = subprocess.run(
                    [launcher, "-3.12", "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True, errors="replace", timeout=10,
                )
                resolved = (out.stdout or "").strip()
                if resolved and os.path.isfile(resolved):
                    candidates.append(resolved)
            except Exception:
                pass
            break
    # Deduplicate, keep order.
    seen, unique = set(), []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return tuple(unique)


_KNOWN_PY312_PATHS = _known_py312_paths()
_lock = threading.Lock()
# Guards the spawn section so N concurrent fallbacks produce ONE server rather
# than N racing children all fighting for the port. Kept separate from `_lock`
# so diagnostic reads (last_failure / is_ready) never block behind a spawn.
_spawn_lock = threading.Lock()
# Readiness/failure state. This used to be a single `_started` bool that was set
# True on EVERY path, including the failures - so one transient error (project
# not found, spawn raised, startup timed out) permanently disabled the local
# voice for the rest of the session. State below separates "known good" from
# "known bad, retry after", so a failure is recoverable and observable.
_ready_at = 0.0          # monotonic time of the last successful probe
_failure = ""            # human-readable reason for the last failure
_retry_after = 0.0       # monotonic; do not attempt again before this
_child = None            # the Popen we spawned, for dead-process detection

# A successful probe is trusted for this long, so a steady-state speak() costs
# one monotonic comparison instead of an HTTP round trip per sentence.
READY_TTL = float(os.environ.get("FRIDAY_TTS_READY_TTL", "60"))
# After a failure, do not re-probe/re-spawn more often than this. Bounds the
# cost of a server that is genuinely broken (an edge-tts 403, a missing
# interpreter) while still allowing recovery later.
FAILURE_COOLDOWN = float(os.environ.get("FRIDAY_TTS_FAIL_COOLDOWN", "30"))


def last_failure():
    """Why the local server is currently unavailable ("" when healthy)."""
    with _lock:
        return _failure


def is_ready(base_url=DEFAULT_BASE_URL):
    """True when the server is believed good: freshly probed, or cached-good."""
    with _lock:
        if _failure and time.monotonic() < _retry_after:
            return False
        if time.monotonic() - _ready_at < READY_TTL:
            return True
    return probe(base_url)


def mark_degraded(reason, cooldown=None):
    """Record that a running server is answering but cannot actually speak.

    `/v1/models` can return 200 while `/v1/audio/speech` 500s (observed: the
    server was healthy on the probe endpoint and failed every synthesis with
    an upstream 403). Without this, readiness would stay cached-good and every
    sentence would pay for a round trip that is known to fail.
    """
    global _failure, _retry_after, _ready_at
    with _lock:
        _failure = str(reason or "local tts degraded")
        _ready_at = 0.0
        _retry_after = time.monotonic() + (
            FAILURE_COOLDOWN if cooldown is None else float(cooldown)
        )


def _reset_state():
    """Clear readiness/failure state. Test hook; also used on a hard restart."""
    global _ready_at, _failure, _retry_after, _child
    with _lock:
        _ready_at = 0.0
        _failure = ""
        _retry_after = 0.0
        _child = None


def probe(base_url=DEFAULT_BASE_URL, timeout=3.0):
    """Return True when the TTS server answers /v1/models."""
    try:
        return requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout).status_code == 200
    except Exception:
        return False


def find_edge_tts_project():
    """Locate the edge-tts server directory (env override, then drive scan)."""
    configured = os.environ.get("FRIDAY_TTS_SERVER_DIR", "").strip()
    if configured:
        candidate = Path(configured)
        if (candidate / "app" / "server.py").is_file():
            return candidate
    anchors = {Path(__file__).resolve().anchor}
    system_drive = os.environ.get("SystemDrive", "")
    if system_drive:
        anchors.add(Path(system_drive).anchor)
    for anchor in sorted(anchors, key=len, reverse=True):
        found = _scan_drive(anchor)
        if found is not None:
            return found
    return None


_OMIT_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "users", "perflogs",
}


def _scan_drive(anchor):
    """Two-level scan for a directory containing app/server.py and 'edge-tts'."""
    root = Path(anchor)
    if not root.is_dir():
        return None
    try:
        children = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return None
    for child in children:
        if child.name.lower() in _OMIT_DIRS:
            continue
        if _matches_server(child):
            return child
        try:
            for sub in child.iterdir():
                if sub.is_dir() and _matches_server(sub):
                    return sub
        except OSError:
            continue
    return None


def _matches_server(candidate):
    name = candidate.name.lower()
    if "edge-tts" not in name and "openai-edge" not in name:
        return False
    return (candidate / "app" / "server.py").is_file()


def find_tts_python():
    """Locate a Python 3.12 executable to run the edge-tts server."""
    configured = os.environ.get("FRIDAY_TTS_PYTHON", "").strip()
    if configured and Path(configured).is_file():
        return configured
    for candidate in _KNOWN_PY312_PATHS:
        if Path(candidate).is_file():
            return candidate
    from shutil import which

    return which("python")


def _spawn_server(project_dir, python_exe, base_url=None):
    """Start the local edge-tts server, pinned to its own port.

    TWO BUGS FIXED HERE (2026-09-30, found by inspecting a live orphan):

    1. PORT COLLISION. The project reads its listen port from the `PORT`
       environment variable (`PORT = int(os.getenv('PORT', 5050))` in
       app/server.py). FRIDAY's own launcher sets `PORT=10101` to tell
       FRIDAY which port to bind (start_friday.cmd: `set "PORT=%1"`), and a
       spawned child INHERITS that value. The edge-tts server therefore bound
       to 10101 - FRIDAY's own port - so FRIDAY could no longer start, and
       `/ping` answered 404 from the TTS server instead. The child now gets an
       explicit, sanitised PORT taken from the base_url we intend to use.

    2. ORPHAN SURVIVAL. `DETACHED_PROCESS` let the server outlive FRIDAY and
       keep holding the port after the user closed her. It is now a normal
       child (no DETACHED_PROCESS), and registered for cleanup via
       `atexit` so it is terminated when FRIDAY exits normally.
    """
    creationflags = subprocess.CREATE_NO_WINDOW
    env = dict(os.environ)
    # Pin the child to the port we actually probe/speak to, and never let it
    # inherit FRIDAY's own PORT (which means something entirely different).
    target = base_url or base_url_for_spawn()
    parsed = urlparse(target)
    if parsed.port:
        env["PORT"] = str(parsed.port)
        env["OPENAI_BASE_URL"] = f"http://127.0.0.1:{parsed.port}/v1"
    process = subprocess.Popen(
        [python_exe, "app/server.py"],
        cwd=str(project_dir),
        # Capture, do not discard. These used to be DEVNULL, which is exactly
        # why an upstream 403 in the server was invisible: the process started,
        # the probe answered 200, and the traceback went nowhere.
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=creationflags,
    )
    _register_child(process)
    _drain_to_log(process)
    return process


def _drain_to_log(process):
    """Copy the child's output into ~/.friday/logs/edge_tts_server.log.

    A daemon thread keeps draining so the pipe can never fill and block the
    server, and so a late traceback is still on disk when synthesis 500s.
    """

    def _pump():
        path = _server_log_path()
        sink = None
        try:
            if path:
                sink = open(path, "ab", buffering=0)
            for raw in iter(process.stdout.readline, b""):
                if sink is None:
                    continue
                try:
                    sink.write(b"[edge-tts] " + raw)
                except Exception:
                    return
        except Exception:
            pass
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass
            if sink is not None:
                try:
                    sink.close()
                except Exception:
                    pass

    threading.Thread(target=_pump, daemon=True).start()


_CHILDREN = []


def _register_child(process):
    """Track spawned servers so they are not left running after FRIDAY exits."""
    _CHILDREN.append(process)
    atexit.register(_terminate_children)


def _terminate_children():
    for process in list(_CHILDREN):
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            pass
    _CHILDREN.clear()


def base_url_for_spawn():
    """The base URL the TTS server should be reachable on."""
    return os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:5050/v1")


def _mark_ready():
    global _ready_at, _failure, _retry_after
    with _lock:
        _ready_at = time.monotonic()
        _failure = ""
        _retry_after = 0.0


def _note_failure(reason):
    """Record why the server is unavailable, print it, and set a retry window.

    This replaces the old `_started = True` on failure, which turned one
    transient error into a permanent mute for the rest of the session.
    """
    global _failure, _retry_after, _ready_at
    text = str(reason or "local tts unavailable")
    with _lock:
        _failure = text
        _ready_at = 0.0
        _retry_after = time.monotonic() + FAILURE_COOLDOWN
    # Observable without claiming success: FRIDAY's log says what happened.
    print(f"[local tts] {text}", flush=True)


def _port_open(port, host="127.0.0.1", timeout=0.5):
    """True when something is already listening on the port."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _server_log_path():
    root = os.environ.get("FRIDAY_STATE_DIR") or os.path.expanduser("~/.friday")
    log_dir = os.path.join(root, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, "edge_tts_server.log")
    except Exception:
        return None


def ensure_edge_tts_server(base_url=DEFAULT_BASE_URL, timeout=MAX_STARTUP_WAIT_SECONDS):
    """Idempotently ensure the local TTS server is up, within a bounded wait.

    Returns True when it can serve speech. Never raises.

    Contract, all new (the old version latched `_started = True` on failure):
      * bounded      - a single call waits at most `timeout` seconds
      * single-flight - concurrent callers share one spawn, not N children
      * recoverable  - a failure sets FAILURE_COOLDOWN, never a permanent latch
      * observable   - last_failure() and a `[local tts]` log line say why
      * honest       - a port owned by something else, a dead child, a missing
                       project/interpreter and a startup timeout each produce a
                       distinct reason instead of a silent False

    The spawned server is pinned to the port in `base_url` and is terminated
    when FRIDAY exits, so it can neither steal FRIDAY's own port nor linger as
    an orphan after she is closed.
    """
    global _child

    # FAST PATH - the per-sentence case. No lock contention, no I/O: a cached
    # good probe costs one monotonic comparison, so a healthy session never
    # pays a startup penalty per utterance.
    with _lock:
        good = _ready_at and time.monotonic() - _ready_at < READY_TTL
        cooling = _failure and time.monotonic() < _retry_after
    if good:
        return True
    if cooling:
        return False

    with _spawn_lock:
        # Re-check: another caller may have brought it up while we waited.
        with _lock:
            good = _ready_at and time.monotonic() - _ready_at < READY_TTL
            cooling = _failure and time.monotonic() < _retry_after
        if good:
            return True
        if cooling:
            return False

        # STALE/DEAD PROCESS: our own child exited - forget it so we respawn
        # instead of probing a port nothing owns any more.
        with _lock:
            child = _child
        if child is not None:
            code = child.poll()
            if code is not None:
                _note_failure(
                    f"local tts server exited (code {code}); respawning"
                )

        if probe(base_url):
            _mark_ready()
            return True

        parsed = urlparse(base_url)
        port = parsed.port or 5050
        # PORT OWNERSHIP: something is listening but does not answer our probe.
        # Spawning here would fail to bind, so say so instead of timing out.
        if _port_open(port):
            _note_failure(
                f"port {port} is held by another process that does not answer "
                f"{base_url}"
            )
            return False

        project_dir = find_edge_tts_project()
        python_exe = find_tts_python()
        # WRONG INTERPRETER / CWD / PROJECT: each is a distinct, actionable
        # reason rather than a bare False that looks like "TTS is broken".
        if project_dir is None:
            _note_failure(
                "edge-tts project not found (set FRIDAY_TTS_SERVER_DIR)"
            )
            return False
        if not (Path(project_dir) / "app" / "server.py").is_file():
            _note_failure(f"not an edge-tts project (no app/server.py): {project_dir}")
            return False
        if python_exe is None:
            _note_failure(
                "no interpreter for the edge-tts server (set FRIDAY_TTS_PYTHON)"
            )
            return False
        if not Path(python_exe).is_file():
            _note_failure(f"interpreter does not exist: {python_exe}")
            return False

        try:
            process = _spawn_server(project_dir, python_exe, base_url=base_url)
        except Exception as error:
            _note_failure(f"could not spawn the edge-tts server: {error}")
            return False
        with _lock:
            _child = process

        # STARTUP TIMEOUT, bounded. Poll the child as well as the port so a
        # server that dies on import reports its own exit instead of burning
        # the full timeout.
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                _note_failure(
                    f"local tts server exited during startup (code {code}); "
                    f"see edge_tts_server.log"
                )
                return False
            if probe(base_url, timeout=1.0):
                _mark_ready()
                return True
            time.sleep(0.3)

        _note_failure(f"local tts server did not answer within {timeout:.0f}s")
        return False