import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from source.server.companion import Companion, DEFAULTS, PRESENTATION, validate_card
from source.server.companion_native import Controller, BarBridge, attach, buddy_image, panel_position
from source.server.mcp_turn import ModelTools


def card(**changes):
    return {**dict(id="lesson-1", kind="lesson", title="A small explanation", text="Plain <script> text"), **changes}


def test_passive_reads_do_not_create_files_or_start_adapters(tmp_path):
    presenter = Companion(tmp_path / "missing" / "settings.json")
    for _ in range(3):
        assert presenter.snapshot()["preferences"]["values"] == DEFAULTS
    assert not list(tmp_path.iterdir())


def test_checked_preference_save_reopen_and_failure(tmp_path, monkeypatch):
    presenter = Companion(tmp_path / "settings.json")
    revision = presenter.preferences()["revision"]
    assert presenter.save({"cursor":"always"}, revision)["ok"]
    saved = presenter.path.read_bytes()
    assert Companion(presenter.path).preferences()["values"]["cursor"] == "always"
    assert not presenter.save({"cursor":"off"}, revision)["ok"]
    revision = presenter.preferences()["revision"]
    assert not presenter.save({"allow_tools":True}, revision)["ok"]
    monkeypatch.setattr("os.replace", Mock(side_effect=OSError("synthetic private error")))
    result = presenter.save({"style":"leaf"}, revision)
    assert not result["ok"] and "private" not in result["message"]
    assert presenter.path.read_bytes() == saved
    assert {p.name for p in tmp_path.iterdir()} == {'settings.json','settings.json.lock'}  # Durable interprocess lock, no leaked temporary file.


def test_local_cards_are_bounded_deduplicated_and_generation_fenced(tmp_path):
    now = [10]
    p = Companion(tmp_path / "settings.json", clock=lambda:now[0])
    owner, generation = object(), object()
    for _ in range(2):
        assert p.publish(owner, generation, card(), live=lambda:True)
    assert len(p.snapshot()["rows"]) == 1
    assert not p.publish(owner, object(), card(), live=lambda:False)
    for i in range(40):
        assert p.publish(owner, generation, card(id="item-"+str(i)), live=lambda:True)
    assert len(p.snapshot()["rows"]) == 32
    assert p.acknowledge(p.snapshot()["rows"][0]["id"])["ok"]
    now[0] += 1801
    assert not p.snapshot()["rows"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("payload", [
    {"scene":{"type":"html", "text":"<script>bad()</script>"}},
    {"scene":{"type":"mesh", "url":"https://not-fetched.invalid/model.glb"}},
    {"scene":{"type":"mesh", "vertices":[[0,0,0]]*3, "faces":[[0,1,100]]}},
    {"scene":{"type":"diagram", "shapes":[{"kind":"eval", "code":"bad()"}]}},
    {"scene":{"type":"diagram", "shapes":[{"kind":"line", "x":float('nan')}]}},
    {"text":"x"*8001}, {"id":"../invalid"}
])
def test_untrusted_scene_data_rejected(payload):
    with pytest.raises((ValueError, TypeError)):
        validate_card(card(**payload))


def test_real_mesh_and_diagram_contract():
    mesh={"type":"mesh", "vertices":[[0,0,0],[1,0,0],[0,1,1]], "faces":[[0,1,2]]}
    assert validate_card(card(scene=mesh))["scene"] == mesh
    diagram={"type":"diagram","shapes":[{"kind":"arrow","x":50,"y":50,"w":-40,"h":0}]}
    assert validate_card(card(scene=diagram))["scene"] == diagram


def test_complete_function_arguments_required_and_mcp_domain_separate():
    params={"tools":[]}
    stream=ModelTools(None,params,presentation=True)
    stream.filter({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"friday_present","arguments":'{"id":"a",'}}]}}]})
    assert stream.proposal() is False
    stream.filter({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":'"kind":"lesson","title":"a","text":"hello"}'}}]},"finish_reason":"tool_calls"}]})
    reference, data = stream.proposal()
    assert reference is PRESENTATION and data["text"] == "hello"
    assert stream.refs == {"friday_present":PRESENTATION}


class Buddy:
    def __init__(self):
        self.calls=[]
        self.excluded=True
    def pointer(self): self.calls.append("pointer");return (20,30)
    def paint(self,*args): self.calls.append(("paint",args))
    def hide(self): self.calls.append("hide")
    def close(self): self.calls.append("close")


def test_native_off_when_asked_hide_restore_and_cleanup_use_only_owned_adapters(tmp_path):
    presenter=Companion(tmp_path / "settings.json")
    bar=SimpleNamespace(show=Mock(),hide=Mock(),destroy=Mock())
    visible=[False]
    buddy=Buddy()
    factory=Mock(return_value=buddy)
    cue=[False]
    controller=Controller(presenter,bar=bar,visible=lambda:visible[0],buddy_factory=factory,cue=lambda:cue[0])
    controller.tick()
    bar.show.assert_called_once();factory.assert_not_called()
    p=presenter.preferences();assert presenter.save({"cursor":"when_asked"},p["revision"])["ok"]
    controller.tick();factory.assert_not_called()
    cue[0]=True;controller.tick();assert "pointer" in buddy.calls
    visible[0]=True;controller.tick();bar.hide.assert_called_once()
    p=presenter.preferences();presenter.save({"cursor":"off"},p["revision"])
    controller.tick();assert buddy.calls[-1]=="close"
    controller.stop();bar.destroy.assert_called_once()
    controller.stop();bar.destroy.assert_called_once()
    assert presenter.preferences()["values"]["top_panel"] is True


def test_notification_acknowledgement_cannot_become_consent_or_execution(tmp_path):
    presenter=Companion(tmp_path / "settings.json")
    assert presenter.publish(object(),object(),card(kind="notification"),live=lambda:True)
    identifier=presenter.snapshot()["rows"][0]["id"]
    assert not presenter.acknowledge("another-request")["ok"]
    assert presenter.acknowledge(identifier)["ok"]
    assert presenter.snapshot()["rows"][0]["seen"]
    assert presenter.open("unknown") == {"ok":False,"message":"The main FRIDAY window is unavailable."}


def test_actual_native_attachment_uses_one_owner_and_existing_open_callback(tmp_path,monkeypatch):
    monkeypatch.setattr('source.server.companion_native._panel_thread',lambda window,operation:operation())
    class Event:
        def __init__(self): self.callbacks=[]
        def __iadd__(self, callback): self.callbacks.append(callback);return self
    windows=[]
    def create(title, url, **kwargs):
        window=SimpleNamespace(title=title,show=Mock(),hide=Mock(),destroy=Mock(),
            events=SimpleNamespace(loaded=Event(),closing=Event()))
        windows.append(window)
        assert "companion-" in url
        return window
    fake=SimpleNamespace(screens=[SimpleNamespace(width=1920,height=1080)],create_window=create)
    presenter=Companion(tmp_path / "preferences.json")
    restore=Mock(return_value=True)
    controller=attach(presenter,fake,on_open=restore,visible=lambda:False,start_hidden=True)
    assert attach(presenter,fake,on_open=restore,visible=lambda:False) is controller
    assert len(windows)==2
    assert BarBridge(presenter).open("home")["ok"]
    restore.assert_called_once()
    assert BarBridge(presenter).open("lessons")["ok"]
    windows[1].show.assert_called_once()
    restore.assert_called_once()
    assert windows[1].events.closing.callbacks[0]() is False
    windows[1].hide.assert_called_once()
    controller.stop()
    assert windows[0].events.closing.callbacks[0]() is True
    assert not BarBridge(presenter).open("home")["ok"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("style",["friday","leaf","pointer"])
def test_companion_assets_have_native_size_and_transparent_surroundings(style):
    image=buddy_image(style)
    assert image.mode=="RGBA" and image.size==(32,32)
    assert image.getpixel((0,0))[3]==0
    assert max(image.getchannel("A").getdata())==255
    image.close()


def test_panel_position_supports_installed_screen_without_xy(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,"win32api",SimpleNamespace())
    assert panel_position(SimpleNamespace(width=1920,height=1080),320)==(800,8)


def test_new_lesson_requests_one_display_while_notifications_do_not(tmp_path):
    p=Companion(tmp_path / "settings.json")
    owner,generation=object(),object()
    p.publish(owner,generation,card(kind="notification"),live=lambda:True)
    assert p.take_presentation() is None
    p.publish(owner,generation,card(id="lesson-new"),live=lambda:True)
    assert p.take_presentation() is not None
    assert p.take_presentation() is None


def test_partial_native_creation_releases_its_own_bar(tmp_path):
    bar=SimpleNamespace(destroy=Mock())
    calls=[]
    def create(*args,**kwargs):
        calls.append(args[0])
        if len(calls)==1:return bar
        raise RuntimeError("synthetic surface unavailable")
    fake=SimpleNamespace(screens=[SimpleNamespace(width=1920)],create_window=create)
    p=Companion(tmp_path / "settings.json")
    with pytest.raises(RuntimeError):attach(p,fake,on_open=lambda:True,visible=lambda:False)
    bar.destroy.assert_called_once()
    assert p.action is None


def test_real_cursor_backend_sets_passive_styles_and_never_moves_system_pointer(monkeypatch):
    import ctypes
    import sys
    from contextlib import nullcontext
    from source.server import screen_overlay
    from source.server.companion_native import Win32Buddy
    gui=SimpleNamespace(CreateWindowEx=Mock(return_value=456),SetWindowPos=Mock(),IsWindow=lambda hwnd:hwnd==456,
        PumpWaitingMessages=Mock(),DestroyWindow=Mock(),GetForegroundWindow=lambda:123)
    con=SimpleNamespace(WS_EX_LAYERED=1,WS_EX_TRANSPARENT=2,WS_EX_NOACTIVATE=4,
        WS_EX_TOOLWINDOW=8,WS_EX_TOPMOST=16,WS_POPUP=32,HWND_TOPMOST=-1,
        SWP_NOACTIVATE=64,SWP_SHOWWINDOW=128)
    api=SimpleNamespace(GetMonitorInfo=lambda _:{"Monitor":(-1920,0,0,1080)},
        MonitorFromPoint=lambda *a:1,GetCursorPos=Mock(return_value=(-100,20)))
    def affinity(hwnd,value):value._obj.value=0x11;return True
    dll=SimpleNamespace(SetWindowDisplayAffinity=Mock(return_value=True),GetWindowDisplayAffinity=Mock(side_effect=affinity))
    monkeypatch.setitem(sys.modules,"win32gui",gui)
    monkeypatch.setitem(sys.modules,"win32con",con)
    monkeypatch.setitem(sys.modules,"win32api",api)
    import os
    from threading import get_native_id
    monkeypatch.setitem(sys.modules,"win32process",SimpleNamespace(GetWindowThreadProcessId=lambda hwnd:(get_native_id(),os.getpid())))
    monkeypatch.setattr(ctypes,"windll",SimpleNamespace(user32=dll))
    monkeypatch.setattr(screen_overlay,"_PerMonitorDPI",nullcontext)
    render=Mock(return_value=None)
    monkeypatch.setattr(screen_overlay,"_render",render)
    buddy=Win32Buddy()
    assert buddy.pointer()==(-100,20)
    buddy.paint(-100,20,"friday")
    assert gui.CreateWindowEx.call_args.args[0]==31
    assert buddy.excluded is True and render.call_count==1
    assert gui.SetWindowPos.call_args.args[-1]==192
    buddy.paint(-90,20,"friday")
    assert render.call_count==1
    buddy.close();gui.DestroyWindow.assert_called_once_with(456)
    assert not hasattr(api,"SetCursorPos") and not hasattr(gui,"SendInput")
