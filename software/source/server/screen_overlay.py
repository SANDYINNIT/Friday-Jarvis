"""Display-only on-screen annotation overlay (ROUND ZB-OV).

Shows what FRIDAY MEANS - a chess square, a UI control, a route - as an arrow,
a highlight rectangle or a short label drawn ON TOP of the screen.

HARD PROPERTIES (measured on this machine, 2026-11-02, overlay_probe3)
------------------------------------------------------------------------
* **Display only.** `WS_EX_TRANSPARENT` passes every mouse message through and
  `WS_EX_NOACTIVATE` means the overlay can never take focus - so it cannot click
  a button, cannot accept input, and cannot act on the app underneath. Verified
  live: foreground window unchanged, exstyle bits set, geometry exact.
* **Excluded from capture by default.** `SetWindowDisplayAffinity(hwnd,
  WDA_EXCLUDEFROMCAPTURE)` hides it from screen capture. Verified with a
  baseline diff on BOTH of FRIDAY's capture paths: PIL ImageGrab showed
  blue_before == blue_after == 17 while blue == 690 with the affinity off (so it
  really renders), and DXcam dropped 14269 -> 4019 blue pixels, a delta of 10250
  ~= the overlay's own 136x76 = 10336 blue area. Consequence: FRIDAY cannot see
  her own annotations, so no feedback loop into vision/OCR/screen history.
* **Multi-monitor / DPI.** Rects are virtual-desktop pixels and may be negative
  (this machine's left display starts at x=-1920). Creation and placement run
  inside `_PerMonitorDPI`, so a 125% panel is addressed in physical pixels.
* **Bounded.** Short TTL, automatic clear on focus loss or when the target
  window moves/disappears, plus an explicit `clear()`. Nothing persists by
  default.

CONSENT (proportionate - display only)
--------------------------------------
The FIRST `annotate()` in a session must present a signed token minted from what
Sir ACTUALLY said (`action_confirmations.mint`/`consume`); there is deliberately
no `confirmed=` bypass anywhere. One token covers a short session so annotating
three squares does not need three spoken permissions, and the session ends on
TTL, focus loss, target change or `clear()`. Every draw is written to the audit
log. This module owns its gate and is NOT added to `ui_probe.ALLOWED_ACTIONS`,
so the existing action boundary is unchanged.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time

# -- bounded configuration (house style: FRIDAY_ prefixed, clamped) ----------
DEFAULT_TTL = float(os.environ.get("FRIDAY_OVERLAY_TTL", "20"))
MAX_TTL = 120.0
SESSION_TTL = float(os.environ.get("FRIDAY_OVERLAY_SESSION_TTL", "60"))
TICK_SECONDS = 0.5
MAX_LABEL_CHARS = 80

WDA_NONE = 0x00                     # SetWindowDisplayAffinity (winuser.h)
WDA_EXCLUDEFROMCAPTURE = 0x11

DEFAULT_COLOR = (255, 64, 64)
FILL_ALPHA = 70                     # straight alpha, premultiplied on render
SHAPES = ("rect", "arrow", "label")

BOUNDS_TOLERANCE = 3                # a resize animation may shift a pixel or two

_lock = threading.RLock()
_window = {"hwnd": 0}
# The window's owning thread, plus an optional way to get back onto it. MEASURED
# on this machine: ShowWindow(SW_HIDE) issued from any OTHER thread is a no-op -
# the TTL watcher cleared the state but the box stayed on screen, and only a
# clear() arriving on the owner thread finally hid it. ShowWindowAsync did not
# help either. So window state changes must run where the window was created.
_window_owner_tid = None
_window_executor = None
_active = None
_revision = 0
_session = {"on": False, "deadline": 0.0, "target": None}
_watch_stop = threading.Event()
_watch_thread = None
_stats = {"drawn": 0, "cleared": 0, "refused": 0, "superseded": 0}


def set_window_executor(fn):
    """Install `fn(callback)` which runs the callback on the window's thread.

    The server wires this with `loop.call_soon_threadsafe`, because its asyncio
    loop is the thread that creates the window. Left unset (tests, standalone
    use) the module falls back to running inline.
    """
    global _window_executor
    _window_executor = fn


def _clamp_ttl(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TTL
    return max(1.0, min(MAX_TTL, value))


def _bounds_match(a, b, tolerance=BOUNDS_TOLERANCE):
    if not a or not b or len(a) != 4 or len(b) != 4:
        return False
    return all(abs(int(x) - int(y)) <= tolerance for x, y in zip(a, b))


def _rect_inside(inner, outer, slack=128):
    if not inner or not outer or len(outer) != 4:
        return False
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


def _window_rect(hwnd):
    try:
        import win32gui

        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        return tuple(int(v) for v in win32gui.GetWindowRect(hwnd))
    except Exception:
        return None


def _foreground():
    try:
        import win32gui

        return win32gui.GetForegroundWindow() or 0
    except Exception:
        return 0


def _resolve_target(query):
    """(hwnd, reason-or-None, rect) for exactly one window."""
    try:
        from . import windows_control as _wc
    except Exception as error:
        return None, "window lookup unavailable: %s" % type(error).__name__, None
    try:
        candidates = _wc.find_windows(query, limit=5, exclude_dev=False)
    except TypeError:
        candidates = _wc.find_windows(query, limit=5)
    except Exception as error:
        return None, "window lookup failed: %s" % type(error).__name__, None
    if not candidates:
        return None, "no open window matched %r" % query, None
    if len(candidates) > 1:
        return None, "several windows match %r" % query, None
    best = candidates[0]
    hwnd = best.get("handle")
    bounds = best.get("bounds")
    if bounds and len(bounds) == 4:
        rect = (int(bounds[0]), int(bounds[1]),
                int(bounds[0]) + int(bounds[2]), int(bounds[1]) + int(bounds[3]))
    else:
        rect = _window_rect(hwnd)
    if rect is None:
        return None, "that window has no usable rectangle", None
    return hwnd, None, rect


def _candidates_of(query):
    try:
        from . import windows_control as _wc
        return [{"handle": c.get("handle"), "title": c.get("title")}
                for c in (_wc.find_windows(query, limit=5, exclude_dev=False)
                          or [])[:6]]
    except Exception:
        return []


def _PerMonitorDPI():
    """Reuse the proven thread-scoped DPI helper (screen_understanding)."""
    try:
        from .screen_understanding import _PerMonitorDPI as cls

        return cls()
    except Exception:
        class _Null:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Null()


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _premultiply(image):
    """Straight -> premultiplied alpha, which ULW_ALPHA requires."""
    from PIL import Image, ImageChops

    r, g, b, a = image.split()
    # `merge` is a MODULE function in Pillow, not a method on the image class -
    # `image.__class__.merge(...)` raises AttributeError and, because _render
    # converts that into a refusal, the overlay would silently never draw.
    return Image.merge(
        "RGBA",
        (ImageChops.multiply(r, a), ImageChops.multiply(g, a),
         ImageChops.multiply(b, a), a),
    )


def _font(size=16):
    from PIL import ImageFont

    for name in ("segoeui.ttf", "arial.ttf", "consola.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw(shape, width, height, label, color, fill, points=None):
    """Render one annotation to an RGBA image of (width, height)."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))   # alpha 0 = clear
    draw = ImageDraw.Draw(image)
    r, g, b = tuple(color)[:3]
    edge = (r, g, b, 255)

    if shape == "rect":
        if fill:
            draw.rectangle([0, 0, width - 1, height - 1],
                           fill=(r, g, b, FILL_ALPHA), outline=edge, width=3)
        else:
            draw.rectangle([1, 1, width - 2, height - 2], outline=edge, width=3)
    elif shape == "arrow":
        import math
        (x1, y1), (x2, y2) = points or ((3, 3), (width - 6, height - 6))
        draw.line([x1, y1, x2, y2], fill=edge, width=4)
        angle = math.atan2(y2-y1, x2-x1)
        head = min(18, max(6, math.hypot(x2-x1, y2-y1) / 4))
        draw.polygon([(x2, y2),
                      (x2-head*math.cos(angle-.5), y2-head*math.sin(angle-.5)),
                      (x2-head*math.cos(angle+.5), y2-head*math.sin(angle+.5))], fill=edge)
    elif shape == "label":
        text = str(label or "")[:MAX_LABEL_CHARS]
        font = _font(max(12, min(28, max(12, height // 2))))
        draw.rectangle([0, 0, width - 1, height - 1],
                       fill=(0, 0, 0, 200), outline=edge, width=2)
        draw.text((6, 4), text, font=font, fill=(255, 255, 255, 255))
    else:
        raise ValueError("unsupported annotation shape %r" % (shape,))
    return _premultiply(image)


def _ensure_window():
    """Create (once) the layered, click-through, no-activate top-level window."""
    hwnd = _window.get("hwnd") or 0
    if hwnd:
        try:
            import win32gui

            if win32gui.IsWindow(hwnd):
                return hwnd
        except Exception:
            pass

    import win32con
    import win32gui

    style = (win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
             | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW
             | win32con.WS_EX_TOPMOST)
    with _PerMonitorDPI():
        try:
            hwnd = win32gui.CreateWindowEx(
                style, "Static", "FRIDAY Annotation Overlay",
                win32con.WS_POPUP, 0, 0, 1, 1, 0, 0, 0, None)
        except Exception:
            hwnd = 0
        if not hwnd:
            _window["hwnd"] = 0
            return 0
        # Hide from DXcam / ImageGrab / print-screen so FRIDAY can never see her
        # own annotation (feedback loop) and evidence stays clean.
        try:
            from ctypes import wintypes
            setter = ctypes.windll.user32.SetWindowDisplayAffinity
            setter.argtypes = [wintypes.HWND, wintypes.DWORD]
            setter.restype = wintypes.BOOL
            getter = ctypes.windll.user32.GetWindowDisplayAffinity
            getter.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            getter.restype = wintypes.BOOL
            value = wintypes.DWORD()
            _window["capture_excluded"] = bool(setter(hwnd, WDA_EXCLUDEFROMCAPTURE)
                and getter(hwnd, ctypes.byref(value)) and value.value == WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            _window["capture_excluded"] = False
    global _window_owner_tid
    _window_owner_tid = threading.get_ident()
    _window["hwnd"] = hwnd
    return hwnd


def _render(hwnd, left, top, width, height, shape, label, color, fill, points=None, image=None):
    """Draw the annotation and push it as per-pixel alpha.

    Returns None on success, otherwise a short reason. Never raises: a failed
    blend must come back as a REFUSAL, never as a claimed success.
    """
    import win32con
    import win32gui
    import win32ui

    try:
        if image is None:
            image = _draw(shape, width, height, label, color, fill, points)
        screen_hdc = win32gui.GetDC(0)
        mem_dc = bitmap = None
        try:
            screen_dc = win32ui.CreateDCFromHandle(screen_hdc)
            mem_dc = screen_dc.CreateCompatibleDC()
            # A memory DC needs a SELECTED bitmap or drawing/blending into it is
            # undefined and UpdateLayeredWindow fails with ERROR_GEN_FAILURE.
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(screen_dc, width, height)
            mem_dc.SelectObject(bitmap)
            from PIL import ImageWin

            ImageWin.Dib(image).draw(mem_dc.GetSafeHdc(),
                                     (0, 0, width, height))
            # BLENDFUNCTION = (BlendOp, BlendFlags, SourceConstantAlpha,
            # AlphaFormat); AC_SRC_OVER=0, AC_SRC_ALPHA=1 -> per-pixel alpha
            # straight from the DIB.
            win32gui.UpdateLayeredWindow(
                hwnd, screen_hdc, (left, top), (width, height),
                mem_dc.GetSafeHdc(), (0, 0), 0, (0, 0, 255, 1),
                win32con.ULW_ALPHA)
        finally:
            # These objects are created by this blend. Deleting the DC first
            # deselects the bitmap; neither resource belongs to another window.
            if mem_dc is not None:
                try:
                    mem_dc.DeleteDC()
                except Exception:
                    pass
            if bitmap is not None:
                try:
                    win32gui.DeleteObject(bitmap.GetHandle())
                except Exception:
                    pass
            try:
                win32gui.ReleaseDC(0, screen_hdc)
            except Exception:
                pass
    except Exception as error:
        return "render failed: %s" % type(error).__name__
    return None


# ---------------------------------------------------------------------------
# consent / audit
# ---------------------------------------------------------------------------
def _audit(**kwargs):
    """Write one audit line. Never raises, never blocks a draw."""
    try:
        from .audit import log_action

        log_action(**kwargs)
    except Exception:
        pass


def _consent(query, target, *, confirmation, known_names, command_text, actor):
    """Return (ok, None) or (False, refusal_reason).

    One signed token covers a short session: annotating three squares must not
    need three spoken permissions, but the FIRST draw still cannot happen
    without Sir having actually said so. No `confirmed=` bypass exists here.
    """
    now = time.monotonic()
    with _lock:
        if _session["on"] and now < _session["deadline"]:
            return True, None                      # already authorised

    try:
        from . import action_confirmations as _confirm
    except Exception as error:
        return False, "consent module unavailable: %s" % type(error).__name__

    if not confirmation:
        token, denied = _confirm.mint("annotate", query, target,
                                      known_names=known_names)
        if not token:
            denied = denied if isinstance(denied, dict) else {}
            return False, ("refused: %s" % denied.get(
                "reason", "no consent recorded for annotation this turn"))
        confirmation = token
    ok, why, _info = _confirm.consume(confirmation, "annotate", query, target)
    if not ok:
        return False, "refused: %s" % why
    with _lock:
        _session["on"] = True
        _session["deadline"] = now + _clamp_ttl(SESSION_TTL)
        _session["target"] = query
    return True, None


# ---------------------------------------------------------------------------
# lifecycle / watching
# ---------------------------------------------------------------------------
def _hide_window(expected_revision=None):
    """Hide the overlay. Never raises.

    Must run on the thread that created the window - a ShowWindow from any other
    thread is a measured no-op here, which would leave a box stuck on screen
    after its TTL.
    """
    hwnd = _window.get("hwnd") or 0
    revision = _revision if expected_revision is None else expected_revision
    if not hwnd:
        return

    def _do_hide():
        with _lock:
            if revision != _revision:
                return
        try:
            import win32con
            import win32gui

            if win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        except Exception:
            pass

    if threading.get_ident() == _window_owner_tid:
        _do_hide()
        return
    executor = _window_executor
    if executor is None:
        _do_hide()          # best effort (tests / standalone)
        return
    try:
        executor(_do_hide)  # the real path: back onto the owning thread
    except Exception:
        _do_hide()


def clear(reason="manual", *, expected=None):
    """Hide the overlay and drop its session. Never raises."""
    global _active, _revision
    with _lock:
        if expected is not None and _active is not expected:
            return {"cleared": False, "reason": "superseded"}
        annotation = _active
        _active = None
        _revision += 1
        revision = _revision
        _session["on"] = False
        _session["deadline"] = 0.0
        _session["target"] = None
        if annotation is not None:
            _stats["cleared"] += 1
    _hide_window(revision)
    _audit(operation="overlay_clear", risk="reversible", allowed=True,
           outcome="ok", target=(annotation or {}).get("query", ""),
           reason=reason)
    return {"cleared": annotation is not None, "reason": reason}


def status():
    """Read-only snapshot. Safe when nothing is running."""
    with _lock:
        annotation = dict(_active) if _active else None
        session = dict(_session)
        stats = dict(_stats)
    if annotation:
        annotation["remaining_seconds"] = round(
            max(0.0, annotation["deadline"] - time.monotonic()), 1)
        annotation["target_alive"] = bool(
            _window_rect(annotation.get("target_handle")))
    return {
        "active": annotation is not None,
        "annotation": annotation,
        "session": {"on": session["on"],
                    "remaining_seconds": round(
                        max(0.0, session["deadline"] - time.monotonic()), 1)},
        "stats": stats,
        "shapes": list(SHAPES),
        "ttl_seconds": DEFAULT_TTL,
        "excluded_from_capture": _window.get("capture_excluded"),
    }


def is_active():
    with _lock:
        return _active is not None


def _watch_once():
    """One expiry check. Split out of the loop so it is testable directly."""
    with _lock:
        annotation = _active
    if annotation is None:
        return None
    now = time.monotonic()
    if now >= annotation["deadline"]:
        return clear("ttl_expired", expected=annotation)
    # Focus loss: the user moved on, so a box over the old window is now
    # misleading. Default safety behaviour.
    if (annotation.get("watch_foreground")
            and _foreground() != annotation.get("focus_at_start")):
        return clear("focus_lost", expected=annotation)
    # Target change: the window moved, resized or vanished.
    target = annotation.get("target_handle")
    if target:
        current = _window_rect(target)
        if current is None:
            return clear("target_gone", expected=annotation)
        if not _bounds_match(current, annotation.get("target_rect") or current):
            return clear("target_changed", expected=annotation)
    return None


def _watch():
    """Daemon tick: clear on TTL, focus loss, or a moved/disappeared target."""
    while not _watch_stop.wait(TICK_SECONDS):
        try:
            _watch_once()
        except Exception:
            continue              # a watcher must never take the process down


def _start_watch():
    global _watch_thread
    with _lock:
        if _watch_thread is not None and _watch_thread.is_alive():
            return
        _watch_stop.clear()
        thread = threading.Thread(target=_watch, name="friday-overlay-watch",
                                  daemon=True)
        thread.start()
        _watch_thread = thread


def _stop_watch():
    global _watch_thread
    _watch_stop.set()
    with _lock:
        _watch_thread = None


def reset():
    """Test/shutdown hook: drop state without touching the audit log."""
    global _active, _revision
    with _lock:
        _revision += 1
        revision = _revision
        _active = None
        _session["on"] = False
        _session["deadline"] = 0.0
        _stats.update({"drawn": 0, "cleared": 0, "refused": 0, "superseded": 0})
    _hide_window(revision)
    _stop_watch()


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def annotate(query, *, shape, rect, points=None, label="", color=None,
             fill=False, ttl=None, confirmation=None, known_names=(),
             command_text="", actor="voice"):
    """Draw ONE display-only annotation bound to `query`'s window.

    `rect` is (left, top, right, bottom) in VIRTUAL-DESKTOP pixels (negative
    coordinates are valid - this machine's left display starts at -1920).
    `shape` is `rect` | `arrow` | `label`; `arrow` may instead take `points`
    as [(x1, y1), (x2, y2)].

    Returns {"ok": bool, "reason": str, "candidates": [...], "status": {...}}.
    Never raises.
    """
    global _active, _revision
    shape = str(shape or "").strip().lower()
    if shape not in SHAPES:
        with _lock:
            _stats["refused"] += 1
        return {"ok": False, "reason": "shape must be one of %s" % (SHAPES,),
                "candidates": [], "status": status()}

    arrow_points = None
    try:
        if shape == "arrow" and points and len(points) == 2:
            xs = [int(p[0]) for p in points]
            ys = [int(p[1]) for p in points]
            if xs[0] == xs[1] and ys[0] == ys[1]:
                raise ValueError("zero-length arrow")
            rect = (min(xs)-12, min(ys)-12, max(xs)+13, max(ys)+13)
            arrow_points = tuple((xs[i]-rect[0], ys[i]-rect[1]) for i in (0, 1))
        rect = tuple(int(v) for v in (rect or ()))
    except Exception:
        rect = ()
    if len(rect) != 4 or rect[2] <= rect[0] or rect[3] <= rect[1]:
        with _lock:
            _stats["refused"] += 1
        return {"ok": False,
                "reason": "a positive (left, top, right, bottom) rect is required",
                "candidates": [], "status": status()}

    if ((rect[2]-rect[0]) * (rect[3]-rect[1]) > 4_194_304
            or max(abs(v) for v in rect) > 100_000):
        return {"ok": False, "reason": "annotation geometry exceeds the display limit",
                "candidates": [], "status": status()}

    target_handle, reason, target_rect = _resolve_target(query)
    if target_handle is None:
        with _lock:
            _stats["refused"] += 1
        return {"ok": False, "reason": reason,
                "candidates": _candidates_of(query), "status": status()}

    # The annotation must sit on ITS target window: a stale or off-window rect
    # is refused rather than drawn over something else.
    if not _rect_inside(rect, target_rect, slack=128):
        with _lock:
            _stats["refused"] += 1
        return {"ok": False,
                "reason": "that rectangle is not on window %r" % query,
                "target_rect": list(target_rect),
                "candidates": [], "status": status()}

    consent_target = str(label or query)
    ok, refused = _consent(query, consent_target, confirmation=confirmation,
                           known_names=known_names, command_text=command_text,
                           actor=actor)
    if not ok:
        with _lock:
            _stats["refused"] += 1
        _audit(operation="overlay_annotate", risk="reversible", allowed=False,
               outcome="refused", actor=actor, target=query,
               command=command_text, error=refused)
        return {"ok": False, "reason": refused, "candidates": [],
                "status": status()}

    width = max(2, rect[2] - rect[0])
    height = max(2, rect[3] - rect[1])
    hwnd = _ensure_window()
    if not hwnd:
        with _lock:
            _stats["refused"] += 1
        return {"ok": False, "reason": "could not create the overlay window",
                "candidates": [], "status": status()}

    with _PerMonitorDPI():
        failure = _render(hwnd, rect[0], rect[1], width, height, shape, label,
                          tuple(color) if color else DEFAULT_COLOR, bool(fill), arrow_points)
        if failure:
            with _lock:
                _stats["refused"] += 1
            _audit(operation="overlay_annotate", risk="reversible",
                   allowed=False, outcome="refused", actor=actor,
                   target=query, command=command_text, error=failure)
            return {"ok": False, "reason": failure, "candidates": [],
                    "status": status()}
        try:
            import win32con
            import win32gui

            # UpdateLayeredWindow lays out the pixels but does NOT show the
            # window - measured on this machine: the probe was still hidden
            # after ULW and needed this ShowWindow. NOACTIVATE keeps focus.
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        except Exception:
            pass

    now = time.monotonic()
    annotation = {
        "query": query,
        "shape": shape,
        "rect": list(rect),
        "label": str(label or "")[:MAX_LABEL_CHARS],
        "target_handle": target_handle,
        "target_rect": list(target_rect),
        "focus_at_start": _foreground(),
        "watch_foreground": True,
        "created_at": now,
        "deadline": now + _clamp_ttl(ttl if ttl is not None else DEFAULT_TTL),
        "session_target": consent_target,
    }
    with _lock:
        _revision += 1
        if _active is not None:
            _stats["superseded"] += 1
        _active = annotation
        _stats["drawn"] += 1
    _start_watch()
    _audit(operation="overlay_annotate", risk="reversible", allowed=True,
           outcome="ok", actor=actor, target=query, command=command_text,
           shape=shape, rect=",".join(str(v) for v in rect))
    return {"ok": True, "reason": "", "target": query, "shape": shape,
            "status": status()}


__all__ = ["annotate", "clear", "status", "is_active", "reset",
           "set_window_executor", "SHAPES", "DEFAULT_TTL", "SESSION_TTL"]
