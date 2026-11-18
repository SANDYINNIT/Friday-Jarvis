"""Clicky-style source integration with synthetic state; no desktop/devices/providers."""
from types import SimpleNamespace
from threading import RLock
from unittest.mock import Mock
import json
import sys
import time

import pytest

from source.server.companion import Companion
from source.server.companion_native import Controller, BarBridge, runtime_activity, buddy_surface, place_panel
from source.server.companion_motion import BuddyMotion
from source.server.status_ui import StatusBus


def fixture(tmp_path):
    p = Companion(tmp_path / "prefs.json")
    owner, generation = SimpleNamespace(is_external=False), object()
    state = {"turn_ownership": owner, "turn_attempt": generation}
    bus = StatusBus()
    runtime = SimpleNamespace(server_state=state, companion=p, companion_execution_lock=RLock(),
                              respond_thread=SimpleNamespace(is_alive=lambda: True))
    return p, owner, generation, state, bus, runtime


def test_visible_text_is_bounded_local_current_and_never_persisted(tmp_path):
    p, owner, generation, state, bus, runtime = fixture(tmp_path)
    assert not p.observe_text(SimpleNamespace(is_external=True), generation, "Private phone text", live=lambda: True)
    assert not p.observe_text(owner, generation, "Late text", live=lambda: False)
    assert p.observe_text(owner, generation, "visible "*100, live=lambda: True)
    value = runtime_activity(runtime, bus)
    assert value["state"] == "responding" and len(value["caption"]) <= 280
    assert "phone" not in json.dumps(value)
    state["turn_attempt"] = object()
    assert not runtime_activity(runtime, bus)["caption"]
    p.discard_pending()
    assert p.caption is None and not p.path.exists()


@pytest.mark.parametrize("change", ["generation", "owner", "stop", "barge"])
def test_final_draw_refuses_interleaved_old_observation(tmp_path, change):
    p, owner, generation, state, bus, runtime = fixture(tmp_path)
    p.observe_text(owner, generation, "Current response", live=lambda: True)
    observation = runtime_activity(runtime, bus)
    p.save({"cursor": "always"}, p.preferences()["revision"])
    def pointer():
        if change == "generation": state["turn_attempt"] = object()
        elif change == "owner": state["turn_ownership"] = SimpleNamespace(is_external=True)
        elif change == "stop": state["cancelled_owner"] = owner
        else: state["barge_in"] = True
        return (20,30)
    buddy = SimpleNamespace(pointer=pointer, hide=Mock(), paint_activity=Mock(), excluded=True)
    c = Controller(p, bar=SimpleNamespace(hide=Mock(), show=Mock()), visible=lambda: True,
                   buddy_factory=lambda: buddy, activity=lambda: observation)
    c.tick()
    buddy.paint_activity.assert_not_called()
    buddy.hide.assert_called_once()
    assert not BarBridge(p).get_snapshot()["activity"]["caption"]


def test_activity_requires_client_progress_for_speaking(tmp_path):
    p, owner, generation, state, bus, runtime = fixture(tmp_path)
    bus.publish("speaking", "untrusted detail must not be displayed")
    assert runtime_activity(runtime,bus)["state"] == "processing"
    state.update(playback_owner=owner, playback_generation=generation,
                 audio_link=SimpleNamespace(playback_snapshot=lambda: {
                     "client_playback_observable": True, "playback_recent": True}))
    value = runtime_activity(runtime,bus)
    assert value["state"] == "speaking" and "untrusted" not in json.dumps(value)
    state["turn_ownership"] = SimpleNamespace(is_external=True)
    assert runtime_activity(runtime,bus)["state"] == "idle"


def test_exact_highlight_expires_and_old_generation_cannot_guide(tmp_path,monkeypatch):
    from source.server import screen_overlay
    p, owner, generation, state, bus, runtime = fixture(tmp_path)
    annotation = {"rect": [-400,20,-200,80], "label":"The selected control", "deadline":time.monotonic()+30}
    monkeypatch.setattr(screen_overlay,"_active",annotation)
    p.overlay = (owner,generation,annotation)
    value = runtime_activity(runtime,bus)
    assert value["guide"]["x"] == -200 and value["state"] == "guiding"
    monkeypatch.setattr(screen_overlay,"_active",None)
    with value.drawing() as valid:
        assert not valid
    assert "guide" not in runtime_activity(runtime,bus)


def test_motion_completes_and_returns_without_replaying_same_target():
    m = BuddyMotion()
    guide = {"key":"one", "x":100, "y":100}
    m.step((0,0),guide,"turn",0)
    assert m.phase == "flying"
    assert m.step((0,0),guide,"turn",1) == (100,100)
    assert m.phase == "pointing"
    m.step((0,0),guide,"turn",3.6)
    assert m.phase == "returning"
    assert m.step((0,0),guide,"turn",4.3) == (0,0)
    assert m.phase == "following"
    m.step((20,20),guide,"turn",5)
    assert m.phase == "following"
    m.step((20,20),None,"next-turn",6)
    assert m.position == (20,20)
    m.step((20,20),guide,"next-turn",7,reduced=True)
    assert m.position == (100,100) and m.phase == "pointing"
    m.step((20,20),{"key":"invalid","x":float('nan'),"y":1},"next-turn",8)
    assert m.phase == "following"


def test_panel_refuses_delayed_expansion_and_collapse_never_disables(tmp_path):
    p = Companion(tmp_path/"prefs.json")
    visible = [False]
    place, present = Mock(), Mock()
    bar = SimpleNamespace(show=Mock(), hide=Mock())
    c = Controller(p,bar=bar,visible=lambda:visible[0],position=place,present=present)
    assert c.set_expanded(True,True) == {"ok":True,"expanded":True,"pinned":True}
    visible[0] = True
    assert not c.set_expanded(True)["ok"]
    present.reset_mock()
    assert c.set_expanded(False)["ok"]
    present.assert_not_called(); bar.hide.assert_called_once()
    assert p.preferences()["values"]["top_panel"] is True
    visible[0] = False
    assert p.save({"top_panel":False},p.preferences()["revision"])["ok"]
    assert not c.set_expanded(True)["ok"]
    assert not c.set_expanded("yes")["ok"]


def test_panel_geometry_failure_is_not_success(tmp_path):
    p = Companion(tmp_path/"prefs.json")
    c = Controller(p,bar=SimpleNamespace(hide=Mock()),visible=lambda:False,
                   position=Mock(side_effect=RuntimeError("private details")))
    result = c.set_expanded(True)
    assert not result["ok"] and not c.expanded and "private" not in result["message"]
    assert not p.path.exists()


def test_panel_recovers_after_preferences_reader_failure(tmp_path,monkeypatch):
    p = Companion(tmp_path/"prefs.json")
    bar = SimpleNamespace(hide=Mock(),show=Mock())
    c = Controller(p,bar=bar,visible=lambda:False)
    c.tick()
    with monkeypatch.context() as patch:
        patch.setattr(p,"preferences",lambda:{"available":False})
        c.tick()
    c.tick()
    assert bar.show.call_count == 2 and bar.hide.call_count == 1


@pytest.mark.parametrize("scale", [1,1.25,1.5])
def test_panel_geometry_uses_owned_physical_pixels_without_showing(monkeypatch,scale):
    from source.server import companion_native as native
    gui = SimpleNamespace(SetWindowPos=Mock())
    con = SimpleNamespace(HWND_TOPMOST=-1,SWP_NOACTIVATE=16)
    api = SimpleNamespace(MonitorFromWindow=lambda *_:1,GetMonitorInfo=lambda _: {"Work":(-1920,0,0,1040)})
    monkeypatch.setitem(sys.modules,"win32gui",gui)
    monkeypatch.setitem(sys.modules,"win32con",con)
    monkeypatch.setitem(sys.modules,"win32api",api)
    monkeypatch.setattr(native,"_panel_handle",lambda window:123)
    from contextlib import nullcontext
    monkeypatch.setattr("source.server.screen_overlay._PerMonitorDPI",nullcontext)
    form = SimpleNamespace(scale_factor=scale)
    window = SimpleNamespace(uid="only",gui=SimpleNamespace(BrowserView=SimpleNamespace(instances={"only":form})))
    form.pywebview_window=window
    place_panel(window,True)
    args = gui.SetWindowPos.call_args.args
    assert args == (123,-1,int(-1920+(1920-round(360*scale))/2),8,round(360*scale),round(480*scale),16)


@pytest.mark.parametrize("state",["idle","listening","processing","responding","speaking","guiding","error"])
def test_caption_surface_is_inert_bounded_and_decodable(state):
    image = buddy_surface("pointer",state,"<script>literal only</script> "*20)
    try:
        assert image.mode == "RGBA" and image.width <= 304 and image.height <= 150
        assert image.getpixel((0,0))[3] == 0
    finally:
        image.close()


from test_request_reply_ownership import isolated_server, drain, telegram
from test_mcp_light import produce
import asyncio


@pytest.mark.parametrize("isolated_server", ["mcp"], indirect=True)
@pytest.mark.parametrize("external",[False,True])
def test_real_admitted_text_path_feeds_only_local_visible_caption(isolated_server,external,tmp_path):
    runtime, _ = isolated_server
    runtime.companion.path = tmp_path/"prefs.json"
    def provider(params):
        yield {"choices":[{"delta":{"content":"Actual bounded fixture response"},"finish_reason":"stop"}]}
    async def scenario():
        await runtime.server.app.router.startup()
        runtime.fixture_stream = provider
        if external:
            await telegram(runtime,"101","Explain this")
        else:
            assert (await asyncio.to_thread(runtime.submit_text,"Explain this"))["accepted"]
        await asyncio.to_thread(produce,runtime)
        await drain(runtime)
        return runtime.companion.caption
    try:
        result = asyncio.run(scenario())
        if external: assert result is None
        else: assert result and "Actual bounded fixture response" in result[2]
    finally:
        asyncio.run(runtime.server.app.router.shutdown())


def test_expansion_rechecks_visibility_after_geometry(tmp_path):
    p = Companion(tmp_path/"prefs.json")
    visible = [False]
    def place(expanded):
        if expanded: visible[0] = True  # HUD restored during native operation.
    present, hide = Mock(), Mock()
    c = Controller(p,bar=SimpleNamespace(hide=hide),visible=lambda:visible[0],
                   position=place,present=present)
    assert not c.set_expanded(True)["ok"]
    present.assert_not_called()
    hide.assert_called_once()
    assert not c.expanded


def test_owned_panel_focus_policy_and_foreground_refusal(monkeypatch):
    from source.server import companion_native as native
    gui=SimpleNamespace(GetWindowLong=lambda *_:0, SetWindowLong=Mock(), ShowWindow=Mock(),
                        SetForegroundWindow=Mock(side_effect=RuntimeError("Windows refused focus")))
    con=SimpleNamespace(GWL_EXSTYLE=-20,WS_EX_NOACTIVATE=0x8000000,WS_EX_TOOLWINDOW=0x80,
                        WS_EX_APPWINDOW=0x40000,SW_SHOW=5,SW_SHOWNOACTIVATE=4)
    monkeypatch.setitem(sys.modules,"win32gui",gui)
    monkeypatch.setitem(sys.modules,"win32con",con)
    monkeypatch.setattr(native,"_panel_handle",lambda window:123)
    window=SimpleNamespace(focus=False)
    window.uid="panel"
    window.gui=SimpleNamespace(BrowserView=SimpleNamespace(instances={"panel":SimpleNamespace(pywebview_window=window)}))
    native.show_panel(window,expanded=True)
    assert window.focus and gui.ShowWindow.call_args.args==(123,5)
    native.show_panel(window,expanded=False)
    assert not window.focus and gui.ShowWindow.call_args.args==(123,4)
    assert gui.SetWindowLong.call_args.args[-1] == 0x8000080


def test_recycled_cursor_handle_is_never_hidden_or_destroyed(monkeypatch):
    from source.server.companion_native import Win32Buddy
    from threading import get_native_id
    gui=SimpleNamespace(IsWindow=lambda _:True, ShowWindow=Mock(),DestroyWindow=Mock())
    monkeypatch.setitem(sys.modules,"win32gui",gui)
    monkeypatch.setitem(sys.modules,"win32process",SimpleNamespace(GetWindowThreadProcessId=lambda _:(get_native_id(),999999)))
    buddy=Win32Buddy()
    buddy.hwnd=777;buddy.thread=get_native_id()
    buddy.hide();buddy.close()
    gui.ShowWindow.assert_not_called();gui.DestroyWindow.assert_not_called()
    assert buddy.hwnd is None


def test_passive_native_panel_reads_refuse_stale_cached_text(tmp_path):
    p,owner,generation,state,bus,runtime=fixture(tmp_path)
    p.observe_text(owner,generation,"only the old turn",live=lambda:True)
    observed=runtime_activity(runtime,bus)
    p.native={"activity":{"caption":"old cached text"},"panel_expanded":False}
    c=Controller(p,bar=SimpleNamespace(),visible=lambda:True,activity=lambda:observed)
    p._native_controller=c
    state["turn_attempt"]=object()
    value=BarBridge(p).get_snapshot()
    assert not value["activity"]["caption"] and "old cached" not in json.dumps(value)
    assert not p.path.exists()


def test_native_panel_queued_timeout_cannot_run_later(monkeypatch):
    from source.server import companion_native as native
    queued=[]
    target=SimpleNamespace(uid="panel")
    form=SimpleNamespace(pywebview_window=target,InvokeRequired=True,BeginInvoke=queued.append)
    target.gui=SimpleNamespace(BrowserView=SimpleNamespace(instances={"panel":form}))
    monkeypatch.setitem(sys.modules,"System",SimpleNamespace(Action=lambda callback:callback))
    monkeypatch.setattr(native,"_PANEL_CALL_TIMEOUT",.001)
    action=Mock()
    with pytest.raises(TimeoutError):native._panel_thread(target,action)
    queued[0]()
    action.assert_not_called()


def test_native_panel_dispatches_on_owned_gui_thread(monkeypatch):
    from source.server import companion_native as native
    target=SimpleNamespace(uid="panel")
    form=SimpleNamespace(pywebview_window=target,InvokeRequired=True,BeginInvoke=lambda callback:callback())
    target.gui=SimpleNamespace(BrowserView=SimpleNamespace(instances={"panel":form}))
    monkeypatch.setitem(sys.modules,"System",SimpleNamespace(Action=lambda callback:callback))
    action=Mock(return_value="acknowledged")
    assert native._panel_thread(target,action)=="acknowledged"
    action.assert_called_once()


def test_surface_navigation_timeout_is_not_success_or_a_decision(tmp_path):
    p=Companion(tmp_path/'preferences.json')
    surface=SimpleNamespace(show=Mock())
    c=Controller(p,bar=SimpleNamespace(),surface=surface,visible=lambda:False,
                 surface_present=Mock(side_effect=TimeoutError('private details')))
    p.action=c.open
    result=p.open('inbox')
    assert not result['ok'] and p.navigation is None
    assert 'private' not in json.dumps(result)
    surface.show.assert_not_called()
