"""Ensure the external Edge-TTS server (localhost:5050) is running.

FRIDAY speaks through an OpenAI-compatible edge-tts server that lives in a
separate project (travisvn/openai-edge-tts, Python 3.12).  This module probes
the port, finds that project + a suitable interpreter, spawns the server when
needed, and waits until it answers.  It is idempotent so the desktop launcher
and the CLI warmup can both call it safely.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import requests


DEFAULT_BASE_URL = "http://localhost:5050/v1"
_KNOWN_PY312_PATHS = (
    r"C:\Users\<USER-PC>\AppData\Local\Programs\Python\Python312\python.exe",
)
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


def _spawn_server(project_dir, python_exe):
    creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    return subprocess.Popen(
        [python_exe, "app/server.py"],
        cwd=str(project_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def ensure_edge_tts_server(base_url=DEFAULT_BASE_URL, timeout=45.0):
    """Idempotently start the TTS server and wait until it responds."""
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
            _spawn_server(project_dir, python_exe)
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