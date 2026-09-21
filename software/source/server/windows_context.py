"""Read-only Windows foreground-window context for on-demand assistant use."""

import ctypes
import os

import psutil
import pywinctl


def get_active_window_context():
    """Return foreground-window metadata without changing desktop state."""
    if os.name != "nt":
        return {}

    window = pywinctl.getActiveWindow()
    if window is None:
        return {}

    handle = int(window.getHandle())
    process_id = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))

    process_name = ""
    executable = ""
    if process_id.value:
        try:
            process = psutil.Process(process_id.value)
            process_name = process.name()
            executable = process.exe()
        except (psutil.Error, OSError):
            pass

    return {
        "handle": handle,
        "title": str(window.title or "").strip(),
        "process": process_name,
        "executable": executable,
        "pid": process_id.value or None,
        "bounds": [window.left, window.top, window.width, window.height],
    }
