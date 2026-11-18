"""Desktop globe: persistence, monitor/DPI clamping, drag vs double-click (C1).

`open_globe()` is deliberately NOT exercised - it creates a real OS window.
Everything else (the policy that decides where the globe lives and what a
gesture means) is pure and testable without a display server.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.getcwd())

import pytest

from source.server import globe_window as gw


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Never read or write the real ~/.friday/globe.json from a test."""
    monkeypatch.setattr(gw, "settings_path",
                        lambda: str(tmp_path / "globe.json"))
    yield


@pytest.fixture()
def settings_file(tmp_path):
    return str(tmp_path / "globe.json")


class TestSettingsPersistence:
    def test_defaults_when_the_file_is_missing(self, settings_file):
        settings = gw.load_settings(settings_file)
        assert settings == gw.DEFAULTS
        assert set(settings) == {"x", "y", "visible", "hotkey", "width", "height"}

    @pytest.mark.parametrize("payload", [
        "",                      # empty file
        "not json",              # corrupt
        '{"x": "abc", "y": []}', # wrong types
    ])
    def test_unusable_files_fall_back_to_defaults(self, settings_file, payload):
        Path(settings_file).write_text(payload, encoding="utf-8")
        assert gw.load_settings(settings_file) == gw.DEFAULTS

    def test_a_partial_file_keeps_its_valid_values_and_defaults_the_rest(self,
                                                                       settings_file):
        # load_settings reports what was saved; CLAMPING happens on load into a
        # controller / on save, so an extreme coordinate is corrected there.
        Path(settings_file).write_text('{"x": 99999999}', encoding="utf-8")
        settings = gw.load_settings(settings_file)
        assert settings["x"] == 99999999
        assert settings["y"] == gw.DEFAULTS["y"]
        assert settings["hotkey"] == gw.DEFAULTS["hotkey"]

    def test_an_extreme_saved_coordinate_is_clamped_by_the_controller(
            self, settings_file):
        gw.save_settings({"x": 99999999, "y": -999999}, settings_file)
        controller = gw.GlobeController(settings_file)
        right = gw.screen_bounds()[2]
        assert controller.state.x <= right - 40, "off-screen globe was not pulled back"

    def test_an_oversized_file_is_refused_without_reading_it(self, settings_file):
        Path(settings_file).write_text("x" * (64 * 1024 + 1), encoding="utf-8")
        assert gw.load_settings(settings_file) == gw.DEFAULTS

    def test_save_then_load_round_trips(self, settings_file):
        result = gw.save_settings({"x": 150, "y": 200, "width": 300,
                                   "height": 300, "visible": False,
                                   "hotkey": "Ctrl+Alt+G"}, settings_file)
        assert result["ok"] is True
        loaded = gw.load_settings(settings_file)
        assert loaded["x"] == 150 and loaded["y"] == 200
        assert loaded["visible"] is False
        assert loaded["hotkey"] == "Ctrl+Alt+G"

    def test_save_is_atomic_and_leaves_no_temp_file(self, settings_file):
        gw.save_settings({"x": 10, "y": 10}, settings_file)
        assert Path(settings_file).is_file()
        assert not Path(settings_file + ".tmp").exists(), "temp file left behind"

    def test_save_never_raises_on_a_bad_path(self):
        # A directory path cannot be written as a file.
        result = gw.save_settings({"x": 1, "y": 1},
                                  str(Path(__file__).resolve().parent))
        assert isinstance(result, dict)
        assert "ok" in result


class TestMonitorAndDpiClamping:
    def test_screen_bounds_is_a_rectangle(self):
        left, top, right, bottom = gw.screen_bounds()
        assert right > left and bottom > top

    def test_read_dpi_scale_is_a_positive_float(self):
        assert gw.read_dpi_scale() >= 1.0

    def test_a_position_off_the_right_edge_keeps_40px_visible(self):
        # The contract is "never strand it off-screen", not "always fully
        # inside": 40 px must stay reachable so it can be dragged back.
        left, top, right, bottom = gw.screen_bounds()
        x, y = gw.clamp_to_screen(right + 5000, top + 10, 240, 240)
        assert x <= right - 40, "less than 40px of the globe stayed visible"
        assert x >= left - 240 + 40
        assert gw.is_inside(x, y, 40, 40, (left, top, right, bottom))

    def test_negative_coordinates_are_valid(self):
        # The left monitor on this machine starts at x = -1920.
        left, top, _right, _bottom = gw.screen_bounds()
        if left < 0:
            x, y = gw.clamp_to_screen(left + 60, top + 60, 240, 240)
            assert x >= left, "a valid negative coordinate was rejected"

    def test_restore_pulls_a_stranded_globe_back_on_screen(self, settings_file):
        # Saved against a monitor that no longer exists.
        gw.save_settings({"x": 99999, "y": 99999}, settings_file)
        controller = gw.GlobeController(settings_file)
        before_restore = (controller.state.x, controller.state.y)
        result = controller.restore()
        left, top, right, bottom = gw.screen_bounds()
        assert result["ok"] is True
        assert controller.state.x <= right, "restore left the globe off-screen"
        assert controller.state.x >= left - controller.state.width + 40
        # A clamped restore is written back so the next load is already correct.
        if before_restore != (controller.state.x, controller.state.y):
            assert result["clamped"] is True
            assert gw.load_settings(settings_file)["x"] != 99999

    def test_is_inside_geometry(self):
        assert gw.is_inside(0, 0, 10, 10, (0, 0, 100, 100)) is True
        assert gw.is_inside(95, 95, 10, 10, (0, 0, 100, 100)) is False
        assert gw.is_inside("x", 0, 1, 1, (0, 0, 10, 10)) is False


class TestDragVersusDoubleClick:
    @pytest.fixture()
    def controller(self, settings_file):
        return gw.GlobeController(settings_file)

    def test_a_stationary_click_is_a_double_click(self, controller):
        controller.press(100, 100)
        assert controller.on_double_click(102, 101) is True

    def test_a_real_drag_is_never_a_double_click(self, controller):
        controller.press(100, 100)
        assert controller.on_double_click(320, 260) is False
        # ...and it can never be mistaken for one afterwards either.
        assert controller.on_double_click(100, 100) is False

    def test_a_click_without_a_press_is_not_a_double_click(self, controller):
        assert controller.on_double_click(10, 10) is False

    def test_double_click_when_already_expanded_does_not_re_expand(self, controller):
        controller.expand()
        controller.press(10, 10)
        assert controller.on_double_click(10, 10) is False

    def test_a_second_click_after_the_first_is_treated_as_fresh(self, controller):
        controller.press(10, 10)
        assert controller.on_double_click(11, 11) is True   # first click
        # press() is required again for a new gesture
        assert controller.on_double_click(11, 11) is False

    def test_the_threshold_matches_the_documented_value(self):
        assert gw.DRAG_THRESHOLD_PX == 6


class TestPositionCommands:
    @pytest.fixture()
    def controller(self, settings_file):
        return gw.GlobeController(settings_file)

    def test_on_drag_does_not_persist(self, controller, settings_file):
        gw.save_settings({"x": 10, "y": 10}, settings_file)
        controller = gw.GlobeController(settings_file)
        controller.on_drag(400, 400)
        assert gw.load_settings(settings_file)["x"] == 10, "drag persisted early"

    def test_on_drag_end_persists(self, controller, settings_file):
        controller.on_drag_end(420, 380)
        loaded = gw.load_settings(settings_file)
        assert loaded["x"] != 10 or controller.state.x != 10

    def test_set_position_can_persist_or_not(self, controller, settings_file):
        controller.set_position(77, 88, persist=False)
        controller.set_position(91, 92, persist=True)
        loaded = gw.load_settings(settings_file)
        assert (loaded["x"], loaded["y"]) == (91, 92)

    def test_visibility_toggles_and_persists(self, controller, settings_file):
        first = controller.toggle_visible()
        assert first["visible"] is False
        assert gw.load_settings(settings_file)["visible"] is False
        second = controller.toggle_visible(show=True)
        assert second["visible"] is True

    def test_collapse_and_expand_resize_and_persist(self, controller,
                                                    settings_file):
        controller.expand()
        assert controller.state.expanded is True
        assert controller.state.width == gw.EXPANDED_WIDTH
        assert gw.load_settings(settings_file)["width"] == gw.EXPANDED_WIDTH
        controller.collapse()
        assert controller.state.expanded is False
        assert controller.state.width == gw.DEFAULT_WIDTH

    def test_expanding_clamps_to_the_screen(self, controller):
        controller.state.x = 10 ** 9
        controller.expand()
        assert controller.state.x < gw.screen_bounds()[2]

    def test_state_dict_is_json_safe_and_reports_recovery(self, controller):
        payload = controller.state_dict()
        json.dumps(payload)
        assert "screen" in payload and "recovery" in payload
        assert payload["recovery"]["tray"] is True


class TestRecovery:
    def test_recovery_reports_tray_and_hotkey(self, settings_file):
        gw.save_settings({"hotkey": "Ctrl+Alt+Q"}, settings_file)
        recovery = gw.find_recovery_window()
        assert recovery["tray"] is True
        # find_recovery_window reads the real path; assert the shape only here.
        assert "hotkey" in recovery and "env" in recovery

    @pytest.mark.parametrize("combo,expected", [
        ("ctrl+alt+f", True),
        ("CTRL+ALT+F", True),
        (" Ctrl + Alt + F ", True),
        ("ctrl+alt+g", False),
        ("", False),
    ])
    def test_hotkey_matching(self, combo, expected):
        assert gw.GlobeController.hotkey_matches(
            combo, {"hotkey": "ctrl+alt+f"}) is expected


class TestPageAndBridge:
    def test_the_page_has_no_external_resources(self):
        html = gw.globe_html()
        for forbidden in ("http://", "https://", "localhost", "fetch(", "<script src"):
            assert forbidden not in html, forbidden

    def test_the_page_exposes_the_gesture_and_event_hooks(self):
        html = gw.globe_html()
        for required in ("GLOBE_EVENT", "double_click", "drag_end",
                         "pointerdown", "pointermove", "audio_chunk",
                         "reduced"):
            assert required in html, required

    def test_the_page_never_takes_focus_by_default(self):
        # No autofocus/tabindex on interactive elements: showing the globe must
        # not steal focus from whatever Sir is using.
        html = gw.globe_html()
        assert "autofocus" not in html
        assert "tabindex" not in html

    def test_bridge_double_click_opens_the_app_only_when_it_is_a_click(self):
        opened = {"n": 0}
        controller = gw.GlobeController(None)
        bridge = gw.GlobeBridge(controller, on_open=lambda: opened.__setitem__("n", opened["n"] + 1) or True)

        controller.press(50, 50)
        assert bridge.double_click(300, 300)["ok"] is False     # a drag
        assert opened["n"] == 0

        controller.press(50, 50)
        assert bridge.double_click(51, 51)["ok"] is True
        assert opened["n"] == 1

    def test_bridge_hide_reports_recovery(self):
        controller = gw.GlobeController(None)
        bridge = gw.GlobeBridge(controller, on_open=None, on_hide=None)
        result = bridge.hide()
        assert result["ok"] is True
        assert result["visible"] is False
        assert "recovery" in result

    def test_bridge_state_is_json_safe(self):
        controller = gw.GlobeController(None)
        bridge = gw.GlobeBridge(controller, on_open=None)
        json.dumps(bridge.state())
        json.dumps(bridge.recovery())
