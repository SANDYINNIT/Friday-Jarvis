"""Companion prerequisites through real, device-free production objects."""
import asyncio
import threading
import time
from queue import Queue, Full
from types import SimpleNamespace

import pytest
from PIL import Image

from source.server import audio_link, light_archive, screen_capture_service as capture
from source.server import screen_overlay as overlay


def service(*buffers):
    result = capture.ScreenCaptureService(target_fps=0)
    result._buffers = list(buffers)
    return result


def buffer(index=0, name="DISPLAY-A", bounds=(0, 0, 20, 10)):
    b = capture._FrameBuffer(0, index, (bounds[2], bounds[3]))
    b.pil = Image.new("RGB", b.size, "red" if index == 0 else "blue")
    b.captured_mono = time.monotonic()
    b.geometry = (0, name, bounds)
    return b


@pytest.mark.parametrize("selection", [-1, 2, "missing", float("inf")])
def test_explicit_missing_display_never_reads_primary(selection):
    s = service(buffer())
    assert s.frame(selection) is None
    assert s._buffer_for(selection) is None
    assert s.history_read(selection) is None
    assert not s.changed(selection)
    assert s.frame() is not None


def test_live_labels_match_exact_identity_instead_of_output_order():
    right = buffer(0, "RIGHT", (0, 0, 20, 10))
    left = buffer(1, "LEFT", (-20, 0, 20, 10))
    s = service(right, left)
    layout = [dict(device_name="LEFT", bounds=(-20, 0, 20, 10), label="left", primary=False),
              dict(device_name="RIGHT", bounds=(0, 0, 20, 10), label="right", primary=True)]
    rows = s.labelled_frames(layout)
    assert [r["label"] for r in rows] == ["left", "right"]
    assert rows[0]["image"] is left.pil and rows[1]["image"] is right.pil
    assert [r["primary"] for r in rows] == [False, True]
    layout[0]["device_name"] = "unknown"
    assert s.labelled_frames(layout) is None
    right.geometry = None
    assert s.labelled_frames(layout) is None


def test_rotation_or_placement_changes_invalidate_history():
    class Outputs:
        def output_info(self):
            return "Device[0] Output[0]: Res:(20, 10) Rot:180 Primary:True"
    old = buffer()
    old.history.append({"synthetic": True})
    s = service(old)
    assert s._output_signature([capture._Output(0, 0, (20, 10), True, 0)]) != s._output_signature(
        capture._enumerated_outputs(Outputs()))
    assert s.rebind(Outputs())
    assert not s._buffers[0].history
    assert s._buffers[0] is not old


def test_failed_opposite_voice_write_preserves_delayed_receipt():
    state = dict(is_paused=True)
    link = audio_link.AudioLink(state)
    session = link.connect(True)
    q = Queue(maxsize=1)
    assert link.set_voice(False, q)["revision"] == 1
    accepted = q.get_nowait()
    q.put_nowait("occupied")
    assert link.set_voice(True, q) is None
    assert not state["is_paused"]
    assert link.accept_voice(session, {**accepted, "applied": True})
    assert link.voice_snapshot()["state"] == "applied"
    assert audio_link.AudioWebSocket(None, link, None)._can_capture(1)


def test_sealed_zero_audio_is_never_client_completion():
    link = audio_link.AudioLink(dict(is_paused=False))
    session = link.connect(True)
    owner = object()
    link.begin(owner)
    frames = []
    link.finish_synthesis(owner, frames.append)
    assert link.outgoing(session, frames[0], True) is None
    assert link.playback_snapshot()["playback_outcome"] == "no_audio"
    assert not link.playback_snapshot()["client_playback_observable"]
    assert not link.accept_playback(session, {**frames[0], "event": "drained", "bytes": 0})


def test_visible_json_survives_reopen_but_credentials_and_reasoning_do_not(tmp_path):
    async def scenario():
        archive = light_archive.ConversationArchive(tmp_path / "archive.db")
        owner, generation = object(), object()
        archive.admit(owner, generation, "hud", "Explain this JSON")
        archive.append(owner, generation, object(), '{"answer": 42, "password": "fixture-secret"}')
        archive.finish(owner)
        import sqlite3
        with sqlite3.connect(archive.path) as db:
            answer = db.execute("SELECT text FROM message WHERE speaker='assistant'").fetchone()[0]
            assert '"answer": 42' in answer and "fixture-secret" not in answer
            assert db.execute("SELECT outcome FROM turn").fetchone()[0] == "completed"
        assert light_archive.visible_text('<think>hidden</think>', assistant=True) == ""
    asyncio.run(scenario())


@pytest.mark.parametrize("points", [((10,40),(70,40)),((70,40),(10,40)),
    ((40,10),(40,70)),((40,70),(40,10)),((10,10),(70,70)),((70,70),(10,10)),
    ((10,70),(70,10)),((70,10),(10,70))])
def test_oriented_arrow_renders_both_endpoints(points):
    image=overlay._draw("arrow",80,80,"",(255,0,0),False,points)
    alpha=image.getchannel("A")
    assert alpha.getpixel(points[0]) > 0 and alpha.getpixel(points[1]) > 0
    assert alpha.getpixel((0,0)) == 0


def test_old_watcher_cannot_clear_a_replacement(monkeypatch):
    old={"deadline":0,"query":"old"}
    new={"deadline":10**12,"query":"new"}
    monkeypatch.setattr(overlay,"_active",new)
    assert overlay.clear("late",expected=old) == {"cleared":False,"reason":"superseded"}
    assert overlay._active is new


def test_queued_hide_checks_the_original_revision_before_native_access(monkeypatch):
    callbacks=[]
    monkeypatch.setattr(overlay,"_window",{"hwnd":-123})
    monkeypatch.setattr(overlay,"_window_owner_tid",-1)
    monkeypatch.setattr(overlay,"_window_executor",callbacks.append)
    monkeypatch.setattr(overlay,"_revision",10)
    overlay._hide_window(10)
    assert len(callbacks)==1
    overlay._revision=11
    callbacks[0]()  # must return before any Win32 operation against a new mark


def test_capture_exclusion_is_unknown_until_observed(monkeypatch):
    monkeypatch.setattr(overlay,"_window",{})
    assert overlay.status()["excluded_from_capture"] is None
    monkeypatch.setitem(overlay._window,"capture_excluded",False)
    assert overlay.status()["excluded_from_capture"] is False


def test_native_blend_releases_only_its_owned_gdi_resources(monkeypatch):
    import sys
    from unittest.mock import Mock
    from PIL import ImageWin
    bitmap=SimpleNamespace(CreateCompatibleBitmap=Mock(),GetHandle=lambda:123)
    memory=SimpleNamespace(SelectObject=Mock(),GetSafeHdc=lambda:456,DeleteDC=Mock())
    screen=SimpleNamespace(CreateCompatibleDC=lambda:memory)
    gui=SimpleNamespace(GetDC=lambda _:789,ReleaseDC=Mock(),UpdateLayeredWindow=Mock(),DeleteObject=Mock())
    monkeypatch.setitem(sys.modules,"win32gui",gui)
    monkeypatch.setitem(sys.modules,"win32con",SimpleNamespace(ULW_ALPHA=2))
    monkeypatch.setitem(sys.modules,"win32ui",SimpleNamespace(CreateDCFromHandle=lambda _:screen,CreateBitmap=lambda:bitmap))
    monkeypatch.setattr(ImageWin,"Dib",lambda _:SimpleNamespace(draw=Mock()))
    assert overlay._render(-1,0,0,32,32,"rect","",(255,0,0),False) is None
    memory.DeleteDC.assert_called_once()
    gui.DeleteObject.assert_called_once_with(123)
    gui.ReleaseDC.assert_called_once_with(0,789)
