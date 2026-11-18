"""Optional native presentation, attached only by the normal HUD lifecycle.

The cursor surface is input-transparent and owned by a single message-loop
thread. The keyboard-accessible top panel is an ordinary pywebview window.
No assistant process is launched by either surface.
"""
from pathlib import Path
from threading import Event, Thread
from threading import RLock, get_native_id
import hashlib
import math
import os
import time
from contextlib import contextmanager, nullcontext


class ActivityObservation(dict):
    """Serializable values with an in-process draw fence; attributes never go to the UI."""
    def __init__(self, values, interpreter, owner, generation):
        super().__init__(values)
        self.interpreter, self.owner, self.generation = interpreter, owner, generation

    @contextmanager
    def drawing(self):
        lock = getattr(self.interpreter, "companion_execution_lock", None)
        with lock if lock is not None else nullcontext():
            state = getattr(self.interpreter, "server_state", None) or {}
            valid = state.get("turn_ownership") is self.owner and state.get("turn_attempt") is self.generation
            active = self.get("state") in {"listening", "processing", "responding", "speaking", "guiding"}
            if active and (state.get("barge_in") or self.owner is not None and state.get("cancelled_owner") is self.owner):
                valid = False
            guide = self.get("guide")
            if guide is not None:
                from . import screen_overlay
                with screen_overlay._lock:
                    annotation = screen_overlay._active
                    valid = valid and annotation is not None and str(id(annotation)) == guide["key"] and annotation.get("deadline",0) > time.monotonic()
                    yield valid
            else:
                yield valid


def runtime_activity(interpreter, status_bus):
    """Passive local presentation from admitted ownership and observed activity only."""
    state = getattr(interpreter, "server_state", None) or {}
    owner, generation = state.get("turn_ownership"), state.get("turn_attempt")
    presenter = getattr(interpreter, "companion", None)
    observe = lambda value: ActivityObservation(value, interpreter, owner, generation)
    result = {"state": "idle", "label": "Ready when you are", "caption": "", "binding": None}
    if owner is not None and getattr(owner, "is_external", True):
        return observe(result)  # No phone/integration text leaks onto the local desktop.
    local = owner is not None and not getattr(owner, "is_external", True) and generation is not None
    stopped = owner is not None and (state.get("cancelled_owner") is owner or state.get("barge_in"))
    if stopped:
        return observe({**result, "label": "Stopped"})
    if local:
        result["binding"] = hashlib.sha256(
            f'{getattr(presenter,"epoch","")}:{id(owner)}:{id(generation)}'.encode()).hexdigest()[:20]
        if presenter is not None:
            result["caption"] = presenter.visible_caption(owner, generation)
            with presenter.lock:
                current = presenter.overlay
            if current is not None and current[0] is owner and current[1] is generation:
                from . import screen_overlay
                with screen_overlay._lock:
                    annotation = current[2]
                    if (screen_overlay._active is annotation
                            and annotation.get("deadline", 0) > time.monotonic()):
                        rect = annotation.get("rect") or []
                        if (len(rect) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in rect)
                                and rect[2] >= rect[0] and rect[3] >= rect[1]):
                            result["guide"] = {"key": str(id(annotation)), "x": rect[2],
                                "y": (rect[1]+rect[3])/2,
                                "label": str(annotation.get("label") or "This highlight")[:80]}
    try:
        snapshot = status_bus.snapshot()
        recent = 0 <= time.monotonic()-snapshot.updated_at <= 8
        thread = getattr(interpreter, "respond_thread", None)
        working = bool(local and thread is not None and thread.is_alive())
        if local and state.get("playback_generation") is generation:
            playback_owner = state.get("playback_owner")
            if playback_owner is owner:
                from .speech_playback import speech_snapshot
                speech = speech_snapshot(state)
                if speech.get("client_playback_observable") and speech.get("playback_recent") and not speech.get("stale"):
                    return observe({**result, "state": "speaking", "label": "Output client playing audio"})
        if result.get("guide"):
            return observe({**result, "state": "guiding", "label": "Showing the highlight"})
        if result["caption"]:
            return observe({**result, "state": "responding", "label": "Writing a response"})
        if recent and snapshot.state == "listening":
            return observe({**result, "state": "listening", "label": "Listening"})
        if working and snapshot.state in ("thinking", "speaking"):
            return observe({**result, "state": "processing", "label": "Working on your request"})
        if recent and snapshot.state == "error":
            return observe({**result, "state": "error", "label": "FRIDAY reported an error"})
        return observe(result)
    except Exception:
        return observe({**result, "state": "unknown", "label": "Activity unavailable", "caption": ""})


def buddy_surface(style, state, caption):
    """Inert pixels. Listening indicator is symbolic, never a fabricated waveform."""
    from PIL import Image, ImageDraw
    from .screen_overlay import _font
    mark = buddy_image(style)
    labels = {"listening": "Listening", "processing": "Working", "responding": "Response text",
              "speaking": "Client playing audio", "guiding": "Look here", "error": "Needs attention"}
    label = labels.get(state, "")
    caption = " ".join(str(caption or "").split())[:280]
    if not label and not caption:
        return mark
    font = _font(14)
    scratch = ImageDraw.Draw(mark)
    lines, line = [], ""
    # Character wrapping also bounds an unbroken URL/token without parsing it.
    for char in caption:
        if scratch.textlength(line+char, font=font) > 238:
            lines.append(line); line = ""
        line += char
        if len(lines) >= 6:
            break
    if line and len(lines) < 6:
        lines.append(line)
    image = Image.new("RGBA", (304, max(48, 42+18*len(lines))))
    image.alpha_composite(mark, (0, 6)); mark.close()
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((40, 1, 303, image.height-2), radius=13, fill="#173a31", outline="#b8cc9b", width=1)
    draw.text((51, 10), label or "FRIDAY", font=font, fill="#cfed87")
    for index, text in enumerate(lines):
        draw.text((51, 31+18*index), text, font=font, fill="#f7f4e9")
    return image


def _panel_handle(window):
    """Exact installed WinForms window, never a title-based global search."""
    import win32gui, win32process
    gui = window.gui
    if getattr(gui, "__name__", "") != "webview.platforms.winforms":
        raise RuntimeError("Companion window backend unavailable")
    form = gui.BrowserView.instances.get(window.uid)
    if form is None or form.pywebview_window is not window:
        raise RuntimeError("Companion window is not owned")
    hwnd = int(form.Handle.ToInt64())
    if not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != os.getpid():
        raise RuntimeError("Companion window is no longer owned")
    return hwnd


_PANEL_CALL_TIMEOUT = 2


def _panel_thread(window, operation):
    """Never synchronously drive a WinForms form from the polling/bridge thread.

    Queue onto its owner and bound the wait. A timed-out queued operation is
    invalidated, rather than surprising a newer UI state when it eventually runs.
    """
    form = window.gui.BrowserView.instances.get(window.uid)
    if form is None or form.pywebview_window is not window:
        raise RuntimeError("Companion window is not owned")
    if not getattr(form, "InvokeRequired", False):
        return operation()
    from System import Action
    completed, cancelled = Event(), Event()
    result = []
    def apply():
        if cancelled.is_set():
            completed.set()
            return
        try:
            result.append((True, operation()))
        except Exception:
            result.append((False, None))
        finally:
            completed.set()
    form.BeginInvoke(Action(apply))
    if not completed.wait(_PANEL_CALL_TIMEOUT):
        cancelled.set()
        raise TimeoutError("Companion window did not respond")
    if not result or not result[0][0]:
        raise RuntimeError("Companion window action failed")
    return result[0][1]


def show_panel(window, *, expanded=False):
    return _panel_thread(window, lambda: _show_panel_owned(window, expanded=expanded))


def _show_panel_owned(window, *, expanded=False):
    import win32gui, win32con
    hwnd = _panel_handle(window)
    window.focus = bool(expanded)
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    style = (style | win32con.WS_EX_TOOLWINDOW) & ~win32con.WS_EX_APPWINDOW
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE,
        style & ~win32con.WS_EX_NOACTIVATE if expanded else style | win32con.WS_EX_NOACTIVATE)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOW if expanded else win32con.SW_SHOWNOACTIVATE)
    if expanded:
        try:
            win32gui.SetForegroundWindow(hwnd)  # Deliberate user expansion only.
        except Exception:
            # Windows may refuse foreground focus; visible expansion still
            # succeeded. Do not corrupt compact focus policy on rollback.
            pass


def place_panel(window, expanded):
    return _panel_thread(window, lambda: _place_panel_owned(window, expanded))


def _place_panel_owned(window, expanded):
    """WinForms move() scales coordinates: use owned physical pixels consistently."""
    import win32api, win32gui, win32con
    hwnd = _panel_handle(window)
    form = window.gui.BrowserView.instances[window.uid]
    scale = float(form.scale_factor)
    if not math.isfinite(scale) or not .5 <= scale <= 4:
        raise RuntimeError("Companion scale unavailable")
    from .screen_overlay import _PerMonitorDPI
    with _PerMonitorDPI():
        left, top, right, bottom = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))["Work"]
        width, height = (360,480) if expanded else (184,52)
        width, height = min(round(width*scale), right-left-16), min(round(height*scale), bottom-top-16)
        if width < 100 or height < 48:
            raise RuntimeError("Companion display area unavailable")
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, int(left+(right-left-width)/2),
            int(top+8), width, height, win32con.SWP_NOACTIVATE)


def buddy_image(style):
    from .tray_assets import render_mark
    if style == "friday":
        return render_mark(32)
    if style not in ("leaf", "pointer", "bee"):
        raise ValueError("Unknown companion appearance")
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (32, 32))
    draw = ImageDraw.Draw(image)
    if style == "bee":
        draw.rectangle((6,12,23,23),fill="#cfed87")
        draw.rectangle((6,24,23,26),fill="#749f50")
        draw.rectangle((24,15,28,23),fill="#749f50")
        for x in (10,18):draw.rectangle((x,12,x+2,23),fill="#173a31")
        for x in (8,21):draw.rectangle((x,4,x+4,9),fill="#f7f4e9")
        draw.rectangle((25,16,26,18),fill="#173a31")
        draw.point((25,15),fill="#f7f4e9")
    elif style == "leaf":
        draw.ellipse((4, 2, 27, 29), fill="#173a31", outline="#f7f4e9", width=2)
        draw.line((7, 26, 23, 7), fill="#cfed87", width=2)
    else:
        draw.polygon([(3, 2), (26, 17), (16, 20), (12, 29)], fill="#173a31", outline="#f7f4e9", width=2)
    return image


def panel_position(screen, width):
    # Installed pywebview Screen exposes width/height/frame, not x/y.
    # Use Windows work-area metadata when available; no capture is performed.
    try:
        import win32api
        left, top, right, _bottom = win32api.GetMonitorInfo(
            win32api.MonitorFromPoint((0, 0), 1))["Work"]
        return int(left+(right-left-width)/2), int(top+8)
    except Exception:
        return max(0, int((screen.width-width)/2)), 8


class Win32Buddy:
    def __init__(self):
        self.hwnd = None
        self.style = None
        self.excluded = False

    def _owned(self):
        if not self.hwnd or getattr(self, "thread", None) != get_native_id():
            return False
        import win32gui, win32process
        return bool(win32gui.IsWindow(self.hwnd) and
            win32process.GetWindowThreadProcessId(self.hwnd) == (self.thread, os.getpid()))

    def pointer(self):
        import win32gui, win32api
        from .screen_overlay import _PerMonitorDPI
        with _PerMonitorDPI():
            if not win32gui.GetForegroundWindow():
                return None
            return win32api.GetCursorPos()

    def paint(self, x, y, style):
        return self.paint_activity(x, y, style, "idle", "")

    def paint_activity(self, x, y, style, state, caption):
        import ctypes
        from ctypes import wintypes
        import win32api, win32con, win32gui
        from . import screen_overlay as overlay
        with overlay._PerMonitorDPI():
            if self.hwnd is not None and not self._owned():
                self.hwnd, self.style = None, None
                raise RuntimeError("Cursor window ownership changed")
            if self.hwnd is None:
                self.hwnd = win32gui.CreateWindowEx(
                    win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT |
                    win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TOPMOST,
                    "Static", "FRIDAY Cursor Buddy", win32con.WS_POPUP, 0, 0, 32, 32, 0, 0, 0, None)
                self.thread = get_native_id()
                setter, getter = ctypes.windll.user32.SetWindowDisplayAffinity, ctypes.windll.user32.GetWindowDisplayAffinity
                setter.argtypes, setter.restype = [wintypes.HWND, wintypes.DWORD], wintypes.BOOL
                getter.argtypes, getter.restype = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL
                value = wintypes.DWORD()
                self.excluded = bool(setter(self.hwnd, 0x11) and getter(self.hwnd, ctypes.byref(value)) and value.value == 0x11)
            if not self.excluded:
                raise RuntimeError("Cursor capture exclusion is unavailable")
            screen = win32api.GetMonitorInfo(win32api.MonitorFromPoint((int(x), int(y)), 2))["Monitor"]
            signature = (style, state, caption)
            if self.style != signature:
                image = buddy_surface(style, state, caption)
                width, height = image.size
                left = max(screen[0], min(int(x)+20, screen[2]-width-4))
                top = max(screen[1], min(int(y)+20, screen[3]-height-4))
                pixels = overlay._premultiply(image)
                try:
                    failure = overlay._render(self.hwnd, left, top, width, height, "rect", "", (0, 0, 0), False,
                                              image=pixels)
                    if failure:
                        raise RuntimeError("Cursor rendering unavailable")
                finally:
                    pixels.close()
                    image.close()
                self.style, self.size = signature, (width, height)
            width, height = self.size
            left = max(screen[0], min(int(x)+20, screen[2]-width-4))
            top = max(screen[1], min(int(y)+20, screen[3]-height-4))
            win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOPMOST, left, top, width, height,
                                 win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
            win32gui.PumpWaitingMessages()

    def hide(self):
        if self._owned():
            import win32gui, win32con
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)

    def close(self):
        if self._owned():
            import win32gui
            win32gui.DestroyWindow(self.hwnd)
        self.hwnd = self.style = None


class BarBridge:
    def __init__(self, presenter):
        self._presenter = presenter

    def get_snapshot(self):
        snapshot = self._presenter.snapshot()
        owner = getattr(self._presenter, "_native_controller", None)
        activity = owner.activity() if owner is not None else {"state":"unknown", "label":"Activity unavailable", "caption":""}
        fence = activity.drawing() if isinstance(activity, ActivityObservation) else nullcontext(True)
        with fence as valid:
            activity = dict(activity) if valid else {"state":"unknown", "label":"Activity changed", "caption":""}
        native = {key: value for key, value in snapshot["native"].items() if key != "activity"}
        if owner is not None:
            with owner.lock:
                native.update(panel_expanded=owner.expanded,panel_pinned=owner.pinned)
        return {"unread": sum(not row["seen"] and row["content"]["kind"] == "notification"
                              for row in snapshot["rows"]), "native": native,
                "activity": activity,
                "appearance": snapshot['preferences'].get('values',{}).get('style','friday')}

    def set_expanded(self, expanded, pinned=False):
        owner = getattr(self._presenter, "_native_controller", None)
        return owner.set_expanded(expanded, pinned) if owner is not None else {"ok": False}

    def open(self, tab="home"):
        return self._presenter.open(tab)

    def dismiss(self):
        snapshot = self._presenter.preferences()
        return self._presenter.save({"top_panel": False}, snapshot.get("revision"))


class SurfaceBridge(BarBridge):
    def __init__(self, presenter):
        super().__init__(presenter)
        self._hide = None

    def get_companion(self):
        return self._presenter.snapshot()

    def set_companion(self, changes, revision):
        return self._presenter.save(changes, revision)

    def acknowledge_companion(self, identifier):
        return self._presenter.acknowledge(identifier)

    def hide_companion_surface(self):
        if self._hide is None:
            return {"ok": False}
        self._hide()
        return {"ok": True}


class Controller:
    def __init__(self, presenter, *, bar, visible, buddy_factory=Win32Buddy, cue=lambda: False,
                 surface=None, on_open=lambda: False, activity=None, position=None, present=None, clock=time.monotonic,
                 surface_present=None, panel_hide=None, dispose_window=None):
        self.presenter, self.bar, self.visible = presenter, bar, visible
        self.buddy_factory, self.cue = buddy_factory, cue
        self.buddy = None
        self.stop_event = Event()
        self.thread = None
        self.position = None
        self.bar_visible = None
        self.surface, self.on_open = surface, on_open
        self.surface_present=surface_present or (surface.show if surface is not None else lambda:None)
        self.panel_hide=panel_hide or (lambda:bar.hide())
        self.dispose_window=dispose_window or (lambda window:window.destroy())
        self.stopped = False
        self.activity = activity or (lambda: {"state":"unknown", "label":"Activity unavailable", "caption":""})
        self.place, self.present, self.clock = position, present, clock
        self.expanded = self.pinned = False
        self.lock = RLock()
        from .companion_motion import BuddyMotion
        self.motion = BuddyMotion()

    def set_expanded(self, expanded, pinned=False):
        if type(expanded) is not bool or type(pinned) is not bool or pinned and not expanded:
            return {"ok": False, "message": "Invalid companion panel choice."}
        with self.lock:
            if self.stopped or self.place is None:
                return {"ok": False, "message": "Companion panel unavailable."}
            preferences = self.presenter.preferences()
            show = preferences.get("available") and preferences["values"]["top_panel"] and not self.visible()
            if expanded and not show:
                return {"ok": False, "message": "The companion panel is hidden or disabled. Open it again after hiding FRIDAY."}
            before = self.expanded
            try:
                self.place(expanded)
                preferences = self.presenter.preferences()
                show = preferences.get("available") and preferences["values"]["top_panel"] and not self.visible()
                if expanded and not show:
                    raise RuntimeError("Panel visibility changed")
                if self.present is not None and show:
                    self.present(expanded)
                elif not show:
                    self.panel_hide()
            except Exception:
                try:
                    self.place(before)
                    if self.present is not None and show:
                        self.present(before)
                    elif not show:
                        self.panel_hide()
                except Exception:
                    pass
                return {"ok": False, "message": "The companion panel could not change. Your saved preferences are unchanged."}
            self.expanded, self.pinned = expanded, pinned
            return {"ok": True, "expanded": expanded, "pinned": pinned}

    def open(self, tab):
        if self.stop_event.is_set():
            return False
        if tab == "home":
            return self.on_open() is True
        if self.surface is None:
            return False
        try:
            self.surface_present()
        except Exception:
            return False
        return True

    def start(self, *args):
        if self.thread is None and not self.stopped:
            self.thread = Thread(target=self._run, daemon=True, name="friday-cursor-buddy")
            self.thread.start()

    def tick(self):
        snapshot = self.presenter.preferences()
        if not snapshot["available"]:
            if self.buddy:
                self.buddy.hide()
            with self.lock:
                self.panel_hide()
                self.bar_visible = False
            self.presenter.native = {"available": False, "state": "preferences_unavailable"}
            return
        values = snapshot["values"]
        if self.surface is not None:
            presentation = self.presenter.take_presentation()
            if presentation is not None and values["teaching"]:
                # The native thread consumes only a bounded display request;
                # it never calls providers or replays the assistant turn.
                self.presenter.open("lessons")
        with self.lock:
            current = self.presenter.preferences()
            show_bar = current.get("available") and current["values"]["top_panel"] and not self.visible()
            if show_bar != self.bar_visible:
                if show_bar:
                    if self.place is not None:
                        self.place(False)
                    if self.present is not None:
                        self.present(False)
                    else:
                        self.bar.show()
                else:
                    self.panel_hide()
                    if self.expanded:
                        if self.place is not None:
                            self.place(False)
                        self.expanded = self.pinned = False
                self.bar_visible = show_bar
        try:
            activity = self.activity()
            if not isinstance(activity, dict):
                raise ValueError("Unavailable activity")
        except Exception:
            activity = {"state":"unknown", "label":"Activity unavailable", "caption":""}
        active = activity.get("state") in {"listening","processing","responding","speaking","guiding"}
        show_buddy = values["cursor"] == "always" or values["cursor"] == "when_asked" and (self.cue() or active)
        if show_buddy:
            if self.buddy is None:
                self.buddy = self.buddy_factory()
            point = self.buddy.pointer()
            if point is None:
                self.buddy.hide()
            else:
                self.position = self.motion.step(point, activity.get("guide"), activity.get("binding"),
                    self.clock(), reduced=values["reduced_motion"])
                caption = activity.get("caption", "")
                if self.motion.phase in ("flying", "pointing") and activity.get("guide"):
                    caption = activity["guide"].get("label", "")
                fence = activity.drawing() if isinstance(activity, ActivityObservation) else nullcontext(True)
                with fence as valid:
                    if not valid:
                        self.buddy.hide()
                        self.presenter.native = {"available": True, "state": "activity_changed", "activity": {"state":"unknown","label":"Activity changed","caption":""}}
                        return
                    if hasattr(self.buddy, "paint_activity"):
                        self.buddy.paint_activity(*self.position, values["style"], activity.get("state", "idle"), caption)
                    else:
                        self.buddy.paint(*self.position, values["style"])
        elif self.buddy:
            self.buddy.close()
            self.buddy = None
            self.position = None
        self.presenter.native = {"available": True, "state": "attached", "top_panel_visible": show_bar,
                                 "panel_expanded": self.expanded, "panel_pinned": self.pinned,
                                 "activity": dict(activity), "motion": self.motion.phase,
                                 "cursor_requested": bool(show_buddy),
                                 "capture_excluded": self.buddy.excluded if self.buddy else None}

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    self.presenter.native = {"available": False, "state": "presentation_unavailable",
                        "message": "The companion could not be displayed. Check Windows overlay support; capture protection remains required."}
                    if self.buddy:
                        self.buddy.hide()
                self.stop_event.wait(.04 if self.buddy else .5)
        finally:
            if self.buddy:
                self.buddy.close()
            self.presenter.native = {"available": False, "state": "stopped"}

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.presenter.action = None
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        for window in (self.bar, self.surface):
            if window is not None:
                try:
                    self.dispose_window(window)
                except Exception:
                    pass
        if getattr(self.presenter, "_native_controller", None) is self:
            self.presenter._native_controller = None


def attach(presenter, webview, *, on_open, visible, start_hidden=False, activity=None):
    """Create through the shipped pywebview owner; never from a polling read."""
    existing = getattr(presenter, "_native_controller", None)
    if existing is not None:
        return existing
    from . import screen_overlay
    path = Path(__file__).resolve().parent / "ui" / "companion-bar.html"
    screen = webview.screens[0]
    width = 184
    x, y = panel_position(screen, width)
    bar = webview.create_window("FRIDAY Quick Panel", path.as_uri(), js_api=BarBridge(presenter),
        width=width, height=52, x=x, y=y,
        frameless=True, easy_drag=False, resizable=False, on_top=True,
        hidden=True, min_size=(160, 48), background_color="#173a31", focus=False)
    surface_bridge = SurfaceBridge(presenter)
    try:
        surface = webview.create_window("FRIDAY Teaching Notes", path.with_name("companion-surface.html").as_uri(),
            js_api=surface_bridge, width=560, height=680, min_size=(320, 360), hidden=True,
            background_color="#f7f4e9", on_top=True)
    except Exception:
        bar.destroy()
        raise
    surface_bridge._hide = lambda:_panel_thread(surface,surface.hide)
    def cue():
        if screen_overlay.is_active():
            return True
        return any(row["content"]["kind"] == "lesson" and row["age_seconds"] < 30
                   for row in presenter.snapshot()["rows"])
    def place(expanded):
        place_panel(bar, expanded)
    controller = Controller(presenter, bar=bar, visible=visible, cue=cue, surface=surface, on_open=on_open,
        activity=activity, position=place, present=lambda expanded: show_panel(bar, expanded=expanded),
        surface_present=lambda:_panel_thread(surface,surface.show),
        panel_hide=lambda:_panel_thread(bar,bar.hide),
        dispose_window=lambda window:_panel_thread(window,window.destroy))
    bar.events.loaded += controller.start
    def closing():
        if controller.stop_event.is_set():
            return True
        controller.set_expanded(False)
        return False  # collapse is not a preference change or assistant Exit
    bar.events.closing += closing
    def surface_closing():
        if controller.stop_event.is_set():
            return True
        surface.hide()
        return False
    surface.events.closing += surface_closing
    presenter.action = controller.open
    presenter._native_controller = controller
    return controller
