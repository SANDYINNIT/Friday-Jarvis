"""Optional, bounded Windows UI Automation for deterministic FRIDAY tools.

The module is deliberately not imported by the voice path. ``pywinauto`` is
optional; callers receive a structured unavailable result when UIA cannot be
loaded. Actions accept only tokens returned by a fresh ``dom``/``find`` call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

MAX_WINDOWS = 100
MAX_CONTROLS = 200
MAX_TEXT = 500
_ACTIONS = frozenset({"click", "invoke", "type", "set_value"})


def risk_metadata(action=None):
    """Return risk facts for an action; unknown actions require confirmation."""
    known = action in _ACTIONS
    return {"read_only": not known, "reversible": known, "external": known,
            "destructive": False, "confirmation_required": not known}


def _text(value, limit=MAX_TEXT):
    return str(value or "")[:limit]


def _get(obj, name, default=None):
    try:
        value = getattr(obj, name)
        return value() if callable(value) else value
    except Exception:
        return default


def _element_info(control):
    return _get(control, "element_info", None)


def _field(control, name, default=""):
    info = _element_info(control)
    value = _get(info, name, None) if info is not None else None
    if value is None:
        value = _get(control, name, default)
    return _text(value)


def _runtime_id(control):
    value = _get(_element_info(control), "runtime_id", None)
    return repr(value)[:MAX_TEXT] if value is not None else ""


def _control_snapshot(control, index):
    handle = _get(control, "handle", None)
    try:
        handle = int(handle) if handle else None
    except (TypeError, ValueError):
        handle = None
    return {"index": index, "name": _field(control, "name", _get(control, "window_text", "")),
            "title": _text(_get(control, "window_text", "")),
            "control_type": _field(control, "control_type"), "automation_id": _field(control, "automation_id"),
            "handle": handle, "runtime_id": _runtime_id(control), "enabled": _get(control, "is_enabled", None),
            "visible": _get(control, "is_visible", None), "value": _text(_get(control, "value", ""))}


def _identity(item):
    return tuple(item.get(key) for key in ("handle", "runtime_id", "name", "control_type", "automation_id"))


@dataclass
class _TokenRecord:
    snapshot_id: str
    control: object
    before: dict


class UIAutomation:
    """UIA adapter with an injectable provider for headless tests."""

    def __init__(self, provider=None, *, window_limit=MAX_WINDOWS, control_limit=MAX_CONTROLS):
        self.provider = provider
        self.window_limit = max(0, min(MAX_WINDOWS, int(window_limit)))
        self.control_limit = max(0, min(MAX_CONTROLS, int(control_limit)))
        self._tokens = {}

    def _backend(self):
        if self.provider is not None:
            return self.provider
        try:
            from pywinauto import Desktop
        except (ImportError, ModuleNotFoundError):
            return None
        try:
            return Desktop(backend="uia")
        except Exception:
            return None

    def _windows(self):
        backend = self._backend()
        if backend is None:
            return None
        try:
            value = backend.windows(top_level_only=True)
        except TypeError:
            value = backend.windows()
        except Exception:
            return []
        return list(value)[:self.window_limit]

    def _unavailable(self, operation):
        return {"available": False, "operation": operation, "failure_reason": "pywinauto UIA backend unavailable"}

    def list_windows(self):
        windows = self._windows()
        if windows is None:
            return self._unavailable("list_windows")
        return {"available": True, "windows": [self._window_record(window) for window in windows]}

    def _window_record(self, window):
        return {"handle": _get(window, "handle", None),
                "title": _text(_get(window, "window_text", _get(window, "name", ""))),
                "process_id": _get(window, "process_id", None)}

    def _window_matches(self, window, target):
        if target is None:
            return True
        if isinstance(target, int):
            return _get(window, "handle", None) == target
        if not isinstance(target, dict):
            target = {"title": target}
        actual = self._window_record(window)
        actual.update({"name": _field(window, "name", actual["title"]),
                       "control_type": _field(window, "control_type"),
                       "automation_id": _field(window, "automation_id")})
        return all(key not in target or _text(target[key]) == _text(actual.get(key, ""))
                   for key in ("title", "name", "control_type", "automation_id"))

    def _resolve_window(self, target):
        windows = self._windows()
        if windows is None:
            return None, "pywinauto UIA backend unavailable", []
        matches = [window for window in windows if self._window_matches(window, target)]
        if len(matches) != 1:
            return None, "window target is ambiguous or not found", [self._window_record(window) for window in matches]
        return matches[0], None, []

    def _controls(self, window):
        try:
            controls = list(window.descendants())
        except Exception:
            controls = []
        return controls[:self.control_limit]

    def _matches(self, item, criteria):
        return all(_text(item.get(key, "")) == _text(value) for key, value in criteria.items() if value is not None)

    def dom(self, target=None, **criteria):
        window, failure, candidates = self._resolve_window(target)
        if failure:
            if failure.endswith("unavailable"):
                return self._unavailable("dom")
            return {"available": True, "snapshot_id": None, "controls": [], "failure_reason": failure, "candidates": candidates}
        controls = self._controls(window)
        snapshot_id = hashlib.sha256(f"{id(window)}:{id(controls)}".encode()).hexdigest()[:16]
        records = []
        self._tokens = {}
        for index, control in enumerate(controls):
            record = _control_snapshot(control, index)
            if not self._matches(record, criteria):
                continue
            token = hashlib.sha256(json.dumps([snapshot_id, index, _identity(record)], sort_keys=True).encode()).hexdigest()[:24]
            record["target_token"] = token
            records.append(record)
            self._tokens[token] = _TokenRecord(snapshot_id, control, record.copy())
        return {"available": True, "snapshot_id": snapshot_id, "window": self._window_record(window), "controls": records}

    def find_controls(self, target=None, *, title=None, control_type=None, automation_id=None, name=None):
        criteria = {"title": title, "control_type": control_type, "automation_id": automation_id, "name": name}
        return self.dom(target, **{key: value for key, value in criteria.items() if value is not None})

    def perform(self, action, token, value=None, *, confirmation=False):
        risk = risk_metadata(action)
        result = {"action": action, "success": False, "changed": False, "verified": False, "risk": risk}
        if action not in _ACTIONS:
            result["failure_reason"] = "unsupported action; confirmation required"
            return result
        if not isinstance(token, str) or token not in self._tokens:
            result["failure_reason"] = "target token is stale or was not obtained from a fresh DOM snapshot"
            return result
        record = self._tokens[token]
        before = _control_snapshot(record.control, record.before["index"])
        if _identity(before) != _identity(record.before):
            result["failure_reason"] = "target token is stale"
            return result
        current_matches = []
        for window in self._windows() or []:
            for control in self._controls(window):
                if _identity(_control_snapshot(control, 0)) == _identity(record.before):
                    current_matches.append(control)
        if len(current_matches) != 1 or current_matches[0] is not record.control:
            result["failure_reason"] = "target token is stale or ambiguous"
            return result
        result["before"] = before
        try:
            if action == "click":
                record.control.click_input()
            elif action == "invoke":
                record.control.invoke()
            elif action == "type":
                record.control.type_keys(_text(value))
            else:
                setter = getattr(record.control, "set_edit_text", None) or getattr(record.control, "set_value")
                setter(_text(value))
        except Exception as error:
            result["failure_reason"] = str(error) or error.__class__.__name__
            result["after"] = _control_snapshot(record.control, record.before["index"])
            return result
        after = _control_snapshot(record.control, record.before["index"])
        result.update(after=after, changed=after != before, verified=after != before)
        if not result["changed"]:
            result["failure_reason"] = "action produced no observable state change"
        else:
            result["success"] = True
        self._tokens.pop(token, None)
        return result


_DEFAULT = UIAutomation()
list_windows = _DEFAULT.list_windows
dom = _DEFAULT.dom
find_controls = _DEFAULT.find_controls
perform = _DEFAULT.perform
