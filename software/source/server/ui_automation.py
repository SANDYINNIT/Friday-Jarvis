"""Optional, bounded Windows UI Automation for deterministic FRIDAY tools.

The module is deliberately not imported by the voice path. ``pywinauto`` is
optional; callers receive a structured unavailable result when UIA cannot be
loaded. Actions accept only tokens returned by a fresh ``dom``/``find`` call.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

MAX_WINDOWS = 100
MAX_CONTROLS = 200
MAX_TEXT = 500
# Characters returned by one read_text() call. Long app output is read in
# bounded chunks rather than by scrolling a viewport, so one request can never
# hand the model an unbounded string or force a scroll it did not need.
READ_CHUNK = 4000
READ_MAX_CONTROLS = 400
# Control types that accept or expose user text. `Document` is included because
# rich-text/editable documents report as Document in UIA, and because its
# TextPattern is often the only place a window's whole text is exposed.
EDITABLE_CONTROL_TYPES = ("Edit", "TextBox", "ComboBox", "Document", "Editable")
# ROUND Z (2026-10-04): "scroll" joins the known actions so it is classified as a
# reversible (not confirmation-required) operation by risk_metadata, exactly like
# click/invoke/type. It is still gated by ui_probe.act()'s signed-token consent
# path - it is NOT a new authorisation system.
_ACTIONS = frozenset({"click", "invoke", "type", "set_value", "scroll"})
_SCROLL_DIRECTIONS = ("up", "down", "left", "right")
_SCROLL_DEFAULT_AMOUNT = 3
_SCROLL_MAX_AMOUNT = 25


def _scroll_pattern(control):
    """Return the element's ScrollPattern interface, or None.

    ROUND Z, measured: pywinauto exposes patterns as a `lazy_property` that
    RAISES `NoPatternInterfaceError` when the pattern is absent. That exception
    derives from `Exception`, NOT `AttributeError`, so the usual
    `getattr(el, "iface_scroll", None)` idiom does NOT return None - it
    propagates. Probing it that way (and catching broadly) silently reports
    EVERY element as scroll-capable. This helper is the only correct probe.
    """
    try:
        from pywinauto.uia_defines import NoPatternInterfaceError
    except Exception:
        NoPatternInterfaceError = ()
    try:
        pattern = getattr(control, "iface_scroll", None)
    except NoPatternInterfaceError:
        return None
    except Exception:
        return None
    return pattern


def _scroll_state(control):
    """Read-only scroll facts about one control. Never raises.

    ROUND Z, MEASURED (2026-10-05) on the real OpenCode window: the UI
    Automation ScrollPattern properties are all `Current`-prefixed -
    `CurrentVerticallyScrollable`, `CurrentVerticalScrollPercent`,
    `CurrentVerticalViewSize` and their `Horizontal` twins. Three separate
    traps, all of which a hand-written fake silently hides:

    1. The unprefixed names do not exist. Reading `VerticallyScrollable`
       returns nothing, which reads as "not scrollable" and makes every
       container look inert.
    2. UIA booleans arrive through COM as VARIANT_BOOL ints, so a genuinely
       scrollable container hands back `1`, NOT `True`. `value is True` is
       False for it.
    3. A provider with no valid percentage reports `-1`, which is a real
       number and must not be compared as if it were a position.

    `Cached*` properties raise COMError on these controls; only `Current*` is
    used. "exposes the pattern" and "can actually scroll" stay separate facts,
    because an element can expose the pattern and still refuse to move.
    """
    out = {
        "exposes_scroll": False,
        "can_scroll_v": False,
        "can_scroll_h": False,
        "scroll_percent_v": None,
        "scroll_percent_h": None,
        "scroll_state": "none",
    }
    pattern = _scroll_pattern(control)
    if pattern is None:
        return out
    out["exposes_scroll"] = True

    def _read(name):
        try:
            return getattr(pattern, name)
        except AttributeError:
            # pywinauto raises this rather than returning False when the
            # provider reports NotScrollable / no percentage.
            return None
        except Exception:
            return None

    def _flag(value):
        """UIA boolean -> Python bool. COM hands back 0/1, not True/False."""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        return None

    def _percent(value):
        """UIA percentage -> float, or None when the provider has none."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if value < 0:
            return None  # UIA sentinel for "no valid percentage"
        return value

    vertically = _flag(_read("CurrentVerticallyScrollable"))
    horizontally = _flag(_read("CurrentHorizontallyScrollable"))
    out["can_scroll_v"] = bool(vertically)
    out["can_scroll_h"] = bool(horizontally)
    percent_v = _percent(_read("CurrentVerticalScrollPercent"))
    percent_h = _percent(_read("CurrentHorizontalScrollPercent"))
    out["scroll_percent_v"] = percent_v
    out["scroll_percent_h"] = percent_h
    if out["can_scroll_v"] or out["can_scroll_h"]:
        out["scroll_state"] = "scrollable"
    elif percent_v is not None or percent_h is not None:
        # Percentages are readable but nothing can move. Recording this
        # honestly is what stops a semantic-scroll path from claiming to work.
        out["scroll_state"] = "reported_but_not_scrollable"
    else:
        out["scroll_state"] = "exposed_not_scrollable"
    return out


def normalize_scroll(value):
    """Coerce a scroll request into (direction, amount). Bounded."""
    direction, amount = "up", _SCROLL_DEFAULT_AMOUNT
    if isinstance(value, dict):
        direction = str(value.get("direction") or direction).strip().lower()
        raw = value.get("amount", amount)
    elif isinstance(value, str):
        direction = value.strip().lower()
        raw = amount
    else:
        raw = value if isinstance(value, (int, float)) else amount
    if direction not in _SCROLL_DIRECTIONS:
        # Tolerate the natural-language spellings rather than refusing.
        if direction in ("top", "earlier", "back", "previous", "prev"):
            direction = "up"
        elif direction in ("bottom", "later", "forward", "next"):
            direction = "down"
        elif direction.startswith("up"):
            direction = "up"
        elif direction.startswith("down"):
            direction = "down"
        else:
            direction = "up"
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        amount = _SCROLL_DEFAULT_AMOUNT
    amount = max(1, min(_SCROLL_MAX_AMOUNT, amount))
    return direction, amount


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


def _window_digest(window, limit=MAX_CONTROLS):
    """A bounded fingerprint of a whole window's observable state.

    2026-10-02: `perform()` verified an action by comparing the TARGET control
    before and after. Measured on a real app: clicking the Calculator's "7"
    button changed the display from "muestra 0" to "muestra 7" - the action
    worked perfectly and `perform()` reported "no observable state change",
    because a button does not change itself when pressed. Almost every real
    action is like that.

    An action is proven by the WINDOW changing, not by the target. This digests
    the window's own value plus a bounded list of descendant names/values, so a
    genuine effect is detected while a no-op still fails honestly.
    """
    parts = [_field(window, "window_text", "") or ""]
    try:
        descendants = list(window.descendants())[:limit]
    except Exception:
        descendants = []
    for control in descendants:
        try:
            name = _field(control, "name", "") or ""
            value = _text(_get(control, "value", ""))
            if name or value:
                parts.append("%s=%s" % (name[:60], value[:120]))
        except Exception:
            continue
    return hashlib.sha256("|".join(parts).encode("utf-8", "replace")).hexdigest()[:32]


def _control_snapshot(control, index):
    handle = _get(control, "handle", None)
    try:
        handle = int(handle) if handle else None
    except (TypeError, ValueError):
        handle = None
    scroll = _scroll_state(control)
    rect = None
    try:
        r = control.rectangle()
        rect = [int(r.left), int(r.top), int(r.right), int(r.bottom)]
    except Exception:
        rect = None
    return {"index": index, "name": _field(control, "name", _get(control, "window_text", "")),
            "title": _text(_get(control, "window_text", "")),
            "control_type": _field(control, "control_type"), "automation_id": _field(control, "automation_id"),
            "handle": handle, "runtime_id": _runtime_id(control), "enabled": _get(control, "is_enabled", None),
            "visible": _get(control, "is_visible", None), "value": _text(_get(control, "value", "")),
            # ROUND Z: read-only scroll facts. Cheap (one COM property read each)
            # and they are what lets a caller discover a scrollable container at
            # all - which was impossible before, and is the reason FRIDAY had no
            # way to bring off-screen content into view.
            "can_scroll": bool(scroll["can_scroll_v"] or scroll["can_scroll_h"]),
            "can_scroll_v": bool(scroll["can_scroll_v"]),
            "can_scroll_h": bool(scroll["can_scroll_h"]),
            "scroll_percent_v": scroll["scroll_percent_v"],
            "scroll_percent_h": scroll["scroll_percent_h"],
            "scroll_state": scroll["scroll_state"],
            # ROUND Z: the bounding rect, so a caller can rank scroll
            # candidates by how much screen they cover and so the wheel
            # fallback can aim at the real viewport rather than a guess.
            "rect": rect}


def _identity(item):
    return tuple(item.get(key) for key in ("handle", "runtime_id", "name", "control_type", "automation_id"))


def _foreground_handle():
    """The HWND currently in the foreground, or None."""
    try:
        import ctypes
        return ctypes.windll.user32.GetForegroundWindow() or None
    except Exception:
        return None


def _focus_handle(hwnd):
    """Bring a window to the foreground. Returns True only if it really is."""
    if hwnd is None:
        return False
    if _foreground_handle() == hwnd:
        return True
    try:
        import win32gui
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, 9)     # SW_RESTORE
        except Exception:
            pass
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            # Windows foreground-lock routinely rejects this; fall back to the
            # helper FRIDAY already uses and then VERIFY either way.
            try:
                from . import windows_control as _wc
                _wc.focus_window(hwnd)
            except Exception:
                pass
    except Exception:
        pass
    # Verification, never assumption: Windows may still refuse.
    for _ in range(10):
        if _foreground_handle() == hwnd:
            return True
        time.sleep(0.05)
    return _foreground_handle() == hwnd


def _pattern_text(control):
    """TextPattern text for one control, or "" when it exposes no Text pattern.

    pywinauto raises NoPatternInterfaceError (not AttributeError) for controls
    without the pattern, and `DocumentRange.GetText(-1)` is what actually reads
    it - there is no `GetTextPatterns` method on the wrapper, and guessing that
    there was produced a false "cannot read this window" the first time.
    """
    try:
        pattern = control.iface_text
    except Exception:
        return ""
    if pattern is None:
        return ""
    try:
        return pattern.DocumentRange.GetText(-1) or ""
    except Exception:
        return ""


@dataclass
class _TokenRecord:
    snapshot_id: str
    control: object
    before: dict
    # 2026-10-02: the owning window handle. The re-verification used to walk
    # every window's descendants on the desktop, which took 23s on this
    # machine - unusable for an interactive action - even though a UIA element
    # only ever exists inside its own window.
    window_handle: object = None


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
        return self._snapshot(window, lambda record: self._matches(record, criteria))

    def _snapshot(self, window, predicate):
        """Enumerate a window's controls, minting one single-use token each.

        Shared by `dom` and `find_inputs` so token issuance - the stale-target
        protection - exists in exactly one place.
        """
        controls = self._controls(window)
        snapshot_id = hashlib.sha256(f"{id(window)}:{id(controls)}".encode()).hexdigest()[:16]
        records = []
        self._tokens = {}
        for index, control in enumerate(controls):
            record = _control_snapshot(control, index)
            if not predicate(record):
                continue
            token = hashlib.sha256(json.dumps([snapshot_id, index, _identity(record)], sort_keys=True).encode()).hexdigest()[:24]
            record["target_token"] = token
            records.append(record)
            self._tokens[token] = _TokenRecord(
                snapshot_id, control, record.copy(),
                window_handle=getattr(window, "handle", None),
            )
        return {"available": True, "snapshot_id": snapshot_id,
                "window": self._window_record(window), "controls": records,
                "captured_at": time.time()}

    def find_controls(self, target=None, *, title=None, control_type=None, automation_id=None, name=None):
        criteria = {"title": title, "control_type": control_type, "automation_id": automation_id, "name": name}
        return self.dom(target, **{key: value for key, value in criteria.items() if value is not None})

    def find_inputs(self, target=None, *, name=None):
        """Editable controls a caller could actually type into.

        ChatGPT reaches a GUI's text field by walking the accessibility tree
        for an Edit and focusing it by NAME. FRIDAY already mints tokens for
        every control but only exposed one-control_type-at-a-time search, so
        "which of these can I type into" was not answerable without guessing.
        The name filter is a substring match, because accessibility names are
        often a longer phrase ("Prompt", "Ask anything, / for commands...").
        """
        window, failure, candidates = self._resolve_window(target)
        if failure:
            if failure.endswith("unavailable"):
                return self._unavailable("find_inputs")
            return {"available": True, "snapshot_id": None, "controls": [],
                    "failure_reason": failure, "candidates": candidates}
        wanted = (name or "").strip().lower()

        def _editable(record):
            if record.get("control_type") not in EDITABLE_CONTROL_TYPES:
                return False
            if wanted and wanted not in (record.get("name") or "").lower():
                return False
            return True

        return self._snapshot(window, _editable)

    def read_text(self, target=None, *, offset=0, limit=READ_CHUNK):
        """Read a window's accessible text in bounded chunks, without scrolling.

        Why this exists: ChatGPT could read a long window by pulling its text
        from the accessibility tree. FRIDAY could not - `dom` only ever returned
        a per-control `value` truncated to MAX_TEXT, and every other route
        wanted a screenshot or a scroll. Reading the text first is strictly
        cheaper than scrolling and re-shooting, and it cannot guess at pixels.

        Mechanism order, reported honestly as `mechanism`:
          1. `uia_textpattern`  - a Document exposing TextPattern gives the
             window's whole accessible text in one call;
          2. `uia_text_nodes`   - else concatenate the Text/Edit nodes the tree
             exposes (Chromium-style apps publish one node per text run);
          3. `none`             - neither, so the caller must fall back to
             screenshot/OCR/vision. We say so rather than returning "" as if
             the window were genuinely empty.
        """
        window, failure, candidates = self._resolve_window(target)
        if failure:
            if failure.endswith("unavailable"):
                return self._unavailable("read_text")
            return {"available": True, "text": "", "total": 0, "offset": max(0, int(offset)),
                    "complete": True, "mechanism": "unavailable",
                    "failure_reason": failure, "candidates": candidates}

        controls = self._controls(window)
        doc_text = ""
        node_parts = []
        scanned = 0
        patterns = 0
        for control in controls[:READ_MAX_CONTROLS]:
            scanned += 1
            try:
                control_type = control.element_info.control_type
            except Exception:
                continue
            text = _pattern_text(control)
            if text:
                patterns += 1
            if control_type == "Document" and not doc_text and text.strip():
                doc_text = text
            elif control_type in ("Text", "Edit", "ListItem"):
                if not text:
                    try:
                        text = control.element_info.name or ""
                    except Exception:
                        text = ""
                # Zero-width/invisible runs are Chromium padding, not content.
                if text and text.replace("â€‹", "").strip():
                    node_parts.append(text)

        if doc_text.strip():
            full, mechanism = doc_text, "uia_textpattern"
        elif node_parts:
            full, mechanism = "\n".join(node_parts), "uia_text_nodes"
        else:
            full, mechanism = "", "none"

        offset = max(0, int(offset))
        limit = max(1, min(int(limit), READ_CHUNK))
        chunk = full[offset:offset + limit]
        total = len(full)
        return {
            "available": True,
            "window": self._window_record(window),
            "captured_at": time.time(),
            "text": chunk,
            "offset": offset,
            "length": len(chunk),
            "total": total,
            "complete": (offset + len(chunk)) >= total,
            "mechanism": mechanism,
            "controls_scanned": scanned,
            "controls_with_text": patterns,
            # Told to the caller so "no text" never looks like an empty window.
            "fallback": ("use screenshot/OCR/vision - this window exposes no "
                         "accessible text" if mechanism == "none" else ""),
        }

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
        # 2026-10-02: this compared the re-found control to the stored one
        # with Python `is`. pywinauto builds a FRESH COM wrapper on every
        # tree walk, so `is` was always False and perform() could NEVER
        # succeed against a real control: the stale-token protection was not
        # protecting anything, it was blocking every action. Compare the UIA
        # identity already computed (runtime_id + name + type +
        # automation_id), which is what "the same element" actually means.
        #
        # The re-scan is also bounded to the control's OWN window. Walking
        # every window on the desktop took 23 seconds on this machine, and a
        # UIA element cannot migrate between top-level windows - so this is
        # strictly safer as well as far faster.
        target_identity = _identity(record.before)
        current_matches = []
        for window in self._windows() or []:
            if (record.window_handle is not None
                    and hasattr(window, "handle")
                    and window.handle != record.window_handle):
                continue
            for control in self._controls(window):
                if _identity(_control_snapshot(control, 0)) == target_identity:
                    current_matches.append(control)
        if len(current_matches) != 1:
            result["failure_reason"] = "target token is stale or ambiguous"
            return result
        # Act on the freshly-found wrapper, so the action targets the element
        # we just verified rather than a possibly-cached one.
        target = current_matches[0]
        result["before"] = before
        # VERIFY FOCUS BEFORE TYPING. `type_keys`/SendKeys reach whatever window
        # owns the keyboard, not necessarily the verified control - so a stale or
        # wrong-window token could otherwise send text into a DIFFERENT app.
        # click/invoke/scroll travel as UIA patterns and need no focus.
        #
        # Skipped when a provider is injected: that is the headless test path,
        # where the HWNDs are fakes and there is no real foreground window to
        # read or obtain. On a real desktop this branch always runs.
        if action in ("type", "set_value") and self.provider is None:
            owner_handle = record.window_handle
            was_foreground = bool(owner_handle) and _foreground_handle() == owner_handle
            focused = was_foreground or _focus_handle(owner_handle)
            if not focused:
                # Windows foreground-lock can reject SetForegroundWindow for the
                # window while still allowing the control itself to take focus,
                # which normally promotes its window as a side effect.
                try:
                    target.set_focus()
                except Exception:
                    pass
                focused = bool(owner_handle) and _foreground_handle() == owner_handle
            result["focus"] = {"target_handle": owner_handle,
                               "was_foreground": was_foreground,
                               "focused": focused}
            if not focused:
                result["failure_reason"] = (
                    "refused: the target window could not be brought to the "
                    "foreground, so typing would land in another window")
                self._tokens.pop(token, None)
                return result
        # Observe the whole window so an effect elsewhere counts (see
        # _window_digest). Falls back to the target-only comparison if the
        # window cannot be re-resolved.
        owner = None
        for candidate in self._windows() or []:
            if (record.window_handle is not None
                    and getattr(candidate, "handle", None) == record.window_handle):
                owner = candidate
                break
        before_digest = _window_digest(owner) if owner is not None else None
        try:
            if action == "click":
                # 2026-10-02: prefer the accessibility pattern over a
                # synthetic mouse click. Measured on this machine,
                # click_input() on the Calculator's "7" button did
                # nothing (it moves the physical cursor and needs the
                # window focused), while invoke() - the InvokePattern -
                # worked immediately. Pattern calls are also immune to
                # UI privilege isolation, which silently swallows
                # injected input into a higher-integrity window.
                _invoke = getattr(target, "invoke", None)
                if callable(_invoke):
                    try:
                        _invoke()
                    except Exception:
                        target.click_input()
                else:
                    target.click_input()
            elif action == "invoke":
                target.invoke()
            elif action == "type":
                target.type_keys(_text(value))
            elif action == "scroll":
                # Semantic scroll ONLY. If the container does not actually scroll
                # we say so and let the caller fall back - we never silently
                # degrade into wheel input here, because that would move the
                # real cursor behind a consent token that was granted for a
                # pattern-based action.
                direction, amount = normalize_scroll(value)
                scroll_before = _scroll_state(target)
                if not (scroll_before["can_scroll_v"] or scroll_before["can_scroll_h"]):
                    result["failure_reason"] = (
                        "control exposes no working ScrollPattern (%s); "
                        "semantic scroll refused" % scroll_before["scroll_state"])
                    result["after"] = _control_snapshot(target, record.before["index"])
                    self._tokens.pop(token, None)
                    return result
                horizontal = direction in ("left", "right")
                if horizontal and not scroll_before["can_scroll_h"]:
                    result["failure_reason"] = "control is not horizontally scrollable"
                    result["after"] = _control_snapshot(target, record.before["index"])
                    self._tokens.pop(token, None)
                    return result
                if not horizontal and not scroll_before["can_scroll_v"]:
                    result["failure_reason"] = "control is not vertically scrollable"
                    result["after"] = _control_snapshot(target, record.before["index"])
                    self._tokens.pop(token, None)
                    return result
                # pywinauto's UIAWrapper.scroll is scroll(direction, amount,
                # count, retry_interval) where `amount` is "line" or "page" -
                # NOT a number of notches. Passing our integer notches here
                # raises ValueError, so a real semantic scroll could never have
                # succeeded. Notches map onto `count`, bounded.
                target.scroll(direction, "line",
                              count=max(1, min(int(amount), _SCROLL_MAX_AMOUNT)))
                result["mechanism"] = "uia_scroll_pattern"
                result["scroll"] = {"direction": direction, "amount": amount,
                                    "before": scroll_before}
            else:
                setter = getattr(target, "set_edit_text", None) or getattr(target, "set_value")
                setter(_text(value))
        except Exception as error:
            result["failure_reason"] = str(error) or error.__class__.__name__
            result["after"] = _control_snapshot(target, record.before["index"])
            return result
        after = _control_snapshot(target, record.before["index"])
        after_digest = _window_digest(owner) if owner is not None else None
        if before_digest is not None and after_digest is not None:
            changed = after_digest != before_digest
        else:
            changed = after != before
        if action == "scroll":
            # ROUND Z: a scroll is verified by the SCROLL POSITION moving, not
            # merely by the window digest changing (a clock ticking in the
            # window would otherwise fake a successful scroll). Percent first,
            # digest as the fallback when the provider exposes no percentage.
            scroll_after = _scroll_state(target)
            before_pct = scroll_before.get("scroll_percent_v")
            after_pct = scroll_after.get("scroll_percent_v")
            if before_pct is not None and after_pct is not None:
                percent_moved = before_pct != after_pct
            else:
                percent_moved = None
            result["scroll"]["after"] = scroll_after
            result["scroll"]["percent_moved"] = percent_moved
            if percent_moved is True:
                changed = True
            elif percent_moved is False:
                # The position did not move, so the viewport did not move. Do not
                # let a coincidental digest change claim success.
                changed = False
            result["evidence"] = ("scroll_percent" if percent_moved is not None
                                  else "window_digest")
        result.update(after=after, changed=changed, verified=changed)
        if not changed:
            result["failure_reason"] = "action produced no observable state change"
        else:
            result["success"] = True
        self._tokens.pop(token, None)
        return result

    def find_scrollable(self, target=None, *, name=None):
        """Bounded discovery of containers that can ACTUALLY scroll.

        ROUND Z. "Exposes ScrollPattern" is not enough: the live gate found
        elements on the real OpenCode window that expose the pattern and raise
        `AttributeError` when used. Only containers reporting can_scroll_v/h are
        returned, so a caller never picks a semantic target that cannot move.
        """
        snapshot = self.dom(target)
        if not snapshot.get("available") or snapshot.get("failure_reason"):
            return {"available": snapshot.get("available", False),
                    "failure_reason": snapshot.get("failure_reason"),
                    "candidates": [], "reported_only": []}
        wanted = str(name).strip().lower() if name else None
        usable, reported_only = [], []
        for record in snapshot.get("controls") or []:
            if wanted and (record.get("name") or "").strip().lower() != wanted:
                continue
            if record.get("can_scroll"):
                usable.append(record)
            elif record.get("exposes_scroll") or record.get("scroll_state") not in (
                    "none", None):
                reported_only.append(record)
        return {"available": True, "window": snapshot.get("window"),
                "candidates": usable, "reported_only": reported_only}


_DEFAULT = UIAutomation()
list_windows = _DEFAULT.list_windows
dom = _DEFAULT.dom
find_controls = _DEFAULT.find_controls
find_scrollable = _DEFAULT.find_scrollable
find_inputs = _DEFAULT.find_inputs
read_text = _DEFAULT.read_text
perform = _DEFAULT.perform
