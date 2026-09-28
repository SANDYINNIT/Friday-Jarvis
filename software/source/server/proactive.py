"""Proactive 'Welcome back' diagnostics.

Tracks how long the machine has been truly idle (no keyboard/mouse input).
When the user returns after a long absence (default 60 minutes), FRIDAY runs a
quick read-only diagnostic scan and speaks a short greeting unless it is
speaking, busy, or on a social call.  Read-only; never steals the mic.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

from .file_logger import friday_logger, log_file_path


def _env_flag(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip() not in ("0", "false", "off", "no")


def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _idle_seconds():
    try:
        user32 = ctypes.windll.user32
        last = ctypes.c_ulong()
        user32.GetLastInputInfo(ctypes.byref(last))
        ticks = user32.GetTickCount()
        return (ticks - last.value) / 1000.0
    except Exception:
        return 0.0


def _top_memory_processes(limit=3):
    if psutil is None:
        return []
    try:
        procs = [
            (p.info["name"], p.info["memory_percent"])
            for p in psutil.process_iter(attrs=["name", "memory_percent"])
            if p.info.get("name")
        ]
        procs.sort(key=lambda item: item[1], reverse=True)
        return [(name, percent) for name, percent in procs[:limit] if percent]
    except Exception:
        return []


def _recent_error_log(max_lines=4):
    path = log_file_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            tail = handle.readlines()[-120:]
    except OSError:
        return []
    hits = [line for line in tail if "ERROR" in line or "WARNING" in line or "watchdog" in line.lower()]
    return hits[-max_lines:]


def _uncommitted_git(software_root):
    root = str(software_root)
    try:
        result = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True, text=True, timeout=6, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        changed = [line for line in result.stdout.splitlines() if line.strip()]
        return changed[:5]
    except Exception:
        return []


def run_default_diagnostics(flog, software_root):
    """Return a short spoken summary of the current machine health."""
    memory_top = _top_memory_processes(3)
    ram_line = ", ".join(f"{name} at {percent:.0f}%" for name, percent in memory_top) or "no heavy processes"
    errors = _recent_error_log()
    error_note = f", and I counted {len(errors)} recent warnings in my log" if errors else ""
    git = _uncommitted_git(software_root)
    git_note = f" Also, {len(git)} file{'s' if len(git) != 1 else ''} in the software tree are uncommitted." if git else ""
    return f"Welcome back, Sir. I scanned things while you were away — {ram_line} are using the most memory, system load feels normal{error_note}.{git_note}"


class WelcomeBackMonitor:
    """Speak a diagnostic greeting when the user returns after long idle time."""

    def __init__(self, speak, *, db_path=None, software_root=None, flog=None):
        self.speak = speak
        self.program_root = software_root
        self.flog = flog or friday_logger()
        self._stop = threading.Event()
        self._thread = None
        self._had_long_idle = False
        self._idle_minutes = _env_int("FRIDAY_WELCOME_IDLE_MIN", 60)

    def _run(self):
        if not _env_flag("FRIDAY_WELCOME_BACK"):
            return
        if self._idle_minutes <= 0:
            return
        threshold = self._idle_minutes * 60
        while not self._stop.wait(20.0):
            try:
                idle = _idle_seconds()
                if idle >= threshold:
                    self._had_long_idle = True
                elif self._had_long_idle and idle < 5:
                    self._had_long_idle = False
                    self._greet()
            except Exception:
                continue

    def _greet(self):
        try:
            summary = run_default_diagnostics(self.flog, self.program_root)
            self.speak(summary)
        except Exception as error:
            self.flog.warning("proactive diagnostics failed: %s", error)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="friday-welcome-back", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)