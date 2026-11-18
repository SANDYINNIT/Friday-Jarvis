"""Cheapest possible desktop awareness: window state, no pixels.

This is layer 1 of the screen-awareness stack and it is deliberately the most
boring layer, because it answers most of the questions people actually ask
without touching a single pixel:

    "OpenCode is minimized"   -> IsIconic() on the resolved HWND
    "what's in the way"       -> z-order + foreground + bounds
    "is Discord open"         -> window list, already maintained by windows_control

Measured for comparison: asking `ImageGrab` about the screen costs 51 ms per
monitor (and a REGION grab costs the same, because Pillow captures the whole
desktop then crops). Asking Windows about a window costs microseconds.

It reuses `windows_control` rather than re-implementing window lookup, so there
is still exactly one fuzzy matcher and one set of confirmation-gated mutators.

Nothing here takes an action. Restoring/focusing stays behind the existing
`windows_control` risk and confirmation path - see `ui_probe` for the UIA
action path, which follows the same rule.
"""

from __future__ import annotations

import os
import threading
import time

from .file_logger import friday_logger as _friday_logger
from . import windows_control as _wc

_log = _friday_logger()

# How often the cheap background poll runs. Event-driven WinEvent hooks would
# be better still, but pywin32's hook callbacks need a message pump on their own
# thread; a 2s poll of GetForegroundWindow + window state is a few microseconds
# and cannot wedge. The existing focus_tracker already polls at 2s, so this
# keeps the same cadence rather than adding a second one.
DEFAULT_POLL_SECONDS = float(os.getenv("FRIDAY_WINDOW_POLL", "2.0"))


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def is_minimized(query, *, limit=5):
    """Is the app matching `query` minimized right now?

    Returns (bool, snapshot_or_None, candidates). Never raises: a window that
    cannot be resolved reports False with no snapshot, which reads as "not
    minimized" and lets the caller move on to a more expensive check.
    """
    try:
        candidates = _wc.find_windows(query, limit=limit)
    except Exception as error:
        _log.warning("is_minimized lookup failed: %s", type(error).__name__)
        return False, None, []
    if not candidates:
        return False, None, []
    best = candidates[0]
    try:
        return bool(best.get("minimized")), best, candidates
    except Exception:
        return False, None, candidates


def window_state(query, *, limit=5):
    """A compact, honest description of one app's window - no pixels."""
    minimized, snapshot, candidates = is_minimized(query, limit=limit)
    if snapshot is None:
        return {"found": False, "query": query,
                "reason": "no open window matched that name"}
    bounds = snapshot.get("bounds") or []
    return {
        "found": True,
        "query": query,
        "title": snapshot.get("title") or "",
        "process": snapshot.get("process") or "",
        "minimized": minimized,
        "maximized": bool(snapshot.get("maximized")),
        "visible": bool(snapshot.get("visible")),
        "handle": snapshot.get("handle"),
        "bounds": list(bounds) if isinstance(bounds, list) else [],
        "ambiguous": len(candidates) > 1,
        "other_candidates": [c.get("title") or "" for c in candidates[1:4]],
    }


# Shell surfaces are not applications. Without this filter the "what is open"
# answer was dominated by three "DesktopWindowXamlSource" rows and a "Running
# applications" launcher, which is exactly the kind of noise that teaches the
# model to reply with a generic greeting instead of the real list.
_SHELL_TITLES = frozenset({
    "desktopwindowxamlsource", "program manager", "running applications",
    "taskbar", "windows input experience", "search", "start",
    "programmanager", "shell_dll_proxy",
    # Explorer child panes are not applications either - they were showing up
    # as "Namespace Tree Control" / "Navigation Pane" in the open-app list.
    "namespace tree control", "navigation pane",
})


def _is_shell_child(window, title):
    """Explorer/solver internals that are windows but not applications."""
    process = (window.get("process") or "").lower()
    if title.lower() in _SHELL_TITLES:
        return True
    if process == "explorer.exe" and " " not in title:
        # A bare folder name is the folder view, not a titled window; the real
        # Explorer window carries a "- File Explorer" suffix.
        return True
    return False


def desktop_overview(limit=8):
    """The open applications, most relevant first. Cheap, pixel-free."""
    try:
        windows = _wc.list_windows(limit=limit * 4)
    except Exception:
        return []
    out = []
    seen = set()
    for window in windows:
        title = (window.get("title") or "").strip()
        if not title:
            continue
        bounds = window.get("bounds") or []
        if isinstance(bounds, list) and len(bounds) >= 4 and bounds[2] < 120:
            continue  # shell placeholder, not a real window
        if _is_shell_child(window, title):
            continue
        key = title[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": title[:120],
            "process": window.get("process") or "",
            "minimized": bool(window.get("minimized")),
            "focused": bool(window.get("focused")),
        })
        if len(out) >= limit:
            break
    return out


def foreground():
    try:
        context = _wc.get_active_window()
    except Exception:
        return {}
    return context or {}


class WindowStateTracker:
    """Keeps a tiny rolling record of window/foreground changes.

    Used to answer "what changed on my screen?" without ever diffing pixels, and
    to know whether the cached frame is even worth re-reading. Cheap by design:
    one title/id comparison per poll.
    """

    def __init__(self, poll_seconds=DEFAULT_POLL_SECONDS):
        self.poll_seconds = _clamp(poll_seconds, 0.5, 30.0, DEFAULT_POLL_SECONDS)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.current = {}
        self.events = []          # newest last, capped
        self.last_poll_at = 0.0
        self.polls = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="friday-window-state", daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop.set()
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.wait(self.poll_seconds):
            self.poll_once()

    # -- polling -----------------------------------------------------------
    def poll_once(self):
        snapshot = self._observe()
        with self._lock:
            previous, self.current = self.current, snapshot
            self.last_poll_at = time.time()
            self.polls += 1
            changed = self._diff(previous, snapshot)
            if changed:
                self.events.extend(changed)
                self.events = self.events[-40:]
        return changed

    def _observe(self):
        info = {}
        try:
            front = foreground()
            if front:
                info["foreground"] = front.get("title") or ""
                info["foreground_process"] = front.get("process") or ""
        except Exception:
            pass
        try:
            for entry in desktop_overview(limit=10):
                key = (entry.get("title") or "")[:80]
                if key:
                    info.setdefault("windows", {})[key] = {
                        "minimized": entry.get("minimized"),
                        "process": entry.get("process"),
                    }
        except Exception:
            pass
        return info

    @staticmethod
    def _diff(previous, current):
        events = []
        old_fg = (previous or {}).get("foreground")
        new_fg = (current or {}).get("foreground")
        if new_fg and new_fg != old_fg:
            events.append({"at": time.time(), "kind": "focus_changed",
                           "detail": new_fg[:120]})
        old_windows = (previous or {}).get("windows") or {}
        new_windows = (current or {}).get("windows") or {}
        for title, meta in new_windows.items():
            before = old_windows.get(title)
            if before is None:
                events.append({"at": time.time(), "kind": "opened",
                               "detail": title})
            elif before.get("minimized") != meta.get("minimized"):
                kind = "minimized" if meta.get("minimized") else "restored"
                events.append({"at": time.time(), "kind": kind,
                               "detail": title})
        for title in old_windows:
            if title not in new_windows:
                events.append({"at": time.time(), "kind": "closed",
                               "detail": title})
        return events

    # -- reading -----------------------------------------------------------
    def snapshot(self):
        with self._lock:
            return {"current": dict(self.current),
                    "events": list(self.events[-10:]),
                    "polls": self.polls,
                    "age": (round(time.time() - self.last_poll_at, 2)
                            if self.last_poll_at else None),
                    "poll_seconds": self.poll_seconds}

    def recent_events(self, limit=5):
        with self._lock:
            return list(self.events[-limit:])


_TRACKER = None
_TRACKER_LOCK = threading.Lock()


def tracker():
    global _TRACKER
    with _TRACKER_LOCK:
        if _TRACKER is None:
            _TRACKER = WindowStateTracker()
        return _TRACKER


def start():
    return tracker().start()


def stop():
    return tracker().stop()


def snapshot():
    return tracker().snapshot()


def recent_events(limit=5):
    return tracker().recent_events(limit)