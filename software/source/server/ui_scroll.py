"""ROUND Z BUILD 1: OBSERVE -> LOCATE -> ACT -> VERIFY for scrolling.

WHY THIS EXISTS
---------------
Measured on 2026-10-04, from a real turn: Sir asked FRIDAY to scroll up and read
what OpenCode had said before. She could not, and the reason was not a transient
failure:

* FRIDAY's own source contained NO scroll code at all.
* The only scroll in the tree was vendored `computer.mouse.scroll`, which
  reached the model as one auto-generated line and was never verified.
* `profiles/default.py` told her that if a target was "NOT visible in the
  screenshot" she must "say so honestly and use the LAUNCH path" - scrolling was
  not an offered option, so the playbook actively trained her not to try.
* Nothing re-observed the screen after an action, so even a sent scroll could
  never be confirmed.

THE LIVE GATE (2026-10-04, real OpenCode window, Chrome_WidgetWin_1)
--------------------------------------------------------------------
* Semantic UIA ScrollPattern is INERT there. Two elements expose the pattern; one
  raises `AttributeError: document "OpenCode" is not vertically scrollable`, the
  other `AttributeError: 'int' object has no attribute 'lower'`. Neither moved a
  pixel. So the wheel path is the PRIMARY mechanism for Chromium/Electron apps,
  and the semantic path is only trusted where it genuinely works.
* Wheel scrolling DOES work there, once the cursor is moved inside the target
  rect and the window is genuinely foreground.

TWO MEASUREMENT TRAPS THIS MODULE IS BUILT TO AVOID
--------------------------------------------------
1. `ImageGrab.grab(bbox=...)` WITHOUT `all_screens=True` measures the PRIMARY
   monitor's coordinate space. On this two-monitor desktop the OpenCode window
   sits at negative X, so an "all_screens-less" capture digests a static region
   of the other monitor and reports every scroll as a no-op. That produced a
   completely wrong gate result before it was caught.
2. pywinauto pattern properties RAISE rather than return False.
   `getattr(el, "iface_scroll", None)` does not yield None for an unsupported
   element, so naive probing marks every control scrollable.

SAFETY
------
* The semantic path goes through `ui_probe.act(action="scroll")`, i.e. the
  EXISTING HMAC signed-token consent gate. No second authorisation system.
* The wheel path deliberately has NO new consent gate (an explicit decision),
  but it is bounded by three hard rules: the target must be the real foreground
  window, the cursor is moved INSIDE the target rect, and the result is only
  reported as success when the viewport actually changed.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import time
from collections import deque

from . import ui_automation as _ua

# Bounded per-turn action record. This is bookkeeping so a turn can report what
# it actually did - focus -> scroll -> observe -> scroll - and state the NET
# change. It is NOT a planner, and it deliberately holds no retry logic.
MAX_ACTION_RECORDS = int(os.getenv("FRIDAY_UI_ACTION_RECORDS", "12"))
_HISTORY = deque(maxlen=max(1, MAX_ACTION_RECORDS))

# Verification sampling. Small on purpose: this runs after every scroll and the
# cost has to stay negligible.
_VIEWPORT_SAMPLE = (int(os.getenv("FRIDAY_UI_VIEWPORT_W", "240")),
                    int(os.getenv("FRIDAY_UI_VIEWPORT_H", "60")))
_SETTLE_SECONDS = float(os.getenv("FRIDAY_UI_SCROLL_SETTLE", "0.8"))


def _user32():
    try:
        return ctypes.windll.user32
    except Exception:
        return None


def foreground_handle():
    """The real foreground HWND, or None."""
    user32 = _user32()
    if user32 is None:
        return None
    try:
        handle = user32.GetForegroundWindow()
        return int(handle) if handle else None
    except Exception:
        return None


def viewport_digest(rect):
    """Content fingerprint of a screen rectangle.

    `all_screens=True` is load-bearing, not decoration: without it Pillow
    measures only the primary monitor and every negative-coordinate window
    (this machine's whole left monitor) digests as a constant.
    """
    left, top, right, bottom = [int(v) for v in rect]
    if right <= left or bottom <= top:
        return None
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        if image is None or image.size[0] <= 0 or image.size[1] <= 0:
            return None
        gray = image.convert("L").resize(_VIEWPORT_SAMPLE)
        return hashlib.sha256(gray.tobytes()).hexdigest()[:32]
    except Exception:
        return None


def rect_of(window_or_control):
    """(left, top, right, bottom) for a UIA element, or None."""
    try:
        r = window_or_control.rectangle()
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def _record(entry):
    _HISTORY.append(entry)
    return entry


def action_history():
    """The bounded per-turn action record, newest last."""
    return list(_HISTORY)


def reset_action_history():
    _HISTORY.clear()


def _window_handle(window):
    try:
        return int(getattr(window, "handle", 0) or 0) or None
    except (TypeError, ValueError):
        return None


def scroll_window(query, *, direction="up", amount=3, mechanism="auto",
                  confirmation=None, command_text="", actor="voice",
                  settle=None):
    """Scroll a window's viewport and VERIFY that it moved.

    `mechanism`:
      "auto"    - semantic ScrollPattern if a container really scrolls, else wheel
      "uia"     - semantic only; refuse rather than fall back
      "wheel"   - wheel only

    Returns a dict shaped like the other ui_probe results. `success` and
    `verified` are BOTH only true when the viewport demonstrably changed.
    """
    from . import ui_probe

    direction, amount = _ua.normalize_scroll({"direction": direction,
                                              "amount": amount})
    settle = _SETTLE_SECONDS if settle is None else float(settle)
    out = {
        "handled": True, "action": "ui_scroll", "mechanism": None,
        "risk": "reversible", "allowed": False, "changed": False,
        "success": False, "verified": False, "scroll_verified": False,
        "failure_reason": None,
        "scroll": {"direction": direction, "amount": amount},
    }

    def finish(**extra):
        out.update(extra)
        _record({
            "window": out.get("window"),
            "control": (out.get("control") or {}).get("name")
            if isinstance(out.get("control"), dict) else out.get("control"),
            "mechanism": out.get("mechanism"),
            "action": "scroll %s %d" % (direction, amount),
            "pre_state": out.get("pre_state"),
            "post_state": out.get("post_state"),
            "verified": bool(out.get("scroll_verified")),
        })
        _audit(out, command_text, actor)
        return out

    # -- OBSERVE: resolve the window ---------------------------------------
    # windows_control already owns exact/fuzzy window resolution and returns a
    # `bounds` tuple per window. Reusing it means scroll inherits the existing
    # refusal semantics (ambiguous target, minimized target) instead of growing
    # a second, weaker resolver.
    from . import windows_control as _wc

    exact = None
    if isinstance(query, dict) and query.get("handle"):
        try:
            exact = int(query["handle"])
        except (TypeError, ValueError):
            exact = None
    # _resolve(target) -> (window, failure). A string target is refused by
    # design (it requires an exact title AND process), so pass the dict form.
    target_arg = query if isinstance(query, dict) else {"handle": exact}
    if not target_arg.get("handle") and not target_arg.get("title"):
        out["failure_reason"] = (
            "scroll needs an exact window target: pass {\"handle\": hwnd} or "
            "{\"title\": ..., \"process\": ...} - a bare name is ambiguous")
        return finish()
    resolved_match, failure = _wc._resolve(target_arg)
    if failure or resolved_match is None:
        out["failure_reason"] = (failure or {}).get("reason") or "window not resolved"
        out["candidates"] = [
            (c or {}).get("title") for c in ((failure or {}).get("candidates") or [])[:6]
        ]
        return finish()
    # _resolve returns ((window, snapshot), failure) - the success payload is
    # itself a pair, which is easy to mistake for the window object.
    try:
        window_obj, resolved = resolved_match
    except (TypeError, ValueError):
        out["failure_reason"] = "window target resolved to an unusable shape"
        return finish()
    title = resolved.get("title") or str(query)
    out["window"] = title
    if resolved.get("minimized"):
        out["failure_reason"] = "%r is minimized - restore it first" % title
        return finish()
    target_handle = exact if exact is not None else resolved.get("handle")
    try:
        target_handle = int(target_handle) if target_handle else None
    except (TypeError, ValueError):
        target_handle = None
    window_rect = None
    raw_bounds = resolved.get("bounds")
    if raw_bounds and len(raw_bounds) == 4:
        try:
            # windows_control._bounds returns (left, top, WIDTH, HEIGHT), not
            # (l, t, r, b). Treating width as right silently produced a rect of
            # negative width on this machine's left-hand monitor, which then
            # looked like "could not determine the rect" and refused every
            # scroll. Convert explicitly.
            left, top, width, height = (int(v) for v in raw_bounds)
            if width > 0 and height > 0:
                window_rect = (left, top, left + width, top + height)
        except (TypeError, ValueError):
            window_rect = None

    # -- OBSERVE: the window must ACTUALLY be foreground --------------------
    # This is the regression for the real 2026-10-04 failure, where the
    # foreground was "System tray overflow window" (explorer.exe) while FRIDAY
    # believed she was operating on OpenCode. Scrolling whatever happens to be
    # under the cursor is the single worst outcome here, so we refuse instead.
    fg = foreground_handle()
    out["foreground_handle"] = fg
    out["target_handle"] = target_handle
    if target_handle is not None and fg is not None and fg != target_handle:
        out["failure_reason"] = (
            "refused: %r is not the foreground window (foreground handle %s) - "
            "refusing to scroll whatever is under the cursor"
            % (title, fg))
        return finish()

    # -- RECORD: pre-action observation ------------------------------------
    pre_digest = viewport_digest(window_rect) if window_rect else None
    out["pre_state"] = {"viewport_digest": pre_digest, "rect": window_rect}

    # -- LOCATE + ACT -------------------------------------------------------
    semantic_used = False
    if mechanism in ("auto", "uia"):
        found = _ua.UIAutomation(
            window_limit=ui_probe.MAX_WINDOWS,
            control_limit=ui_probe.MAX_CONTROLS).find_scrollable(target_handle)
        containers = (found or {}).get("candidates") or []
        if containers:
            # Best = largest visible container; a window-sized Document/Pane is
            # the real viewport, a 40px strip is not.
            best = max(containers, key=lambda c: _container_area(c))
            act = ui_probe.act(title, (best.get("name") or "").strip(),
                               action="scroll",
                               value={"direction": direction, "amount": amount},
                               confirmation=confirmation,
                               command_text=command_text, actor=actor,
                               # ROUND ZB-L1: pin the exact control we located.
                               # OpenCode's TUI exposes FIVE distinct controls
                               # all named "scrollable content", so re-resolving
                               # by name alone hit the ambiguity guard and a
                               # correctly-chosen target was refused.
                               control_index=best.get("index"))
            if act.get("success"):
                out["mechanism"] = "uia_scroll_pattern"
                out["control"] = {"name": best.get("name"),
                                  "control_type": best.get("control_type"),
                                  "scroll_state": best.get("scroll_state")}
                semantic_used = True
                out["consent"] = act.get("consent")
                out["scroll"]["before"] = (act.get("scroll") or {}).get("before")
                out["scroll"]["after"] = (act.get("scroll") or {}).get("after")
            else:
                # ROUND Z FIX: a REFUSED semantic act is terminal in EVERY mode.
                # It used to be terminal only for mechanism="uia"; in "auto" the
                # refusal fell through to the verify block, which set
                # allowed=True and success=True whenever the viewport digest
                # happened to differ - so a turn that was DENIED consent came
                # back claiming a verified scroll. Worse, semantic_used stayed
                # True so the wheel fallback was skipped, and the only thing
                # that "worked" was a coincidental repaint. Consent denial is
                # never a reason to try another mechanism.
                out["failure_reason"] = act.get("failure_reason") or (
                    "semantic scroll did not verify")
                out["control"] = {"name": best.get("name"),
                                  "control_type": best.get("control_type"),
                                  "scroll_state": best.get("scroll_state")}
                out["consent"] = act.get("consent")
                out["allowed"] = False
                out["changed"] = bool(act.get("changed"))
                out["success"] = False
                out["verified"] = False
                out["scroll_verified"] = bool(act.get("verified"))
                return finish()
        else:
            reported = (found or {}).get("reported_only") or []
            out["scrollable_reported_but_inert"] = len(reported)
            if mechanism == "uia":
                out["failure_reason"] = (
                    "no container in %r actually supports ScrollPattern "
                    "(%d element(s) expose it but report themselves as not "
                    "scrollable)" % (title, len(reported)))
                return finish()

    if not semantic_used:
        if mechanism == "uia":
            out["failure_reason"] = "semantic scroll unavailable"
            return finish()
        # -- ACT: wheel fallback. Foreground was already proven above. --------
        if not window_rect:
            out["failure_reason"] = ("could not determine %r's on-screen rect; "
                                     "refusing to scroll blind" % title)
            return finish()
        if fg is None:
            out["failure_reason"] = "foreground window unknown - refusing to scroll"
            return finish()
        left, top, right, bottom = window_rect
        # Cursor INSIDE the target rect, inset from the edges so the pointer never
        # lands on a border or a neighbouring window's overlap.
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        if not (left <= cx <= right and top <= cy <= bottom):
            out["failure_reason"] = "target rect is off-screen; refusing to scroll"
            return finish()
        out["mechanism"] = "wheel"
        out["cursor_target"] = [cx, cy]
        notches = amount if direction in ("up", "right") else -amount
        if direction in ("left", "right"):
            out["failure_reason"] = ("horizontal wheel scrolling is not "
                                     "supported by this fallback")
            return finish()
        try:
            import pyautogui

            pyautogui.moveTo(cx, cy, duration=0.2)
            time.sleep(0.25)
            pyautogui.scroll(notches)
        except Exception as error:
            out["failure_reason"] = ("wheel input failed: %s"
                                     % (error or type(error).__name__))
            return finish()

    # -- VERIFY -------------------------------------------------------------
    time.sleep(max(0.0, settle))
    post_digest = viewport_digest(window_rect) if window_rect else None
    out["post_state"] = {"viewport_digest": post_digest, "rect": window_rect}

    evidence = None
    if semantic_used:
        pct_before = ((out["scroll"].get("before") or {}).get("scroll_percent_v"))
        pct_after = ((out["scroll"].get("after") or {}).get("scroll_percent_v"))
        if pct_before is not None and pct_after is not None:
            evidence = "scroll_percent"
            moved = pct_before != pct_after
        else:
            evidence = "viewport_digest"
            moved = (pre_digest is not None and post_digest is not None
                     and pre_digest != post_digest)
    else:
        evidence = "viewport_digest"
        moved = (pre_digest is not None and post_digest is not None
                 and pre_digest != post_digest)

    out["evidence"] = evidence
    out["changed"] = bool(moved)
    out["verified"] = bool(moved)
    out["scroll_verified"] = bool(moved)
    # Reaching here means an action really was performed: either the consent-
    # gated semantic act returned success, or the wheel fallback ran (which
    # carries no consent gate of its own). Every refusal returns before this
    # point, so `allowed` is still False for a denied turn and True here.
    out["allowed"] = True
    if moved:
        out["success"] = True
    else:
        out["success"] = False
        if pre_digest is None or post_digest is None:
            out["failure_reason"] = (
                "scroll was sent but the viewport could not be observed, so it "
                "is NOT verified - no claim of success is made")
        else:
            out["failure_reason"] = (
                "scroll was sent but %s did not change, so the view did not "
                "move" % title)
    return finish()


def _container_area(record):
    """Rank scroll candidates by how much screen they plausibly cover.

    A window-sized Document/Pane is the real viewport; a 40px strip is not, and
    scrolling the strip moves content the user cannot see.
    """
    if not isinstance(record, dict):
        return 0
    rect = record.get("rect")
    if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
        return 0
    try:
        left, top, right, bottom = [int(v) for v in rect]
    except (TypeError, ValueError):
        return 0
    return max(0, right - left) * max(0, bottom - top)


def _audit(out, command_text, actor):
    try:
        from .audit import log_action

        log_action(actor=actor, risk="reversible",
                   operation="ui_scroll", outcome="ok" if out.get("success") else "failed",
                   allowed=bool(out.get("allowed")), command=command_text,
                   target=str(out.get("window") or ""),
                   changed=bool(out.get("changed")),
                   error=out.get("failure_reason"))
    except Exception:
        pass