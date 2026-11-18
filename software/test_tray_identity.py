"""Actual tray entrypoint with fake backend/windows; no native tray is constructed."""
import importlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from source.server import startup_settings as startup


class FakeIcon:
    def __init__(self,name,image,title,menu):
        self.name,self.image,self.title,self.menu=name,image,title,menu
        self.ready=threading.Event();self.release=threading.Event();self.entered=threading.Event()
        self.allow_ready=threading.Event();self.allow_ready.set()
        self.visible=False;self.stops=0;self.fail=False

    def run(self,setup=None):
        self.entered.set()
        assert self.allow_ready.wait(2),"fake readiness was not released"
        if self.fail:raise RuntimeError("synthetic tray run failure")
        if setup:setup(self)
        else:self.visible=True
        self.ready.set()
        assert self.release.wait(2),"fake tray was not cleaned up"

    def stop(self):self.stops+=1;self.release.set()


@pytest.fixture
def fake(monkeypatch):
    icons=[]
    def factory(*args):
        icon=FakeIcon(*args);icons.append(icon);return icon
    backend=SimpleNamespace(Icon=factory,Menu=lambda *items:items,
        MenuItem=lambda label,callback,**kwargs:SimpleNamespace(label=label,callback=callback,**kwargs))
    monkeypatch.setitem(sys.modules,"pystray",backend)
    monkeypatch.setattr(startup,"_TRAY_CONTROLLER",None,raising=False)
    yield icons,backend
    for icon in icons:icon.allow_ready.set();icon.release.set()
    controller=getattr(startup,"_TRAY_CONTROLLER",None)
    if controller and controller.get("stop"):controller["stop"]()
    for thread in threading.enumerate():
        if thread.name=="friday-tray":thread.join(2)


def test_exact_tooltip_without_mojibake(fake):
    controller=startup.start_tray()
    icon=fake[0][0];assert icon.ready.wait(2)
    assert icon.title=="FRIDAY - Personal assistant"
    assert icon.title.isascii() and len(icon.title)<=127


def test_repeated_initialization_has_one_owner(fake):
    first=startup.start_tray()
    assert fake[0][0].ready.wait(2)
    second=startup.start_tray()
    assert first is second and len(fake[0])==1


def test_concurrent_initialization_keeps_one_owner(fake):
    barrier=threading.Barrier(6)
    def start(_):
        barrier.wait(timeout=2)
        return startup.start_tray()
    with ThreadPoolExecutor(max_workers=6) as pool:
        owners=list(pool.map(start,range(6)))
    assert len(fake[0])==1 and all(owner is owners[0] for owner in owners)
    assert fake[0][0].ready.wait(2)
    finish(owners[0])


def finish(controller):
    thread=controller['thread'];controller['stop']();thread.join(2)
    assert not thread.is_alive() and controller['state']=='stopped'
    assert controller['icon'] is None


def test_menu_targets_once_and_cleanup_never_exits_assistant(fake):
    calls=[];state={'paused':True,'active_work':'owned-turn'}
    controller=startup.start_tray(lambda:calls.append('show'),lambda:calls.append('hide'),lambda:calls.append('exit'))
    icon=fake[0][0];assert icon.ready.wait(2)
    assert [item.label for item in icon.menu]==['Open App','Close App (keep FRIDAY running)','Exit FRIDAY']
    assert icon.menu[0].default
    icon.menu[0].callback(icon,icon.menu[0]);icon.menu[1].callback(icon,icon.menu[1])
    assert calls==['show','hide'] and state=={'paused':True,'active_work':'owned-turn'}
    finish(controller)
    assert not icon.visible and icon.stops==1 and calls==['show','hide']
    assert controller['stop']() is False
    with pytest.raises(ValueError):icon.image.getpixel((1,1))


def test_exit_stops_tray_before_existing_exit_callback_once(fake):
    observations=[]
    controller=startup.start_tray(on_exit=lambda:observations.append((fake[0][0].visible,fake[0][0].stops)))
    icon=fake[0][0];assert icon.ready.wait(2)
    icon.menu[2].callback(icon,icon.menu[2]);icon.menu[2].callback(icon,icon.menu[2])
    controller['thread'].join(2)
    assert observations==[(False,1)] and controller['state']=='stopped'


def test_old_callbacks_cannot_affect_new_owner_or_replace_handlers(fake):
    calls=[];first=startup.start_tray(lambda:calls.append('first'));old=fake[0][0]
    assert old.ready.wait(2)
    assert startup.start_tray(lambda:calls.append('duplicate')) is first
    old.menu[0].callback(old,old.menu[0]);assert calls==['first']
    finish(first)
    newer=startup.start_tray(lambda:calls.append('new'));new=fake[0][-1];assert new.ready.wait(2)
    for item in old.menu:item.callback(old,item)
    assert first['stop']() is False and new.visible
    new.menu[0].callback(new,new.menu[0]);assert calls==['first','new']
    finish(newer)


def test_stop_before_readiness_cannot_show_or_duplicate_tray(fake):
    backend=fake[1];original=backend.Icon
    def delayed(*args):
        icon=original(*args);icon.allow_ready.clear();return icon
    backend.Icon=delayed
    controller=startup.start_tray();icon=fake[0][0];assert icon.entered.wait(2)
    assert controller['stop']() and not icon.visible
    assert startup.start_tray() is controller and len(fake[0])==1
    icon.allow_ready.set();controller['thread'].join(2)
    assert controller['state']=='stopped' and not icon.visible and icon.stops==1


def test_backend_run_failure_releases_owner_and_allows_retry(fake):
    backend=fake[1];original=backend.Icon
    def failed(*args):
        icon=original(*args);icon.fail=True;return icon
    backend.Icon=failed
    controller=startup.start_tray();controller['thread'].join(2)
    assert controller['state']=='stopped' and startup._TRAY_CONTROLLER is None
    backend.Icon=original
    newer=startup.start_tray();assert fake[0][-1].ready.wait(2);finish(newer)


def test_missing_backend_and_constructor_failure_are_nonfatal(fake,monkeypatch):
    monkeypatch.setitem(sys.modules,'pystray',None)
    assert startup.start_tray() is None and startup._TRAY_CONTROLLER is None
    monkeypatch.setitem(sys.modules,'pystray',fake[1])
    def unavailable(*_args):raise RuntimeError('private backend detail')
    fake[1].Icon=unavailable
    assert startup.start_tray() is None and startup._TRAY_CONTROLLER is None


def test_missing_image_dependency_is_nonfatal(fake,monkeypatch):
    from source.server import tray_assets
    def unavailable():raise ImportError('Pillow unavailable')
    monkeypatch.setattr(tray_assets,'load_tray_image',unavailable)
    assert startup.start_tray() is None and not fake[0]


def test_failed_thread_start_closes_image_and_releases_owner(fake,monkeypatch):
    def failed(_thread):raise RuntimeError('thread creation refused')
    monkeypatch.setattr(threading.Thread,'start',failed)
    assert startup.start_tray() is None and startup._TRAY_CONTROLLER is None
    with pytest.raises(ValueError):fake[0][0].image.getpixel((1,1))


def test_menu_failure_does_not_change_owner_or_voice_state(fake):
    def failed():raise RuntimeError('callback detail')
    controller=startup.start_tray(on_open_app=failed);icon=fake[0][0];assert icon.ready.wait(2)
    icon.menu[0].callback(icon,icon.menu[0])
    assert icon.visible and controller['state']=='running'
    finish(controller)


def test_import_and_settings_reads_do_not_start_tray(fake,monkeypatch):
    from source.server import tray_assets
    before=len(fake[0])
    def forbidden(*_args,**_kwargs):pytest.fail('passive import started a thread')
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread,'start',forbidden)
        importlib.reload(tray_assets)
        importlib.reload(startup)
    assert len(fake[0])==before and startup._TRAY_CONTROLLER is None
