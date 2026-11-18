"""Real run_status_window caller, replacing every desktop/lifecycle adapter."""
import builtins
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
import source.server as package
from source.server import status_ui as ui, startup_settings
from source.server import window_integration


@pytest.mark.parametrize('tray_available',[True,False])
def test_real_window_owner_keeps_callbacks_and_cleans_only_tray(tmp_path,monkeypatch,tray_available):
    effects=[];callbacks={};windows=[];threads=[]
    state={'is_paused':True,'active_turn':'unchanged'}
    class Event:
        def __init__(self):self.handlers=[]
        def __iadd__(self,handler):self.handlers.append(handler);return self
        def is_set(self):return False
    class Thread:
        def __init__(self,*args,**kwargs):threads.append(kwargs.get('name'))
        def start(self):pass
    class FakeFile:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def write(self,text):effects.append(('flag-content',text))
    def start_tray(**kwargs):
        callbacks.update(kwargs)
        return {'stop':lambda:effects.append(('stop-tray',))}if tray_available else None
    def create_window(title,*_args,**_kwargs):
        window=SimpleNamespace(title=title,events=SimpleNamespace(closed=Event(),shown=Event(),closing=Event()));windows.append(window);return window
    fake_lab=SimpleNamespace(set_presence=lambda state:effects.append(('presence',state)))
    fake_globe=SimpleNamespace(GlobeController=lambda:object(),open_globe=lambda **kwargs:'synthetic globe')
    monkeypatch.setitem(sys.modules,'source.server.lab_state',fake_lab);monkeypatch.setattr(package,'lab_state',fake_lab,raising=False)
    monkeypatch.setitem(sys.modules,'source.server.globe_window',fake_globe);monkeypatch.setattr(package,'globe_window',fake_globe,raising=False)
    monkeypatch.setitem(sys.modules,'source.server.file_logger',SimpleNamespace(log_file_path=lambda:tmp_path/'synthetic.log'))
    monkeypatch.setitem(sys.modules,'win32con',SimpleNamespace(SW_RESTORE=9,SW_SHOW=5,SW_HIDE=0))
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(FindWindow=lambda _,title:1 if title=='FRIDAY'else 2,IsIconic=lambda hwnd:True,
        ShowWindow=lambda hwnd,action:effects.append(('show',hwnd,action)),SetForegroundWindow=lambda hwnd:None))
    monkeypatch.setattr(startup_settings,'start_tray',start_tray)
    # The new process-grouping call is native work too: fake it in this
    # existing all-adapters-replaced owner fixture, never touch this test runner.
    monkeypatch.setattr(window_integration,'prepare_taskbar_identity',lambda:True)
    monkeypatch.setattr(threading,'Thread',Thread)
    monkeypatch.setattr(ui,'_hud_window_geometry_kwargs',lambda:{'width':1100,'height':780})
    monkeypatch.setattr(ui,'_boot_resume_presence',lambda hidden:effects.append(('boot-presence',hidden)))
    def forbidden(*args,**kwargs):pytest.fail('native/fallback operation attempted')
    monkeypatch.setattr(ui,'FaceStatusWindow',forbidden)
    monkeypatch.setattr(subprocess,'Popen',lambda args,**kwargs:effects.append(('console',args))or object())
    def loop(**kwargs):
        # Actual window callbacks; fake Win32 and process adapter only.
        callbacks['on_open_app']();callbacks['on_open_app']();callbacks['on_close_app']()
        assert len([e for e in effects if e[0]=='console'])==1
        assert effects[-1]==('show',2,0)
        with monkeypatch.context() as patch:
            patch.setattr(os,'makedirs',lambda *args,**kwargs:effects.append(('flag-directory',)))
            patch.setattr(os,'_exit',lambda code:effects.append(('exit',code)))
            def stop_flag(path,*args,**kwargs):
                assert str(path).endswith('stop_friday.flag');effects.append(('flag-open',));return FakeFile()
            patch.setattr(builtins,'open',stop_flag)
            callbacks['on_exit']()
    monkeypatch.setitem(sys.modules,'webview',SimpleNamespace(windows=windows,create_window=create_window,start=loop))
    ui.run_status_window(ui.StatusBus(),SimpleNamespace(server_state=state),start_hidden=True)
    assert state=={'is_paused':True,'active_turn':'unchanged'}
    assert ('show',1,9)in effects and ('show',1,5)in effects and ('show',1,0)in effects
    assert len([e for e in effects if e[0]=='stop-tray'])==int(tray_available)
    assert ('exit',0)in effects and ('flag-content','tray exit - intentional stop')in effects
    assert all('python'not in' '.join(e[1]).lower()for e in effects if e[0]=='console')
    assert threads==['hud-geometry','window-probe']
