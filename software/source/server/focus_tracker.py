"""Foreground window / focus tracker.

Tracks the active foreground window (title + process) with raw Win32 APIs so
no extra dependencies are needed, logs when FRIDAY notices a code editor open,
and serves the current focus to the UI and to future context logic.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from datetime import datetime

from .file_logger import friday_logger


_EDITOR_EXES = {
    "code.exe", "code-insiders.exe", "codium.exe", "notepad++.exe", "pycharm64.exe",
    "pycharm.exe", "idea64.exe", "sublime_text.exe", "atom.exe", "textpad.exe",
    "wordpad.exe", "notepad.exe", "obsidian.exe", "vim.exe", "nvim.exe",
}


def _env_flag(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip() not in ("0", "false", "off", "no")


def foreground_info():
    """Return (title, exe) of the current foreground window or ("", "")."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    title = buffer.value
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    try:
        import psutil

        process = psutil.Process(pid.value)
        exe = process.name().lower()
    except Exception:
        exe = ""
    return title or "", exe


def _is_editor(exe):
    return exe in _EDITOR_EXES or ("code" in exe and exe.endswith(".exe"))


class FocusTracker:
    """Poll the foreground window and expose context to the shared state."""

    def __init__(self, server_state, *, poll_seconds=2.0, notes_preview_callable=None):
        self.server_state = server_state
        self.poll_seconds = max(0.5, min(float(poll_seconds), 30.0))
        self.notes_preview = notes_preview_callable
        self._stop = threading.Event()
        self._thread = None
        self._last_editor_pair = ("", "")

    def _run(self):
        if not _env_flag("FRIDAY_FOCUS_TRACKER"):
            return
        flog = friday_logger()
        while not self._stop.wait(self.poll_seconds):
            try:
                title, exe = foreground_info()
                self.server_state["focus"] = {
                    "title": title[:160],
                    "exe": exe,
                    "since": datetime.now().strftime("%H:%M:%S"),
                    "editor": _is_editor(exe),
                }
                pair = (exe, title)
                if _is_editor(exe) and pair != self._last_editor_pair:
                    self._last_editor_pair = pair
                    flog.info("focus: editor open exe=%s title=%s", exe, _redact(title[:80]))
                    if self.notes_preview is not None:
                        try:
                            self.notes_preview()
                        except Exception:
                            pass
            except Exception:
                continue

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="friday-focus-tracker", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)


def _redact(value):
    return value.replace("\r", " ").replace("\n", " ")