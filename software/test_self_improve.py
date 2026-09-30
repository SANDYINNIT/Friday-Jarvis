"""Tests for self_improve: lessons ledger, ambient probe, prompt blocks."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "source")

from server import self_improve


class Lessons(unittest.TestCase):
    def setUp(self):
        self.tmp = self.mktmp()

    def tearDown(self):
        self.stop()

    def stop(self):
        pass

    def mktmp(self):
        import tempfile

        path = os.path.join(
            tempfile.mkdtemp(prefix="friday_lessons_"), "lessons.md"
        )
        self._old_path = self_improve.lessons_path
        patcher = patch.object(
            self_improve, "lessons_path", lambda: path
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def test_append_creates_header_and_dedupes(self):
        self.assertTrue(self_improve.append_lesson_note("Edge path learned"))
        self.assertTrue(self_improve.append_lesson_note("diff strategy pivots"))
        with open(self.tmp, encoding="utf-8") as handle:
            content = handle.read()
        self.assertTrue(content.startswith("# FRIDAY lessons"))
        self.assertIn("Edge path learned", content)
        self.assertEqual(content.count("lessons (self-maintained)"), 1)
        notes = [ln for ln in content.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(notes), 2)

    def test_append_no_duplicate(self):
        self_improve.append_lesson_note("same idea repeated entry")
        self.assertFalse(self_improve.append_lesson_note("same idea repeated entry"))

    def test_read_lessons_fresh(self):
        self_improve.append_lesson_note("fresh read probe")
        text = self_improve.read_lessons()
        self.assertIn("fresh read probe", text)
        self.assertIn("# FRIDAY lessons", text)

    def test_prompt_block_braces_balanced(self):
        blocks = self_improve._LESSONS_BLOCK
        self.assertTrue("{{" in blocks and "}}" in blocks)
        import re as _re
        parts = _re.split(r"({{.*?}})", blocks, flags=_re.DOTALL)
        self.assertTrue(any(part.startswith("{{") for part in parts))


class AmbientProbe(unittest.TestCase):
    def test_probe_and_remember(self):
        recorded = {}

        class Store:
            def remember(self, **kwargs):
                recorded.update(kwargs)

        with patch.object(self_improve.time, "strftime",
                          return_value="2026-09-17 10:00"), \
             patch.object(self_improve, "_probe_snapshot",
                          return_value="pc_state 10:00: RAM 40% CPU 12% top=a.exe,b.exe"):
            line = self_improve.probe_and_remember(Store())
        self.assertIn("pc_state", line)
        self.assertEqual(recorded["topic"], "pc_state")
        self.assertEqual(recorded["source"], "ambient_probe")


class ProbeSnapshot(unittest.TestCase):
    def test_snapshot_smoke(self):
        try:
            import psutil  # noqa

            installed = True
        except ImportError:
            installed = False
        if not installed:
            self.skipTest("psutil not installed")
        line = self_improve._probe_snapshot()
        self.assertIn("pc_state", line)
        self.assertIn("top=", line)


if __name__ == "__main__":
    unittest.main()


class Scratchpad(unittest.TestCase):
    def setUp(self):
        import tempfile

        scratch = os.path.join(tempfile.mkdtemp(prefix="friday_scratch_"), "scratchpad.md")
        shots = tempfile.mkdtemp(prefix="friday_shots_")
        self._p1 = patch.object(self_improve, "scratchpad_path", lambda: scratch)
        self._p2 = patch.object(self_improve, "scratchdir_path", lambda: shots)
        for p in (self._p1, self._p2):
            p.start()
            self.addCleanup(p.stop)

    def test_reset_note_read_roundtrip(self):
        self_improve.scratchpad_reset()
        self_improve.scratchpad_note("took screenshot", tag="capture", )
        self_improve.scratchpad_note("locate found (10,20)", tag="locate")
        text = self_improve.read_scratchpad()
        self.assertIn("capture", text)
        self.assertIn("locate found (10,20)", text)
        self.assertIn("took screenshot", text)

    def test_reset_clears(self):
        self_improve.scratchpad_note("stale")
        self_improve.scratchpad_reset()
        self.assertEqual(self_improve.read_scratchpad(), "")

    def test_screenshot_folder_prunes(self):
        directory = self_improve.scratchdir_path()
        import time as _t
        for i in range(25):
            path = os.path.join(directory, f"shot{i:03d}.png")
            open(path, "w").write("x")
            _t.sleep(0.01)
        out = self_improve.save_screenshot_folder()
        self.assertEqual(out, directory)
        self.assertEqual(len(os.listdir(directory)), 20)
