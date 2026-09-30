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


DEFAULT_BASE_URL = "http://localhost:5050/v1"
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
                    capture_output=True, text=True, timeout=10,
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
_started = False


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
        env["OPENAI_BASE_URL"] = f"http://localhost:{parsed.port}/v1"
    process = subprocess.Popen(
        [python_exe, "app/server.py"],
        cwd=str(project_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=creationflags,
    )
    _register_child(process)
    return process


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
    return os.environ.get("OPENAI_BASE_URL", "http://localhost:5050/v1")


def ensure_edge_tts_server(base_url=DEFAULT_BASE_URL, timeout=MAX_STARTUP_WAIT_SECONDS):
    """Idempotently start the TTS server and wait until it responds.

    The wait is bounded by MAX_STARTUP_WAIT_SECONDS (default 12s, overridable
    with FRIDAY_TTS_STARTUP_WAIT). It was 45s, which meant a local server that
    was never going to start silently held the voice for three quarters of a
    minute on the last-resort path. A slow local voice is better than no voice,
    but a 45s stall is not.

    The spawned server is pinned to the port in `base_url` and is terminated
    when FRIDAY exits, so it can neither steal FRIDAY's own port nor linger as
    an orphan after she is closed.
    """
    global _started
    with _lock:
        if _started:
            return probe(base_url)
        if probe(base_url):
            _started = True
            return True
        project_dir = find_edge_tts_project()
        python_exe = find_tts_python()
        if project_dir is None or python_exe is None:
            _started = True
            return False
        try:
            _spawn_server(project_dir, python_exe, base_url=base_url)
        except Exception:
            _started = True
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if probe(base_url):
                _started = True
                return True
            time.sleep(0.5)
        _started = True
        return probe(base_url)