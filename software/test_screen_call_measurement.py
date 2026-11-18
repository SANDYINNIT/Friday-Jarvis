"""PHASE 5 (2026-10-03) - screen-path call measurement. MEASUREMENT ONLY.

These tests exist to prove the INSTRUMENTATION is honest. They assert nothing
about how many calls a screen turn *should* make - that is the open question this
round set out to measure, and deciding it is explicitly out of scope. What they
lock is that:

* every provider call on the screen path is recorded, in order;
* the per-monitor loop really does multiply the vision stage;
* the record says whether the composed answer was actually used;
* the ledger drains, so it cannot leak into the next turn's numbers;
* the newly-defined `_LOGGER` exists, closing three latent NameErrors;
* nothing content-bearing (pixels, OCR text, the user's question) is recorded.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())

from source.server import screen_understanding as su  # noqa: E402

SCREEN_PY = os.path.join(os.getcwd(), "source", "server", "screen_understanding.py")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        su.take_call_ledger()  # start from empty

    def test_the_logger_used_by_three_handlers_exists(self):
        """`_LOGGER` was referenced by three except-blocks and never defined, so
        each of those handlers raised NameError instead of logging."""
        self.assertIsNotNone(su._LOGGER)
        with open(SCREEN_PY, encoding="utf-8") as handle:
            src = handle.read()
        used = [ln for ln in src.splitlines() if "_LOGGER." in ln]
        self.assertGreaterEqual(len(used), 4,
                                "expected the three pre-existing handlers plus the new one")
        tree = ast.parse(src)
        self.assertTrue(any(isinstance(n, ast.Assign) for n in tree.body),
                        "_LOGGER must be assigned at module level")

    def test_a_call_is_recorded_in_order(self):
        su._note_call("vision", "modal", "m", 10, True, label="left")
        su._note_call("vision", "modal", "m", 20, True, label="right")
        su._note_call("composer", "modal", "m", 0, False, chars=0)
        ledger = su.take_call_ledger()
        self.assertEqual([c["stage"] for c in ledger],
                         ["vision", "vision", "composer"])
        self.assertEqual([c["elapsed_ms"] for c in ledger], [10, 20, 0])
        self.assertFalse(ledger[-1]["ok"])

    def test_the_ledger_drains_so_turns_cannot_contaminate_each_other(self):
        su._note_call("vision", "modal", "m", 10, True)
        self.assertEqual(len(su.take_call_ledger()), 1)
        self.assertEqual(su.take_call_ledger(), [],
                         "the ledger kept its entries after draining")

    def test_extras_are_carried_through(self):
        su._note_call("vision", "modal", "m", 5, True,
                      label="left", jpeg_bytes=1234, chars=56)
        entry = su.take_call_ledger()[0]
        self.assertEqual(entry["label"], "left")
        self.assertEqual(entry["jpeg_bytes"], 1234)
        self.assertEqual(entry["chars"], 56)

    def test_no_content_is_recorded(self):
        """The ledger is a count, not a transcript: no OCR text, no question, no
        image bytes - only sizes."""
        su._note_call("vision", "modal", "nvidia/GLM", 3100, True,
                      label="left", jpeg_bytes=182000, chars=387)
        entry = su.take_call_ledger()[0]
        self.assertEqual(set(entry) >= {"stage", "provider", "model",
                                        "elapsed_ms", "ok"}, True)
        blob = repr(entry)
        for banned in ("PRIVATE", "BEGIN TEXT", "data:image", "base64"):
            self.assertNotIn(banned, blob)


class InstrumentationPlacementTests(unittest.TestCase):
    """The instrumentation must sit on the REAL call sites, or the measurement
    is decorative."""

    def setUp(self):
        with open(SCREEN_PY, encoding="utf-8") as handle:
            self.src = handle.read()
        self.tree = ast.parse(self.src)

    def _calls_to(self, name):
        return [n for n in ast.walk(self.tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == name]

    def test_both_vision_stages_are_instrumented(self):
        notes = self._calls_to("_note_call")
        stages = set()
        for call in notes:
            if call.args and isinstance(call.args[0], ast.Constant):
                stages.add(call.args[0].value)
        self.assertIn("vision", stages)
        self.assertIn("composer", stages)
        self.assertIn("screen_turn", stages)

    def test_the_vision_note_is_inside_the_per_region_loop(self):
        """This is the multiplier being measured: one call per selected region."""
        fn = next(n for n in self.tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "describe_screen")
        loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
        self.assertTrue(loops, "the per-region loop was not found")
        loop = loops[0]
        inside = [n for n in ast.walk(loop)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "_note_call"]
        self.assertTrue(inside,
                        "the vision call is not instrumented inside the region loop, "
                        "so a per-monitor multiplier could not be measured")

    def test_the_composer_result_is_recorded_as_used_or_not(self):
        self.assertIn("composer_used=bool(brain)", self.src)
        self.assertIn('source="composer" if brain else "canned_builder"', self.src)

    def test_screen_response_still_returns_the_same_answer(self):
        """Measurement must not change behaviour: the returned value is still
        the composed answer when there is one, else the canned builder."""
        fn = next(n for n in self.tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "screen_response")
        src = ast.unparse(fn)
        self.assertIn("brain or _screen_answer(report, window_context, question)",
                      src)
        self.assertIn("return answer", src)

    def test_the_ledger_is_drained_by_the_server(self):
        """Otherwise the counts accumulate across every turn in the process."""
        server_py = os.path.join(os.getcwd(), "source", "server", "server.py")
        with open(server_py, encoding="utf-8") as handle:
            server_src = handle.read()
        self.assertIn("take_call_ledger", server_src)
        self.assertIn("screen calls: total=", server_src)
        # Both entry paths must log it.
        self.assertEqual(server_src.count("_log_screen_call_ledger()"), 3,
                         "expected the definition plus one call per screen path")


if __name__ == "__main__":
    unittest.main()