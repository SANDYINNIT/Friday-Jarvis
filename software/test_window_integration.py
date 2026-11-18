"""Owned native-window behavior with fake WinForms/Win32, never a real window."""
from types import SimpleNamespace
import sys

import pytest

from source.server import status_ui as ui
from source.server import window_integration as integration


class Event:
    def __init__(self):
        self.handlers = []
        self.fired = False

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        self.fired = True
        return [handler() for handler in self.handlers]

    def is_set(self):
        return self.fired


class Backend:
    def __init__(self, window):
        self.window = window
        self.hwnd = window.handle
        self.branded = True
        self.calls = []
        self.is_max = False
        self.owned = True
        self.disposals = 0

    def install(self, on_hide):
        self.calls.append("install")

    def require_owned(self):
        if not self.owned:
            raise RuntimeError("untrusted/private/native failure")

    def maximized(self):
        return self.is_max

    def act(self, action):
        self.calls.append(action)
        if action == "toggle_maximize":
            self.is_max = not self.is_max

    def dispose(self):
        self.disposals += 1


def window(handle=77):
    return SimpleNamespace(handle=handle, events=SimpleNamespace(
        shown=Event(), closing=Event(), closed=Event()))


def test_real_bridge_controls_are_passive_until_exact_shown_window(monkeypatch):
    monkeypatch.setitem(sys.modules, "source.server.lab_authority", SimpleNamespace(shared=lambda: None))
    monkeypatch.setitem(sys.modules, "source.server.lab_budget", SimpleNamespace(shared=lambda: None))
    effects = []
    interpreter = SimpleNamespace(server_state={"is_paused": True, "active_turn": "unchanged"})
    bridge = ui.WebviewBridge(ui.StatusBus(), interpreter)
    assert bridge.get_window_state()["available"] is False
    assert bridge.window_action("close")["ok"] is False
    made = []
    def factory(target):
        made.append(Backend(target))
        return made[-1]
    monkeypatch.setattr(integration, "_WinFormsWindow", factory)
    target = window()
    controls = integration.bind_window_controls(target, bridge, lambda hwnd: effects.append(hwnd) or True)
    for _ in range(3):
        assert not bridge.get_window_state()["available"]
    assert made == [] and effects == []
    target.events.shown.fire(); target.events.shown.fire()
    assert len(made) == 1 and made[0].calls == ["install"]
    assert bridge.get_window_state() == {"available": True, "integrated": True,
                                        "maximized": False, "branded": True,
                                        "opening_revision": ui._WINDOW_OPEN_REVISION}
    assert bridge.window_action("minimize") == {"ok": True}
    assert bridge.window_action("toggle_maximize")["ok"]
    assert bridge.get_window_state()["maximized"] is True
    assert bridge.window_action("toggle_maximize")["ok"]
    assert bridge.window_action("drag")["ok"]
    assert bridge.window_action("close")["ok"]
    assert effects == [77]
    assert interpreter.server_state == {"is_paused": True, "active_turn": "unchanged"}
    assert controls is bridge._window_controls


def test_open_presentation_revision_is_passive_and_does_not_change_work(monkeypatch):
    monkeypatch.setattr(ui, "_WINDOW_OPEN_REVISION", 10)
    monkeypatch.setattr(ui, "_WINDOW_OPENING", None)
    monkeypatch.setitem(sys.modules, "source.server.lab_authority", SimpleNamespace(shared=lambda: None))
    monkeypatch.setitem(sys.modules, "source.server.lab_budget", SimpleNamespace(shared=lambda: None))
    interpreter = SimpleNamespace(server_state={"active_turn": "original", "is_paused": True})
    bridge = ui.WebviewBridge(ui.StatusBus(), interpreter)
    bridge._window_controls = SimpleNamespace(snapshot=lambda: {"available": True, "integrated": True})
    for _ in range(3):
        assert bridge.get_window_state()["opening_revision"] == 10
    ui.note_window_open()
    assert bridge.get_window_state()["opening_revision"] == 11
    ui.note_window_open()
    assert bridge.get_window_state()["opening_revision"] == 12
    assert interpreter.server_state == {"active_turn": "original", "is_paused": True}


def test_native_opening_waits_for_load_and_exact_visible_owner(monkeypatch):
    """Actual binding/bridge path, with only native APIs and form replaced."""
    monkeypatch.setattr(ui, "_WINDOW_OPENING", None)
    monkeypatch.setitem(sys.modules, "source.server.lab_authority", SimpleNamespace(shared=lambda: None))
    monkeypatch.setitem(sys.modules, "source.server.lab_budget", SimpleNamespace(shared=lambda: None))
    state = {"visible": False, "iconic": False, "pid": ui.os.getpid()}
    calls = []
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        IsWindowVisible=lambda handle: calls.append(handle) or state["visible"],
        IsIconic=lambda handle: state["iconic"]))
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=lambda handle: (1, state["pid"])))
    target = window()
    target.events.loaded = Event()
    target.uid = "synthetic-window"
    form = SimpleNamespace(pywebview_window=target, Handle=SimpleNamespace(ToInt64=lambda: 77))
    target.gui = SimpleNamespace(BrowserView=SimpleNamespace(instances={target.uid: form}))
    work = {"active_turn": "original", "is_paused": True}
    bridge = ui.WebviewBridge(ui.StatusBus(), SimpleNamespace(server_state=work))
    bridge._window_controls = SimpleNamespace(snapshot=lambda: {"available": True, "integrated": True})
    opening = ui._bind_window_opening(target, bridge)
    target.events.shown.fire()  # WinForms hidden bootstrap's transient Shown.
    assert bridge.get_window_state()["opening"]["revision"] == 0
    assert calls == []
    target.events.loaded.fire()
    first = bridge.get_window_state()["opening"]
    assert first == {"epoch": opening.epoch, "revision": 0, "ready": True, "visible": False}
    state["visible"] = True
    assert bridge.get_window_state()["opening"]["revision"] == 1
    assert bridge.get_window_state()["opening"]["revision"] == 1
    ui.note_window_open()
    assert bridge.get_window_state()["opening"]["revision"] == 1
    state["iconic"] = True
    assert bridge.get_window_state()["opening"]["visible"] is False
    state["iconic"] = False
    assert bridge.get_window_state()["opening"]["revision"] == 2
    state["pid"] = -1
    assert bridge.get_window_state()["opening"]["visible"] is None
    state["pid"] = ui.os.getpid()
    assert bridge.get_window_state()["opening"]["revision"] == 2
    target.events.closed.fire()
    ui.note_window_open()
    assert bridge.get_window_state()["opening"]["ready"] is False
    assert bridge.get_window_state()["opening"]["revision"] == 2
    assert work == {"active_turn": "original", "is_paused": True}


def test_opening_epochs_and_stale_close_are_independent():
    old = ui.WindowOpening(lambda: True)
    old.loaded()
    new = ui.WindowOpening(lambda: True)
    new.loaded()
    assert old.epoch != new.epoch
    old.close()
    assert new.snapshot() == {"epoch": new.epoch, "revision": 1, "ready": True, "visible": True}
    assert old.snapshot()["revision"] == 1


def test_opening_loaded_uses_actual_installed_event_dispatcher(caplog):
    from webview.event import Event as NativeEvent
    import threading
    opening=ui.WindowOpening(lambda:True)
    event=NativeEvent(SimpleNamespace(),should_lock=True)
    event += opening.loaded
    event.set()
    assert opening.snapshot()["revision"]==1
    assert "unhashable" not in caplog.text
    assert opening.loaded() is None


def test_opening_receipts_are_exact_bounded_and_sanitized(capsys):
    opening=ui.WindowOpening(lambda:True)
    opening.loaded()
    for _ in range(3):assert opening.acknowledge(opening.epoch,1,"playing")["ok"]
    assert not opening.acknowledge("old",1,"playing")["ok"]
    assert not opening.acknowledge(opening.epoch,0,"complete")["ok"]
    assert not opening.acknowledge(opening.epoch,1,"private text")["ok"]
    assert capsys.readouterr().out=="[window-opening] revision=1 outcome=playing\n"
    opening.close()
    assert not opening.acknowledge(opening.epoch,1,"complete")["ok"]


@pytest.mark.parametrize("action", ["exit", "restart", "shell", "resize", None, {}, True])
def test_unsupported_actions_cannot_reach_backend(action):
    controls = integration.WindowControls(window(), lambda hwnd: pytest.fail("hide called"), Backend).attach()
    controls._window.events.shown.fire()
    assert controls.action(action)["ok"] is False
    assert controls._backend.calls == ["install"]


def test_closed_or_replaced_owner_cannot_affect_new_window():
    effects = []
    old = integration.WindowControls(window(1), lambda hwnd: effects.append(hwnd) or True, Backend).attach()
    new = integration.WindowControls(window(2), lambda hwnd: effects.append(hwnd) or True, Backend).attach()
    old._window.events.shown.fire(); new._window.events.shown.fire()
    retired = old._backend
    old._window.events.closed.fire(); old._window.events.closed.fire()
    old._window.events.shown.fire()
    assert retired.disposals == 1
    for action in ("close", "drag", "toggle_maximize", "minimize"):
        assert not old.action(action)["ok"]
    assert effects == [] and not old.snapshot()["available"]
    assert new.action("close")["ok"] and effects == [2]
    new._backend.owned = False
    assert not new.action("close")["ok"]
    assert not new.snapshot()["available"] and effects == [2]


def test_failed_caption_adaptation_retains_native_fallback_and_icon_until_close():
    class Broken(Backend):
        def install(self, on_hide):
            raise RuntimeError("private endpoint must not be displayed")
    target = window()
    calls = []
    controls = integration.WindowControls(target, lambda hwnd: calls.append(hwnd), Broken).attach()
    target.events.shown.fire()
    backend = controls._backend
    assert backend.disposals == 0  # form may still own a successfully assigned icon
    assert not controls.snapshot()["available"]
    assert not controls.action("minimize")["ok"]
    assert calls == []
    target.events.closed.fire()
    assert backend.disposals == 1


def test_failed_hide_is_not_success_and_already_shown_windows_are_supported():
    target = window()
    target.events.shown.fire()
    controls = integration.WindowControls(target, lambda hwnd: False, Backend).attach()
    assert controls.snapshot()["available"]
    assert not controls.action("close")["ok"]


@pytest.fixture
def win32(monkeypatch):
    calls = []
    state = {"style": 0x00CF0000, "max": False, "owned": True, "mouse_down": True}
    con = SimpleNamespace(GWL_STYLE=-16, WS_CAPTION=0xC00000, WS_THICKFRAME=0x40000,
        SWP_NOMOVE=2, SWP_NOSIZE=1, SWP_NOZORDER=4, SWP_NOACTIVATE=16, SWP_FRAMECHANGED=32,
        SW_SHOWMAXIMIZED=3, SW_MINIMIZE=6, SW_RESTORE=9, SW_MAXIMIZE=3,
        WM_SYSCOMMAND=0x112, SC_MOVE=0xF010, HTCAPTION=2, VK_LBUTTON=1)
    def set_style(hwnd, slot, style):
        calls.append(("style", hwnd, style)); state["style"] = style
    def show(hwnd, action):
        calls.append(("show", hwnd, action)); state["max"] = action == con.SW_MAXIMIZE
    gui = SimpleNamespace(GetWindowLong=lambda *_: state["style"], SetWindowLong=set_style,
        SetWindowPos=lambda *args: calls.append(("frame", *args)), IsWindow=lambda hwnd: state["owned"],
        GetWindowPlacement=lambda hwnd: (0, 3 if state["max"] else 1), ShowWindow=show,
        ReleaseCapture=lambda: calls.append(("release",)), PostMessage=lambda *args: calls.append(("post", *args)))
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32con", con)
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(GetAsyncKeyState=lambda key: 0x8000 if state["mouse_down"] else 0))
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, integration.os.getpid())))
    return gui, con, state, calls


def test_actual_winforms_adapter_sets_form_icon_and_retains_native_resize(win32, monkeypatch):
    gui, con, state, calls = win32
    original = state["style"]
    target = SimpleNamespace(uid="exact")
    form = SimpleNamespace(pywebview_window=target, Handle=SimpleNamespace(ToInt64=lambda: 77),
        Icon="Python", Invoke=lambda action: action(), FormClosing=Event())
    target.gui = SimpleNamespace(__name__="webview.platforms.winforms", BrowserView=SimpleNamespace(instances={"exact": form}))
    icon = SimpleNamespace(Dispose=lambda: calls.append(("dispose-icon",)))
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda callback: callback))
    monkeypatch.setitem(sys.modules, "System.Windows.Forms", SimpleNamespace(CloseReason=SimpleNamespace(UserClosing=3)))
    monkeypatch.setattr(integration, "_load_form_icon", lambda: icon)
    backend = integration._WinFormsWindow(target)
    hidden = []
    backend.install(lambda hwnd: hidden.append(hwnd))
    assert form.Icon is icon and backend.branded
    assert state["style"] == original & ~con.WS_CAPTION
    assert state["style"] & con.WS_THICKFRAME
    assert calls[1][0] == "frame" and calls[1][-1] & con.SWP_FRAMECHANGED
    backend.act("minimize"); backend.act("toggle_maximize")
    assert backend.maximized()
    before = list(calls)
    backend.act("drag")
    assert calls == before  # maximized frame cannot be dragged offscreen
    backend.act("toggle_maximize"); backend.act("drag")
    assert calls[-1] == ("post", 77, con.WM_SYSCOMMAND, con.SC_MOVE | con.HTCAPTION, 0)
    before = list(calls)
    state["mouse_down"] = False
    backend.act("drag")
    assert calls == before
    closing, = form.FormClosing.handlers
    for reason, expected in ((3, True), (1, False), (6, False)):
        args = SimpleNamespace(CloseReason=reason, Cancel=False)
        closing(form, args)
        assert args.Cancel is expected
    assert hidden == [77]
    backend.dispose(); backend.dispose()
    assert calls.count(("dispose-icon",)) == 1


def test_frame_refresh_failure_restores_original_caption(win32):
    gui, con, state, calls = win32
    original = state["style"]
    def refresh(*args):
        if state["style"] != original:
            raise RuntimeError("injected frame failure")
    gui.SetWindowPos = refresh
    with pytest.raises(RuntimeError, match="frame failure"):
        integration._remove_caption(77, gui, con)
    assert state["style"] == original


def test_geometry_skips_extremes_and_still_persists_normal_rect(win32, monkeypatch):
    gui, con, state, calls = win32
    saved = []
    gui.IsIconic = lambda hwnd: False
    gui.GetWindowRect = lambda hwnd: (10, 20, 1010, 720)
    monkeypatch.setattr(ui, "_save_hud_geometry", lambda value: saved.append(value) or True)
    normal = ui._hud_geometry_tick(77, None)
    assert normal == {"x": 10, "y": 20, "width": 1000, "height": 700}
    state["max"] = True
    assert ui._hud_geometry_tick(77, normal) is normal
    gui.IsIconic = lambda hwnd: True
    state["max"] = False
    assert ui._hud_geometry_tick(77, normal) is normal
    assert saved == [normal]


def test_process_grouping_is_explicit_local_and_failure_safe(monkeypatch):
    import ctypes
    values = []
    class Setter:
        def __call__(self, value):
            values.append(value)
            return 0
    setter = Setter()
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=setter)))
    assert integration.prepare_taskbar_identity()
    assert values == ["FRIDAY.FieldNotes.Desktop"]
    monkeypatch.setattr(ctypes, "windll", None)
    assert integration.prepare_taskbar_identity() is False


@pytest.mark.parametrize("asset_state", ["valid", "missing", "corrupt"])
def test_form_icon_decodes_approved_asset_or_memory_fallback_from_any_cwd(tmp_path, monkeypatch, asset_state):
    from PIL import Image
    from io import BytesIO
    from source.server import tray_assets
    made = []
    class Stream:
        def __init__(self, data): self.data=bytes(data); self.disposed=False
        def Dispose(self): self.disposed=True
    class Icon:
        def __init__(self, source):
            if isinstance(source, str):
                with Image.open(source) as image:
                    assert image.format == "ICO"
            else:
                with Image.open(BytesIO(source.data)) as image:
                    assert image.format == "ICO" and (32,32) in image.ico.sizes()
            self.disposed=False; made.append(self)
        def Clone(self): return SimpleNamespace(Dispose=lambda:None, cloned=True)
        def Dispose(self): self.disposed=True
    class Array:
        @classmethod
        def __class_getitem__(cls, _): return bytes
    monkeypatch.setitem(sys.modules, "System.Drawing", SimpleNamespace(Icon=Icon))
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Array=Array, Byte=int))
    monkeypatch.setitem(sys.modules, "System.IO", SimpleNamespace(MemoryStream=Stream))
    monkeypatch.chdir(tmp_path)
    if asset_state != "valid":
        path = tmp_path / "bad.ico"
        if asset_state == "corrupt": path.write_bytes(b"not an icon")
        monkeypatch.setattr(tray_assets, "ICON_PATH", path)
    icon = integration._load_form_icon()
    if asset_state == "valid":
        assert isinstance(icon, Icon) and not icon.disposed
    else:
        assert icon.cloned and made[-1].disposed


def test_module_import_and_passive_bridge_reads_do_not_load_native_backend(monkeypatch):
    import ctypes
    import importlib
    class Forbidden:
        def __getattr__(self, name): pytest.fail("passive access to native library " + name)
    monkeypatch.setattr(ctypes, "windll", Forbidden())
    importlib.reload(integration)
    bridge = object.__new__(ui.WebviewBridge)
    assert bridge.get_window_state()["available"] is False
    assert bridge.window_action("close")["ok"] is False
