"""LAB-3R5: native HUD geometry - store handling, watchdog persistence,
extreme-state skipping and diagnostics.

Covers the two 2026-11-06 verification defects:
  1. `_hud_window_kwargs` dereferenced None when hud_window.json was
     missing (first-boot crash -> Tk fallback). Now the extracted
     `_hud_window_geometry_kwargs()` falls back to defaults.
  2. the watchdog called `win32gui.IsZoomed`, which does NOT exist in the
     installed pywin32 (probed: venv AND system python both False), so a
     bare `except: continue` silently killed geometry persistence forever.
     Now `_hud_window_state()` uses IsIconic + GetWindowPlacement and
     `_hud_geometry_tick()` raises on save failure (printed once).

Real Win32/Tk windows are used for the watchdog tests; the geometry store
path is always monkeypatched into tmp_path (never ~/.friday).
"""

import json
import os
import threading
import time

import pytest

from source.server import status_ui as sui


@pytest.fixture()
def geom_path(tmp_path, monkeypatch):
    path = tmp_path / "hud_window.json"
    monkeypatch.setattr(sui, "_hud_geometry_store_path", lambda: str(path))
    return path


class TestGeometryStoreHandling:
    def test_missing_file_falls_back_to_defaults(self, geom_path):
        # the proven first-boot condition from the verification round
        assert not geom_path.exists()
        assert sui._load_hud_geometry() is None
        kw = sui._hud_window_geometry_kwargs()
        assert kw["width"] == 1100 and kw["height"] == 780
        assert kw["x"] is None and kw["y"] is None
        assert kw["min_size"] == (760, 560)

    def test_malformed_json_falls_back(self, geom_path):
        geom_path.write_text("{not json at all", encoding="utf-8")
        assert sui._load_hud_geometry() is None
        kw = sui._hud_window_geometry_kwargs()
        assert kw["width"] == 1100 and kw["height"] == 780

    @pytest.mark.parametrize("bad", [
        {"x": True, "y": 0, "width": 900, "height": 700},   # bool is not int
        {"x": "5", "y": 0, "width": 900, "height": 700},    # string coord
        {"x": 0, "y": 0, "width": 10, "height": 700},       # below floor
        {"x": 0, "y": 0, "width": 900, "height": 99999},    # absurd size
        {"x": 0, "y": 0, "width": 900},                     # missing key
        "not-a-dict",                                       # wrong type
        None,
    ])
    def test_malformed_values_rejected(self, geom_path, bad):
        assert sui._sanitize_hud_geometry(bad) is None
        if bad is not None:
            geom_path.write_text(json.dumps(bad), encoding="utf-8")
            assert sui._load_hud_geometry() is None
        assert sui._hud_window_geometry_kwargs()["width"] == 1100

    def test_valid_geometry_round_trips(self, geom_path):
        geom_path.write_text(json.dumps(
            {"x": 50, "y": 60, "width": 1100, "height": 780}),
            encoding="utf-8")
        assert sui._load_hud_geometry() == {
            "x": 50, "y": 60, "width": 1100, "height": 780}
        kw = sui._hud_window_geometry_kwargs()
        assert kw["x"] == 50 and kw["y"] == 60
        assert kw["width"] == 1100 and kw["height"] == 780

    def test_offscreen_restore_is_clamped(self, monkeypatch):
        # virtual screen = secondary-left monitor layout
        monkeypatch.setattr(sui, "_virtual_screen_bounds",
                            lambda: (-1920, 0, 1920, 1080))
        geo = sui._clamp_hud_geometry(
            {"x": -5000, "y": -4000, "width": 1100, "height": 780})
        # at least 80px horizontally / 120px vertically must remain visible
        assert geo["x"] >= -1920 + 80 - 1100, geo
        assert geo["x"] <= -1920 + 1920 - 80, geo
        assert geo["y"] >= 0 + 120 - 780, geo
        assert geo["y"] <= 0 + 1080 - 120, geo

    def test_save_is_atomic_and_creates_folder(self, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "hud_window.json"
        monkeypatch.setattr(sui, "_hud_geometry_store_path",
                            lambda: str(target))
        assert sui._save_hud_geometry(
            {"x": 1, "y": 2, "width": 1100, "height": 780}) is True
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data == {"x": 1, "y": 2, "width": 1100, "height": 780}
        leftovers = [p for p in target.parent.iterdir()
                     if p.suffix == ".tmp"]
        assert not leftovers, leftovers


@pytest.fixture()
def friday_window(monkeypatch):
    """A real top-level Tk window titled FRIDAY (the watchdog's target).

    Creation is retried briefly: a Tk() started while the previous test's
    interpreter is still finalizing can fail with a spurious TclError
    ('couldn't read file auto.tcl') even though the file exists - observed
    2026-11-06 as a fixture-setup race, not a missing library.
    """
    tkinter = pytest.importorskip("tkinter")
    root = None
    last_error = None
    for _attempt in range(6):
        try:
            root = tkinter.Tk()
            break
        except tkinter.TclError as error:
            last_error = error
            time.sleep(0.4)
    if root is None:
        raise last_error
    # LAB-3R7: while the LIVE production HUD (title "FRIDAY") is running,
    # FindWindow(None, "FRIDAY") in the watchdog resolved to the PRODUCTION
    # window (4 tests failed; worse, the watchdog could have written
    # production rects into this fixture's file). Retarget everything to a
    # unique fixture title via the patchable module constant.
    unique = "FRIDAY-GEOMTEST-%d" % os.getpid()
    monkeypatch.setattr(sui, "HUD_WINDOW_TITLE", unique)
    root.title(unique)
    root.geometry("900x700+40+40")
    root.update()
    time.sleep(0.2)
    yield root
    try:
        root.destroy()
    except Exception:
        pass
    time.sleep(0.3)   # let the interpreter finalize before the next test


class TestWatchdogAndState:
    def test_state_uses_installed_backend_apis(self, friday_window):
        import win32gui
        hwnd = win32gui.FindWindow(None, sui.HUD_WINDOW_TITLE)
        assert hwnd, "fixture window not found by title"
        # NOTE: this pywin32 build has NO win32gui.IsZoomed (the 2026-11-06
        # defect); _hud_window_state must produce correct answers without it.
        assert sui._hud_window_state(hwnd) is None             # normal
        friday_window.state("zoomed")
        friday_window.update()
        time.sleep(0.3)
        assert sui._hud_window_state(hwnd) == "max"
        friday_window.state("normal")
        friday_window.update()
        time.sleep(0.3)
        friday_window.iconify()
        friday_window.update()
        time.sleep(0.3)
        assert sui._hud_window_state(hwnd) == "min"
        friday_window.state("normal")
        friday_window.update()

    def test_watchdog_persists_move_and_skips_extremes(
            self, geom_path, friday_window):
        import win32gui
        stop = threading.Event()
        thread = threading.Thread(
            target=sui._hud_geometry_watchdog,
            kwargs={"poll": 0.3, "stop": stop}, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 6
            while time.time() < deadline and not geom_path.exists():
                time.sleep(0.1)
            assert geom_path.exists(), "watchdog never wrote the geometry"
            first = json.loads(geom_path.read_text(encoding="utf-8"))
            assert first["width"] >= 760 and first["height"] >= 560, first
            # move -> the store must follow
            friday_window.geometry("900x700+400+300")
            friday_window.update()
            deadline = time.time() + 6
            moved = first
            while time.time() < deadline:
                moved = json.loads(geom_path.read_text(encoding="utf-8"))
                if (moved["x"], moved["y"]) != (first["x"], first["y"]):
                    break
                time.sleep(0.2)
            assert (moved["x"], moved["y"]) != (first["x"], first["y"]), (
                "watchdog did not persist the move", first, moved)
            # maximized: maximized bounds must NEVER be persisted
            friday_window.state("zoomed")
            friday_window.update()
            time.sleep(1.2)
            after_zoom = json.loads(geom_path.read_text(encoding="utf-8"))
            assert after_zoom == moved, (
                "maximized bounds leaked into the store",
                moved, after_zoom)
            # minimized: likewise
            friday_window.state("normal")
            friday_window.update()
            time.sleep(0.5)
            friday_window.iconify()
            friday_window.update()
            time.sleep(1.2)
            after_icon = json.loads(geom_path.read_text(encoding="utf-8"))
            assert after_icon == moved, (
                "minimized bounds leaked into the store",
                moved, after_icon)
            friday_window.state("normal")
            friday_window.update()
        finally:
            stop.set()
            thread.join(timeout=4)
        assert not thread.is_alive(), "watchdog must honor the stop event"

    def test_tick_raises_on_save_failure(self, geom_path, friday_window,
                                         monkeypatch):
        import win32gui
        hwnd = win32gui.FindWindow(None, sui.HUD_WINDOW_TITLE)
        monkeypatch.setattr(sui, "_save_hud_geometry", lambda geo: False)
        with pytest.raises(OSError):
            sui._hud_geometry_tick(hwnd, None)

    def test_watchdog_prints_save_failure_once(
            self, geom_path, friday_window, monkeypatch, capsys):
        monkeypatch.setattr(sui, "_save_hud_geometry", lambda geo: False)
        stop = threading.Event()
        thread = threading.Thread(
            target=sui._hud_geometry_watchdog,
            kwargs={"poll": 0.2, "stop": stop}, daemon=True)
        thread.start()
        time.sleep(1.2)
        stop.set()
        thread.join(timeout=4)
        out = capsys.readouterr().out
        assert "geometry watchdog: OSError" in out, out
        assert out.count("writing hud_window.json failed") == 1, (
            "the same error must be reported once, not spammed", out)

    def test_tick_ignores_missing_hwnd(self, geom_path):
        assert sui._hud_geometry_tick(0, None) is None
        assert sui._hud_geometry_tick(None, {"x": 1}) == {"x": 1}
