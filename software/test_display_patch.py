"""Tests for display_patch: OI /point/ replacement (vision+OCR pointing)."""

import json
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, "source")

from server import display_patch


class FakeRGB:
    def save(self, buf, format=None):
        buf.write(b"jpegbytes")


class FakeScreen:
    size = (1920, 1080)

    def convert(self, mode):
        return FakeRGB()


def _ok_cloud(jpeg, prompt):
    return True, json.dumps({"found": True, "x_percent": 25.0, "y_percent": 50.0}), "vision_gemini/gemini-3.8-flash", ""


def _not_found_cloud(jpeg, prompt):
    return True, json.dumps({"found": False}), "vision_gemini/gemini-3.8-flash", ""


def _patched_interceptor_registry():
    return None


def _patched_interpreter():
    interpreter = MagicMock()
    interpreter.computer.display = MagicMock()
    return interpreter


class FindHelpers(unittest.TestCase):
    def _install(self, cloud_side_effect, local_result):
        interpreter = _patched_interpreter()
        with patch.object(display_patch.ImageGrab, "grab", return_value=FakeScreen()), \
             patch.object(display_patch, "_su") as fake_su:
            fake_su.select_vision_model.return_value = "gemma4:e2b"
            fake_su._local_vision.return_value = local_result
            fake_su._cloud_vision.side_effect = cloud_side_effect
            display_patch.install_display_helpers(interpreter)
        return interpreter

    def test_install_marks_patched(self):
        interpreter = self._install((False, None, "", "offline"), (False, None, "x"))
        display_obj = interpreter.computer.display
        self.assertTrue(display_obj._friday_patched)
        self.assertEqual(display_obj.find("Geometry Dash icon"), [])

    def test_find_converts_percent_to_pixels(self):
        interpreter = self._install(_ok_cloud, (False, None, "busy"))
        results = interpreter.computer.display.find("some icon")
        self.assertEqual(len(results), 1)
        x, y = results[0]["coordinates"]
        self.assertEqual((x, y), (int(1920 * 25.0 / 100), int(1080 * 50.0 / 100)))

    def test_find_not_found_returns_empty(self):
        interpreter = self._install(_not_found_cloud, (True, "some text", "local"))
        self.assertEqual(interpreter.computer.display.find("missing thing"), [])


if __name__ == "__main__":
    unittest.main()
