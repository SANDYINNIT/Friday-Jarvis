"""Field Notes Windows chrome. Importing this module creates no native objects.

pywebview 5.2's WinForms backend has no window-icon option. Adapt only its
exact BrowserForm, on its UI thread; leave installed dependencies untouched.
"""
import os
import threading

APP_USER_MODEL_ID = "FRIDAY.FieldNotes.Desktop"


def prepare_taskbar_identity():
    """Process-scoped grouping, before any webview form; no registry writes."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        return setter(APP_USER_MODEL_ID) == 0
    except Exception:
        return False


def _load_form_icon():
    """An independently owned .NET Icon; fall back to the same approved mark."""
    from System.Drawing import Icon
    from .tray_assets import ICON_PATH, load_tray_image

    try:
        return Icon(str(ICON_PATH))
    except Exception:
        from io import BytesIO
        from System import Array, Byte
        from System.IO import MemoryStream

        with load_tray_image() as image, BytesIO() as buffer:
            image.save(buffer, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
            stream = MemoryStream(Array[Byte](buffer.getvalue()))
        try:
            original = Icon(stream)
            try:
                return original.Clone()
            finally:
                original.Dispose()
        finally:
            stream.Dispose()


def _remove_caption(hwnd, gui, con):
    """Keep native sizing/system-menu/min/max styles, remove only caption.

    Keep a real non-client resize frame instead of pywebview frameless=True
    (WinForms FormBorderStyle.None removes its resize edges). Frame refresh
    recomputes the client area. If it fails, restore the previous style.
    """
    previous = gui.GetWindowLong(hwnd, con.GWL_STYLE)
    if not previous & con.WS_THICKFRAME:
        raise RuntimeError("Resizable native frame unavailable")
    try:
        gui.SetWindowLong(hwnd, con.GWL_STYLE, previous & ~con.WS_CAPTION)
        gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                         con.SWP_NOMOVE | con.SWP_NOSIZE | con.SWP_NOZORDER
                         | con.SWP_NOACTIVATE | con.SWP_FRAMECHANGED)
    except Exception:
        gui.SetWindowLong(hwnd, con.GWL_STYLE, previous)
        gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                         con.SWP_NOMOVE | con.SWP_NOSIZE | con.SWP_NOZORDER
                         | con.SWP_NOACTIVATE | con.SWP_FRAMECHANGED)
        raise


class _WinFormsWindow:
    def __init__(self, window):
        gui = window.gui
        if getattr(gui, "__name__", "") != "webview.platforms.winforms":
            raise RuntimeError("Window backend not supported")
        self._instances = gui.BrowserView.instances
        self._uid = window.uid
        self._form = self._instances.get(self._uid)
        if self._form is None or self._form.pywebview_window is not window:
            raise RuntimeError("Window backend not ready")
        self._hwnd = None
        self._icon = None
        self.branded = False

    @property
    def hwnd(self):
        return self._hwnd

    def install(self, on_hide):
        from System import Action
        from System.Windows.Forms import CloseReason
        import win32con as con
        import win32gui as gui

        def apply():
            self._hwnd = int(self._form.Handle.ToInt64())
            self.require_owned()

            def closing(_sender, args):
                if args.CloseReason == CloseReason.UserClosing:
                    args.Cancel = True
                    try:
                        self.require_owned()
                        on_hide(self._hwnd)
                    except Exception:
                        print("[window] Close could not hide the HUD.", flush=True)

            self._form.FormClosing += closing
            try:
                icon = _load_form_icon()
                try:
                    self._form.Icon = icon  # WinForms updates window/taskbar icons.
                except Exception:
                    icon.Dispose()
                    raise
                self._icon = icon
                self.branded = True
            except Exception:
                # Branding failure must not stop the assistant/window.
                print("[window] FRIDAY window icon unavailable; existing icon retained.", flush=True)
            _remove_caption(self._hwnd, gui, con)

        self._form.Invoke(Action(apply))

    def require_owned(self):
        import win32gui
        import win32process
        if (not self._hwnd or self._instances.get(self._uid) is not self._form
                or not win32gui.IsWindow(self._hwnd)
                or win32process.GetWindowThreadProcessId(self._hwnd)[1] != os.getpid()):
            raise RuntimeError("Window is no longer owned")

    def maximized(self):
        import win32con as con
        import win32gui as gui
        self.require_owned()
        return gui.GetWindowPlacement(self._hwnd)[1] == con.SW_SHOWMAXIMIZED

    def act(self, action):
        import win32con as con
        import win32gui as gui
        self.require_owned()
        if action == "minimize":
            gui.ShowWindow(self._hwnd, con.SW_MINIMIZE)
        elif action == "toggle_maximize":
            gui.ShowWindow(self._hwnd, con.SW_RESTORE if self.maximized() else con.SW_MAXIMIZE)
        elif action == "drag":
            from System import Action
            import win32api

            def start_move():
                self.require_owned()
                # Ignore a delayed bridge request after the mouse is released.
                if not self.maximized() and win32api.GetAsyncKeyState(con.VK_LBUTTON) & 0x8000:
                    gui.ReleaseCapture()
                    gui.PostMessage(self._hwnd, con.WM_SYSCOMMAND, con.SC_MOVE | con.HTCAPTION, 0)

            self._form.Invoke(Action(start_move))

    def dispose(self):
        if self._icon is not None:
            self._icon.Dispose()
            self._icon = None


class WindowControls:
    """One exact HUD lifetime; stale bridge objects cannot control its successor."""
    def __init__(self, window, on_hide, backend_factory=None):
        self._window = window
        self._on_hide = on_hide
        self._factory = backend_factory or _WinFormsWindow
        self._backend = None
        self._closed = False
        self._integrated = False
        self._install_started = False
        self._lock = threading.RLock()

    def attach(self):
        self._window.events.shown += self._shown
        self._window.events.closed += self._closed_event
        # create_window() on a running 5.2 backend may return after Shown.
        if self._window.events.shown.is_set():
            self._shown()
        return self

    def _shown(self):
        with self._lock:
            if self._closed or self._install_started:
                return
            self._install_started = True
        backend = None
        integrated = False
        try:
            backend = self._factory(self._window)
            backend.install(self._on_hide)
            integrated = True
        except Exception:
            print("[window] Integrated header unavailable; native frame retained.", flush=True)
        with self._lock:
            if not self._closed:
                # A successfully assigned Icon remains alive even if caption
                # adaptation fails. Dispose it only on actual form closure.
                self._backend = backend
                self._integrated = integrated
                return
        if backend is not None:
            backend.dispose()

    def _closed_event(self):
        with self._lock:
            self._closed = True
            self._integrated = False
            backend, self._backend = self._backend, None
        if backend is not None:
            backend.dispose()

    def snapshot(self):
        with self._lock:
            backend = self._backend if not self._closed and self._integrated else None
        if backend is None:
            return {"available": False, "integrated": False, "maximized": None}
        try:
            backend.require_owned()
        except Exception:
            return {"available": False, "integrated": False, "maximized": None}
        try:
            maximized = backend.maximized()
        except Exception:
            maximized = None
        return {"available": True, "integrated": True, "maximized": maximized,
                "branded": backend.branded}

    def action(self, action):
        if not isinstance(action, str) or action not in {"minimize", "toggle_maximize", "close", "drag"}:
            return {"ok": False, "message": "Unsupported window action."}
        with self._lock:
            backend = self._backend if not self._closed and self._integrated else None
        if backend is None:
            return {"ok": False, "message": "Window controls are unavailable."}
        try:
            backend.require_owned()
            if action == "close":
                if not self._on_hide(backend.hwnd):
                    return {"ok": False, "message": "Window could not be hidden."}
            else:
                backend.act(action)
            return {"ok": True}
        except Exception:
            return {"ok": False, "message": "Window action could not be completed."}


def bind_window_controls(window, bridge, on_hide):
    """No backend import/Win32 work until this exact window's shown event."""
    controls = WindowControls(window, on_hide).attach()
    bridge._window_controls = controls
    return controls
