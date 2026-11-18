"""Freely draggable desktop globe companion (ROUND ZB-C1).

A second, frameless pywebview window that floats above the desktop and can be
double-clicked into the full FRIDAY app.

REQUIREMENTS ENCODED HERE
-------------------------
* **Drag vs double-click** are different gestures on the same gesture surface:
  `press()` records where the pointer went down, `on_drag()` moves a preview
  (never persists), `on_drag_end()` persists, and `on_double_click()` returns
  True only when the pointer stayed inside `DRAG_THRESHOLD_PX` of the press -
  a real drag must never be mistaken for a double-click.
* **Position survives restart, monitor changes and DPI changes.** Saved
  coordinates are stored in *virtual-screen* pixels; `restore()` re-reads them
  and CLAMPS to whatever the screen is now, so unplugging a monitor moves the
  globe back on-screen instead of leaving it stranded in dead space.
* **No focus stealing.** The window is created `frameless=True, easy_drag=False`
  (matching the main HUD, whose frameless windows use `__pywebview__draggable`
  regions rather than easy_drag because easy_drag swallows the scroll wheel) and
  is never explicitly focused on show.
* **Recovery.** Hiding is always reversible: the tray "Open App" and the
  configured hotkey both restore it, and `find_recovery_window()` reports how.

Nothing here touches FRIDAY's memory, providers or secrets.
"""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, asdict, field

SETTINGS_ENV = "FRIDAY_GLOBE_SETTINGS"
DRAG_THRESHOLD_PX = 6
DOUBLE_CLICK_MS = 400
DEFAULT_WIDTH = 240
DEFAULT_HEIGHT = 240
EXPANDED_WIDTH = 460
EXPANDED_HEIGHT = 460

DEFAULTS = {"x": 120, "y": 120, "visible": True, "hotkey": "ctrl+alt+f",
            "width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT}


def settings_path() -> str:
    override = os.environ.get(SETTINGS_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".friday", "globe.json")


def screen_bounds():
    """Virtual desktop bounds; the LEFT monitor has negative coordinates."""
    try:
        u32 = ctypes.windll.user32
        left = int(u32.GetSystemMetrics(76))     # SM_XVIRTUALSCREEN
        top = int(u32.GetSystemMetrics(77))      # SM_YVIRTUALSCREEN
        width = int(u32.GetSystemMetrics(78))    # SM_CXVIRTUALSCREEN
        height = int(u32.GetSystemMetrics(79))   # SM_CYVIRTUALSCREEN
        if width > 0 and height > 0:
            return (left, top, left + width, top + height)
    except Exception:
        pass
    return (0, 0, 1920, 1080)


def clamp_to_screen(x, y, width, height):
    """Keep at least 40 px of the window inside the virtual screen."""
    try:
        x, y, width, height = int(x), int(y), int(width), int(height)
    except Exception:
        return DEFAULTS["x"], DEFAULTS["y"]
    left, top, right, bottom = screen_bounds()
    min_visible = 40
    x = max(left - width + min_visible, min(x, right - min_visible))
    y = max(top - height + min_visible, min(y, bottom - min_visible))
    return x, y


def read_dpi_scale() -> float:
    try:
        scale = float(ctypes.windll.user32.GetDpiForSystem())
        return round(scale / 96.0, 3) if scale > 0 else 1.0
    except Exception:
        return 1.0


def is_inside(x, y, width, height, rect):
    """Pure geometry: does the window at (x,y) sit fully inside `rect`?"""
    try:
        x, y, width, height = int(x), int(y), int(width), int(height)
        left, top, right, bottom = (int(v) for v in rect)
    except Exception:
        return False
    return x >= left and y >= top and x + width <= right and y + height <= bottom


def load_settings(path=None) -> dict:
    path = path or settings_path()
    settings = dict(DEFAULTS)
    try:
        if not os.path.isfile(path):
            return settings
        if os.path.getsize(path) > 64 * 1024:
            return settings
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            for key, default in DEFAULTS.items():
                value = loaded.get(key, default)
                if key in ("visible",) :
                    value = bool(value)
                elif key == "hotkey":
                    value = str(value or default)
                else:
                    try:
                        value = int(value)
                    except Exception:
                        value = default
                settings[key] = value
    except Exception:
        return dict(DEFAULTS)
    return settings


def save_settings(data, path=None) -> dict:
    """Atomic write, clamped to the CURRENT screen. Never raises."""
    path = path or settings_path()
    try:
        merged = dict(DEFAULTS)
        if isinstance(data, dict):
            for key in DEFAULTS:
                if key in data and data[key] is not None:
                    merged[key] = data[key]
        merged["x"], merged["y"] = clamp_to_screen(
            merged["x"], merged["y"], int(merged["width"]), int(merged["height"]))
        for key in ("x", "y", "width", "height"):
            try:
                merged[key] = int(merged[key])
            except Exception:
                merged[key] = DEFAULTS[key]
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(merged, handle)
        os.replace(temp, path)
        return {"ok": True, "settings": merged}
    except Exception as error:
        return {"ok": False, "reason": type(error).__name__}


def find_recovery_window() -> dict:
    """How to bring the globe back. Never acts - only reports."""
    return {"tray": True, "hotkey": load_settings().get("hotkey"),
            "env": "FRIDAY_GLOBE_HIDDEN",
            "note": "tray 'Open App' or the hotkey restores a hidden globe"}


@dataclass
class GlobeState:
    x: int = DEFAULTS["x"]
    y: int = DEFAULTS["y"]
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    visible: bool = True
    expanded: bool = False
    scale: float = 1.0
    monitor: str = "primary"
    updated_at: float = field(default_factory=time.time)

    def to_dict(self):
        payload = asdict(self)
        payload["screen"] = list(screen_bounds())
        payload["recovery"] = find_recovery_window()
        return payload


class GlobeController:
    """Position/visibility/double-click policy for the floating globe."""

    def __init__(self, settings_path_=None):
        self._path = settings_path_
        self._press = None
        settings = load_settings(self._path)
        self.state = GlobeState(
            x=int(settings["x"]), y=int(settings["y"]),
            width=int(settings["width"]), height=int(settings["height"]),
            visible=bool(settings["visible"]),
            scale=read_dpi_scale(),
        )
        # A coordinate saved against a monitor that no longer exists must come
        # back on-screen rather than be rejected.
        self.state.x, self.state.y = clamp_to_screen(
            self.state.x, self.state.y, self.state.width, self.state.height)

    # -- persistence --------------------------------------------------------
    def state_dict(self) -> dict:
        return self.state.to_dict()

    def _persist(self) -> dict:
        return save_settings({"x": self.state.x, "y": self.state.y,
                              "width": self.state.width,
                              "height": self.state.height,
                              "visible": self.state.visible},
                             self._path)

    # -- gestures -----------------------------------------------------------
    def press(self, x, y) -> dict:
        self._press = (int(x), int(y))
        return {"ok": True, "press": list(self._press)}

    def on_drag(self, x, y) -> dict:
        """Move without persisting - cheap, called per mouse move."""
        self.state.x, self.state.y = clamp_to_screen(
            x, y, self.state.width, self.state.height)
        self.state.updated_at = time.time()
        return {"ok": True, "x": self.state.x, "y": self.state.y,
                "persisted": False}

    def on_drag_end(self, x, y) -> dict:
        self.on_drag(x, y)
        result = self._persist()
        return {"ok": bool(result.get("ok")), "x": self.state.x,
                "y": self.state.y, "persisted": True,
                "settings": result.get("settings")}

    def on_double_click(self, x, y) -> bool:
        """True only for a real double-click: pointer inside the press radius."""
        if self._press is None:
            return False
        try:
            moved = abs(int(x) - self._press[0]) + abs(int(y) - self._press[1])
        except Exception:
            return False
        self._press = None
        if moved > DRAG_THRESHOLD_PX:
            return False                    # that was a drag, not a click
        if self.state.expanded:
            return False                    # already open
        return True

    # -- commands -----------------------------------------------------------
    def set_position(self, x, y, *, persist=True) -> dict:
        self.on_drag(x, y)
        if persist:
            return self.on_drag_end(x, y)
        return {"ok": True, "x": self.state.x, "y": self.state.y,
                "persisted": False}

    def toggle_visible(self, show=None) -> dict:
        self.state.visible = (not self.state.visible) if show is None else bool(show)
        self.state.updated_at = time.time()
        self._persist()
        return {"ok": True, "visible": self.state.visible,
                "recovery": find_recovery_window()}

    def collapse(self) -> dict:
        self.state.expanded = False
        self.state.width, self.state.height = DEFAULT_WIDTH, DEFAULT_HEIGHT
        self.state.x, self.state.y = clamp_to_screen(
            self.state.x, self.state.y, self.state.width, self.state.height)
        self.state.updated_at = time.time()
        self._persist()
        return {"ok": True, **self.state_dict()}

    def expand(self) -> dict:
        self.state.expanded = True
        self.state.width, self.state.height = EXPANDED_WIDTH, EXPANDED_HEIGHT
        self.state.x, self.state.y = clamp_to_screen(
            self.state.x, self.state.y, self.state.width, self.state.height)
        self.state.updated_at = time.time()
        self._persist()
        return {"ok": True, **self.state_dict()}

    def restore(self) -> dict:
        """Re-read from disk and clamp to whatever the screen is NOW."""
        settings = load_settings(self._path)
        self.state.x = int(settings["x"])
        self.state.y = int(settings["y"])
        self.state.visible = bool(settings["visible"])
        self.state.width = int(settings["width"])
        self.state.height = int(settings["height"])
        self.state.scale = read_dpi_scale()
        before = (self.state.x, self.state.y)
        self.state.x, self.state.y = clamp_to_screen(
            self.state.x, self.state.y, self.state.width, self.state.height)
        clamped = before != (self.state.x, self.state.y)
        if clamped:
            self._persist()                 # keep the corrected position
        return {"ok": True, "clamped": clamped, **self.state_dict()}

    @staticmethod
    def hotkey_matches(combo: str, settings=None) -> bool:
        wanted = str((settings or load_settings()).get("hotkey") or "")
        left = "".join(str(combo or "").lower().split())
        right = "".join(wanted.lower().split())
        return bool(left) and left == right


# ---------------------------------------------------------------------------
# pywebview bridge + window (lazy so this module imports without pywebview)
# ---------------------------------------------------------------------------
class GlobeBridge:
    """js_api for the globe window. Only what the globe page needs.

    Sir reported on 2026-11-04 that the globe "cannot be dragged anywhere".
    Two independent causes, both fixed here:

    * the page never reached this bridge (the script captured pywebview's api
      before `_createApi()` ran - fixed in `globe_html`), and
    * `drag()` only updated the CONTROLLER's x/y and never moved the OS
      window. pywebview's `Window.move()` must run on the GUI thread, so the
      move is done with `SetWindowPos` on the real hwnd, which is safe from
      the js_api worker thread.
    """

    def __init__(self, controller, on_open, on_hide=None,
                 speech_provider=None):
        self._controller = controller
        self._on_open = on_open
        self._on_hide = on_hide
        self._speech = speech_provider
        self._hwnd = None
        self._press_cursor = None
        self._press_origin = None

    def speech(self):
        """REAL playback acknowledgement, polled by the globe page.

        The globe must bounce on what the OUTPUT CLIENT actually played, not on
        a timer. The server runs muted, so this snapshot (first_audio,
        bytes_played, drained, stop_reason) is the only evidence of sound that
        exists. Never raises: a missing provider yields an honest empty state.
        """
        try:
            payload = self._speech() if self._speech else None
            return payload if isinstance(payload, dict) else {
                "client_playback_observable": False}
        except Exception as error:
            return {"client_playback_observable": False,
                    "error": type(error).__name__}

    def attach_hwnd(self, hwnd):
        """Called by `_finalize_globe` once the native window exists."""
        try:
            self._hwnd = int(hwnd) or None
        except Exception:
            self._hwnd = None
        return {"ok": bool(self._hwnd), "hwnd": self._hwnd}

    def state(self):
        return self._controller.state_dict()

    # -- geometry ---------------------------------------------------------
    def _cursor(self):
        try:
            import win32api
            return win32api.GetCursorPos()
        except Exception:
            return None

    def _rect(self):
        if not self._hwnd:
            return None
        try:
            import win32gui
            return win32gui.GetWindowRect(self._hwnd)
        except Exception:
            return None

    def _move_window(self, x, y):
        """Move the real OS window. Safe from any thread (SetWindowPos)."""
        import win32con
        import win32gui
        win32gui.SetWindowPos(
            self._hwnd, 0, int(x), int(y), 0, 0,
            win32con.SWP_NOSIZE | win32con.SWP_NOZORDER |
            win32con.SWP_NOACTIVATE)

    def press(self, x, y):
        result = self._controller.press(x, y)
        # Screen-space grab point: the page only knows client coordinates, so
        # the delta is taken from the real cursor instead.
        self._press_cursor = self._cursor()
        rect = self._rect()
        self._press_origin = (rect[0], rect[1]) if rect else None
        return result

    def drag(self, x, y):
        if self._hwnd and self._press_cursor and self._press_origin:
            try:
                cursor = self._cursor()
                if cursor is None:
                    return self._controller.on_drag(x, y)
                nx = self._press_origin[0] + (cursor[0] - self._press_cursor[0])
                ny = self._press_origin[1] + (cursor[1] - self._press_cursor[1])
                nx, ny = clamp_to_screen(nx, ny, self._controller.state.width,
                                         self._controller.state.height)
                self._move_window(nx, ny)
                # keep the controller in sync so drag_end persists the truth
                return self._controller.on_drag(nx, ny)
            except Exception:
                pass
        return self._controller.on_drag(x, y)

    def drag_end(self, x, y):
        if self._hwnd and self._press_origin:
            try:
                rect = self._rect()
                if rect:
                    self._controller.on_drag(rect[0], rect[1])
            except Exception:
                pass
        self._press_cursor = None
        self._press_origin = None
        return self._controller.on_drag_end(
            self._controller.state.x, self._controller.state.y)

    def double_click(self, x, y):
        if not self._controller.on_double_click(x, y):
            return {"ok": False, "reason": "not a double-click"}
        opened = False
        try:
            opened = bool(self._on_open and self._on_open())
        except Exception:
            opened = False
        return {"ok": opened, "reason": "" if opened else "main window unavailable"}

    def hide(self):
        result = self._controller.toggle_visible(show=False)
        try:
            if self._on_hide:
                self._on_hide()
        except Exception:
            pass
        return result

    def recovery(self):
        return find_recovery_window()


def globe_html() -> str:
    """The floating page: a self-lit orb, no external assets.

    Three defects are fixed here, all reported by Sir on 2026-11-04:

    1. WHITE PANEL. WebView2 paints an opaque white background no matter what
       `transparent=True` does (measured: 95.1% of the window was pure white,
       centre pixel 255,255,255). The page therefore paints its OWN dark
       sphere across the whole viewport, so an opaque window reads as an
       intentional dark orb badge instead of a pale rectangle, and the native
       window is additionally clipped to a circle (`_clip_to_circle`) so the
       desktop shows through around it.
    2. NOTHING HAPPENED ON CLICK/DRAG. The old script captured
       `window.pywebview.api` ONCE while parsing, but pywebview only calls
       `_createApi()` after the page has loaded (webview/window.py, guarded by
       `events.loaded.is_set()`), so `api.press`/`api.drag`/`api.double_click`
       were permanently undefined. The bridge is now resolved on EVERY call
       and re-resolved on `pywebviewready`.
    3. TOO FAINT TO SEE. The old orb was a 1px ring at 0.35 alpha plus 1px
       dots - "extremely faint cyan dots, a faint outlined circle". The orb is
       now a filled, shaded sphere with a specular highlight, rim light and a
       glow, and the wordmark is high-contrast.
    """
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>FRIDAY</title><style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;
  font-family:Consolas,'Courier New',monospace;user-select:none;cursor:grab}
body{display:grid;place-items:center;position:relative;
  background:radial-gradient(circle at 34% 28%, #7ff0ff 0%, #27b6ff 14%,
    #0a76d8 36%, #06407e 58%, #04214a 76%, #020c1e 90%, #01050d 100%)}
/* atmospheric limb: bright where the light hits, dark on the far side */
body::before{content:"";position:absolute;inset:0;border-radius:50%;
  box-shadow:inset -18px -22px 46px rgba(0,0,0,.72),
             inset 10px 12px 30px rgba(150,240,255,.30),
             inset 0 0 0 1px rgba(160,245,255,.55);
  pointer-events:none}
/* outer glow so the orb reads as floating above the desktop */
body::after{content:"";position:absolute;inset:-6%;border-radius:50%;
  box-shadow:0 0 26px 4px rgba(40,180,255,.35),
             0 0 60px 10px rgba(20,120,255,.18);
  pointer-events:none}
canvas{position:absolute;inset:0;width:100%;height:100%}
.label{position:absolute;bottom:9%;left:0;right:0;text-align:center;
  color:#ffffff;letter-spacing:.34em;font-size:13px;font-weight:700;
  text-shadow:0 0 4px #001018,0 0 5px #001018,0 0 6px #001018,
    0 0 16px #2fd8ff,0 0 30px #0af;
  pointer-events:none;z-index:2}
</style></head><body>
<canvas id="c" width="240" height="240" aria-label="FRIDAY globe"></canvas>
<div class="label">F R I D A Y</div>
<script>
(function () {
  // Resolve the js_api on EVERY call. pywebview populates it only after the
  // page has loaded, so capturing it here once would freeze an empty object.
  function api() {
    var p = window.pywebview;
    return (p && p.api && (p.api.press || p.api.drag)) ? p.api : null;
  }
  window.addEventListener('pywebviewready', function () { draw(); });

  var cv = document.getElementById('c'), ctx = cv.getContext('2d');
  var N = 260, pts = [], ang = 0.5, pulse = 0, raf = 0;
  for (var i = 0; i < N; i++) {
    var y = 1 - 2 * (i + 0.5) / N, r = Math.sqrt(Math.max(0, 1 - y * y));
    var a = i * 2.399963229728653;
    pts.push([Math.cos(a) * r, y, Math.sin(a) * r]);
  }
  var reduced = false;
  try { reduced = localStorage.getItem('friday_globe_reduced') === '1'; } catch (e) {}
  function draw() {
    var w = cv.width, h = cv.height, s = Math.min(w, h) * 0.5;
    ctx.clearRect(0, 0, w, h);
    var cx = w / 2, cy = h / 2, k = 1 + pulse * 0.08;
    // latitude / longitude wire - visible, not a hairline
    ctx.strokeStyle = 'rgba(190,245,255,0.28)';
    ctx.lineWidth = 1.1;
    for (var g = -2; g <= 2; g++) {
      ctx.beginPath();
      ctx.ellipse(cx, cy, s * k, Math.abs(g) * s * k * 0.34, 0, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.ellipse(cx, cy, s * k * 0.42, s * k, 0, 0, Math.PI * 2);
    ctx.stroke();
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i];
      var X = p[0] * Math.cos(ang) + p[2] * Math.sin(ang);
      var Z = p[2] * Math.cos(ang) - p[0] * Math.sin(ang);
      if (Z < -0.05) continue;
      var px = cx + X * s * k, py = cy + p[1] * s * k;
      var lit = (Z + 1) / 2;
      ctx.fillStyle = 'rgba(' + Math.round(120 + 110 * lit) + ',' +
        Math.round(215 + 40 * lit) + ',255,' + (0.35 + 0.6 * lit) + ')';
      ctx.beginPath();
      ctx.arc(px, py, (Z > 0.4 ? 2.1 : 1.4) * k, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  function loop() { ang += 0.006; if (pulse > 0) pulse = Math.max(0, pulse - 0.05); draw(); raf = requestAnimationFrame(loop); }
  function start() { if (raf) return; if (reduced) { draw(); return; } raf = requestAnimationFrame(loop); }
  function stop() { if (raf) { cancelAnimationFrame(raf); raf = 0; } draw(); }
  window.addEventListener('resize', function () {
    var r = cv.getBoundingClientRect(), d = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = Math.max(1, Math.round(r.width * d)); cv.height = Math.max(1, Math.round(r.height * d));
    ctx.setTransform(d, 0, 0, d, 0, 0); draw();
  });
  cv.width = 240; cv.height = 240;
  start();

  // gesture: drag moves the OS window (the bridge does the Win32 move), and a
  // click that barely moved is a double-click candidate.
  var down = null, lastDown = 0, dragging = false, moved = 0;
  document.addEventListener('pointerdown', function (e) {
    down = { x: e.clientX, y: e.clientY }; moved = 0; dragging = false;
    var a = api(); if (a && a.press) { try { a.press(e.clientX, e.clientY); } catch (err) {} }
  });
  document.addEventListener('pointermove', function (e) {
    if (!down) return;
    moved = Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y);
    if (moved > 6 && !dragging) { dragging = true; document.body.style.cursor = 'grabbing'; }
    if (dragging) { var a = api(); if (a && a.drag) { try { a.drag(e.clientX, e.clientY); } catch (err) {} } }
  });
  document.addEventListener('pointerup', function (e) {
    if (down && dragging) { var a1 = api(); if (a1 && a1.drag_end) { try { a1.drag_end(e.clientX, e.clientY); } catch (err) {} } }
    if (down && moved <= 6) {
      var now = Date.now();
      var a2 = api();
      if (now - lastDown <= 400 && a2 && a2.double_click) {
        lastDown = 0;
        try { a2.double_click(e.clientX, e.clientY); } catch (err) {}
      } else { lastDown = now; }
    }
    down = null; dragging = false; document.body.style.cursor = 'grab';
  });
  // playback-driven bounce: driven by what the OUTPUT CLIENT actually played.
  // Two sources, both real:
  //   1. push - the server calls GLOBE_EVENT on audio/stop (emit_ui_event)
  //   2. poll - the globe reads the same playback acknowledgement the HUD's
  //      captions use, so the bounce survives a missed push and never fires
  //      when nothing is playing.
  window.GLOBE_EVENT = function (type) {
    var t = (type && typeof type === 'object') ? String(type.type || '') : String(type || '');
    if (t === 'audio_chunk') { if (!reduced) pulse = Math.min(1, pulse + 0.45); start(); }
    else if (t === 'audio_start') { if (!reduced) start(); }
    else if (t === 'audio_end' || t === 'stop') { stop(); pulse = 0; draw(); }
  };

  var lastBytes = -1, wasActive = false;
  function fire(t) { try { window.GLOBE_EVENT(t); } catch (e) {} }
  function pollSpeech() {
    var a = api(); if (!a || !a.speech) return;
    var s; try { s = a.speech(); } catch (e) { return; }
    if (!s) return;
    var bytes = Number(s.bytes_played || 0);
    var observable = !!s.client_playback_observable;
    var stopped = !!s.stop_reason || !!s.drained;
    var active = observable && bytes > 0 && !stopped;
    if (active && !wasActive) fire('audio_start');
    if (active && bytes !== lastBytes) fire('audio_chunk');
    if (!active && wasActive) fire('audio_end');
    wasActive = active; lastBytes = bytes;
  }
  setInterval(function () { if (!document.hidden) { try { pollSpeech(); } catch (e) {} } }, 400);
})();
</script></body></html>"""


def open_globe(controller=None, on_open=None, on_hide=None, start_hidden=None,
               speech_provider=None):
    """Create the globe window. Returns a dict; never raises.

    Called before `webview.start()` from `run_status_window`. The window is
    frameless + `easy_drag=False` (consistent with the main HUD, whose frameless
    windows drag via `__pywebview__draggable` regions instead), and it is NEVER
    explicitly focused, so showing it cannot steal focus from Sir's app.
    """
    controller = controller or GlobeController()
    try:
        import webview
    except Exception as error:
        return {"ok": False, "reason": "pywebview unavailable: %s"
                % type(error).__name__}
    state = controller.state_dict()
    hidden = (not state.get("visible", True)) if start_hidden is None \
        else not start_hidden
    # pywebview accepts `html=` directly, and that is the ONLY form that works
    # here: a live capture of the globe window showed WebView2's "404 Not Found"
    # for BOTH a `data:` URL and a `file://` URI (even beside the working
    # ui/index.html). Passing the markup avoids the loader entirely, and keeps
    # `globe_html()` as the single source of truth - no page file is written.
    page_html = globe_html()
    bridge = GlobeBridge(controller, on_open=on_open, on_hide=on_hide,
                         speech_provider=speech_provider)
    base = dict(x=int(state["x"]), y=int(state["y"]),
                width=int(state["width"]), height=int(state["height"]),
                frameless=True, easy_drag=False, hidden=bool(hidden),
                # focus=False: showing the globe must never steal focus from
                # whatever Sir is doing (signature confirmed on this pywebview).
                focus=False,
                # WebView2 paints WHITE before/behind the page even with
                # transparent=True (measured 95.1% white). Black is the
                # intentional fallback: never a pale rectangle.
                background_color="#000000",
                js_api=bridge)
    # Capability ladder - ORDER MATTERS, proven 2026-11-04.
    #
    # `transparent=True` must NOT be first (it used to be). On this
    # pywebview/WinForms build it calls SetLayeredWindowAttributes with
    # LWA_COLORKEY, and a colour-keyed layered window receives NO mouse input
    # at all: measured with WindowFromPoint, every point over the globe
    # resolved to the desktop underneath, so drag and double-click were
    # physically impossible no matter what the page did. Clearing the key
    # (SetLayeredWindowAttributes(h,0,255,LWA_ALPHA)) instantly made the
    # whole grid resolve back to the globe.
    #
    # Transparency now comes from the CIRCULAR WINDOW REGION applied in
    # `_finalize_globe` instead: the desktop shows through the corners with
    # no colour key and full input.
    attempts = (
        dict(base, on_top=True),
        dict(base),
    )
    for kwargs in attempts:
        try:
            window = webview.create_window(GLOBE_TITLE, html=page_html, **kwargs)
            threading.Thread(target=_finalize_globe, args=(bridge,),
                             daemon=True, name="globe-finalize").start()
            return {"ok": True, "hidden": bool(hidden), "settings": state,
                    "html_bytes": len(page_html),
                    "options": sorted(k for k in kwargs
                                      if k not in ("x", "y", "width", "height",
                                                   "js_api"))}
        except Exception as error:
            last_error = "%s: %s" % (type(error).__name__, error)
            continue
    return {"ok": False, "reason": last_error}


GLOBE_TITLE = "FRIDAY Globe"


def _finalize_globe(bridge, timeout=25.0):
    """Attach the real hwnd once pywebview has created the native window.

    The native form does not exist until `webview.start()` runs, so this polls
    for it and then clips the window to a CIRCLE. The clip is what makes the
    surroundings genuinely transparent: `SetWindowRgn` removes the corners from
    the window entirely, so the desktop shows through instead of WebView2's
    opaque white - true transparency by geometry, independent of whether this
    pywebview/WebView2 honours `transparent=True`.
    """
    import time as _time

    import win32gui
    hwnd = 0
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            hwnd = win32gui.FindWindow(None, GLOBE_TITLE)
        except Exception:
            hwnd = 0
        if hwnd:
            break
        _time.sleep(0.2)
    if not hwnd:
        print("[globe] native window never appeared (no %r window)"
              % GLOBE_TITLE, flush=True)
        return
    bridge.attach_hwnd(hwnd)
    # Defensive: a colour-keyed layered window is mouse-dead (proven
    # 2026-11-04 - WindowFromPoint resolved every point to the desktop).
    # Whatever pywebview set up, force plain full-opacity compositing so the
    # window can always be dragged and double-clicked. Harmless if the window
    # is not layered (the call simply fails).
    try:
        import ctypes as _ct
        _ct.windll.user32.SetLayeredWindowAttributes(hwnd, 0, 255, 2)  # LWA_ALPHA
    except Exception:
        pass
    clipped = False
    applied = 0
    # Windows DISCARDS a window region whenever the window is resized, and
    # pywebview resizes the form after this first pass (the requested 240x240
    # settles at 224x201). Measured on 2026-11-04: one application had already
    # been lost - `GetWindowRgn` returned ERROR and the orb painted over its
    # own corners. Re-apply only when the region is missing or stale, so a
    # drag is never disturbed by a needless call.
    import time as _t2
    from ctypes import wintypes as _wt

    class _RgnHeader(ctypes.Structure):
        _fields_ = [("dwSize", _wt.DWORD), ("iType", _wt.DWORD),
                    ("nCount", _wt.DWORD), ("nRgnSize", _wt.DWORD),
                    ("rcBound", _wt.RECT)]

    def _region_bounds():
        """(bounds, ok) of the current window region, or (None, False)."""
        probe = ctypes.windll.gdi32.CreateRectRgn(0, 0, 0, 0)
        try:
            state = ctypes.windll.user32.GetWindowRgn(hwnd, probe)
            if state != 1 and state != 2:
                return None, False
            header = _RgnHeader()
            ctypes.windll.gdi32.GetRegionData(
                probe, ctypes.sizeof(header) + 4096, ctypes.byref(header))
            box = header.rcBound
            return (box.left, box.top, box.right, box.bottom), True
        finally:
            ctypes.windll.gdi32.DeleteObject(probe)

    last_rect = None
    for _ in range(60):
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            if width > 20 and height > 20:
                want = (0, 0, width, height)
                have, ok = _region_bounds()
                if not ok or have != want:
                    region = ctypes.windll.gdi32.CreateEllipticRgn(0, 0, width,
                                                                  height)
                    # SetWindowRgn takes ownership of the handle on success.
                    if ctypes.windll.user32.SetWindowRgn(hwnd, region, True):
                        applied += 1
                        clipped = True
            last_rect = (left, top, right, bottom)
        except Exception as error:
            print("[globe] clip failed: %s" % error, flush=True)
            break
        _t2.sleep(1.0)
    print("[globe] hwnd=%s clipped=%s applications=%s rect=%s" % (
        hwnd, clipped, applied, last_rect), flush=True)


def _url_quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")