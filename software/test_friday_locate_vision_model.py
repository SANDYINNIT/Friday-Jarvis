"""Round M, Phase 1 - the brain's own vision tool must not use a retired model.

THE DEFECT
----------
`friday_locate.py` (Open Interpreter's display overlay, which is what serves the
brain's `computer.display.find()` and `computer.vision.query()` inside the
spawned Jupyter kernel) defaulted its OpenRouter stage to

    meta-llama/llama-3.2-11b-vision-instruct:free

which OpenRouter has RETIRED. Measured live 2026-10-03: `GET
/api/v1/models/meta-llama/llama-3.2-11b-vision-instruct:free` returns HTTP 404, and
two consecutive brain-authored `computer.vision.query()` calls in a real screen
turn returned `vision http 404`.

A second, separate problem: the module had NO Modal stage at all, even though
software/AGENTS.md makes Modal the primary for vision ("Vision uses the same Modal
endpoint first") because it is self-hosted and has no free-tier quota.

These tests are source-and-value level on purpose: they must fail if the retired
slug reappears in ANY of the three copies that matter, without needing the
network. The provider-availability facts behind the chosen model were verified
separately and are recorded in SEARCHES.md section 55.
"""

import ast
import importlib.util
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.getcwd())

RETIRED_SLUGS = (
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "inclusionai/ling-3.0-flash-vl:free",
)

VENDORED = os.path.join(
    os.getcwd(), "tmp-oi", "open_interpreter_patched", "interpreter",
    "core", "computer", "display", "friday_locate.py",
)
INSTALLED_COPIES = (
    os.path.join(os.getcwd(), ".venv", "Lib", "site-packages", "interpreter",
                 "core", "computer", "display", "friday_locate.py"),
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs",
                 "Python", "Python311", "Lib", "site-packages", "interpreter",
                 "core", "computer", "display", "friday_locate.py"),
)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RetiredSlugTests(unittest.TestCase):
    """The retired model must not be the ACTIVE default anywhere."""

    def test_the_vendored_source_has_no_retired_default(self):
        src = open(VENDORED, encoding="utf-8").read()
        for slug in RETIRED_SLUGS:
            # Allowed inside a comment explaining the retirement; not as a value.
            self.assertNotIn('"%s"' % slug, src,
                             "%s is an active default again" % slug)

    def test_no_installed_copy_ships_a_retired_default(self):
        for path in INSTALLED_COPIES:
            if not os.path.exists(path):
                continue
            src = open(path, encoding="utf-8").read()
            for slug in RETIRED_SLUGS:
                self.assertNotIn('"%s"' % slug, src,
                                 "%s is active again in %s" % (slug, path))

    def test_the_active_openrouter_default_is_a_verified_live_model(self):
        mod = _load(VENDORED, "fl_roundm_default")
        self.assertEqual(mod.OPENROUTER_VISION_MODEL, "qwen/qwen3.8-27b:free")
        for slug in RETIRED_SLUGS:
            self.assertNotEqual(mod.OPENROUTER_VISION_MODEL, slug)


class EnvOverrideTests(unittest.TestCase):
    """The project already supported env overrides; they must survive."""

    def _reload_with(self, **env):
        import os as _os
        saved = {}
        for k, v in env.items():
            saved[k] = _os.environ.get(k)
            _os.environ[k] = v
        try:
            return _load(VENDORED, "fl_roundm_env_%d" % abs(hash(str(sorted(env)))))
        finally:
            for k, v in saved.items():
                if v is None:
                    _os.environ.pop(k, None)
                else:
                    _os.environ[k] = v

    def test_openrouter_model_override(self):
        mod = self._reload_with(FRIDAY_OPENROUTER_VISION_MODEL="my/override:free")
        self.assertEqual(mod.OPENROUTER_VISION_MODEL, "my/override:free")

    def test_modal_model_override(self):
        mod = self._reload_with(FRIDAY_MODAL_VISION_MODEL="my/modal-model")
        self.assertEqual(mod.MODAL_VISION_MODEL, "my/modal-model")

    def _modal_base_with(self, value):
        """`_modal_base()` reads the environment LAZILY, so it must be called
        while the override is still set - not after the context restores it."""
        import os as _os
        saved = _os.environ.get("FRIDAY_MODAL_BASE")
        _os.environ["FRIDAY_MODAL_BASE"] = value
        try:
            mod = _load(VENDORED, "fl_roundm_base_%s" % abs(hash(value)))
            return mod._modal_base()
        finally:
            if saved is None:
                _os.environ.pop("FRIDAY_MODAL_BASE", None)
            else:
                _os.environ["FRIDAY_MODAL_BASE"] = saved

    def test_modal_base_override_and_v1_suffixing(self):
        """Modal 404s without the trailing /v1, so the helper must add it."""
        self.assertEqual(self._modal_base_with("https://example.test"),
                         "https://example.test/v1")
        self.assertEqual(self._modal_base_with("https://example.test/v1/"),
                         "https://example.test/v1")
        self.assertEqual(self._modal_base_with("https://example.test/v1"),
                         "https://example.test/v1")

    def test_token_budget_overrides(self):
        mod = self._reload_with(FRIDAY_MODAL_VISION_MAX_TOKENS="777")
        self.assertEqual(mod.MODAL_VISION_MAX_TOKENS, 777)


class TokenBudgetTests(unittest.TestCase):
    """A reasoning model that is truncated returns an EMPTY content string.

    Measured: qwen3.8-27b at max_tokens=300 came back finish_reason="length"
    with content="" in 2 of 3 runs; at 2000 it returned parseable JSON every
    time. So the OpenRouter stage must not reuse Gemini's 300 budget."""

    def test_openrouter_budget_is_large_enough_for_reasoning_models(self):
        mod = _load(VENDORED, "fl_roundm_budget")
        self.assertGreaterEqual(mod.OPENROUTER_VISION_MAX_TOKENS, 2000)

    def test_the_payload_actually_carries_the_budget(self):
        """A constant that is never passed through is decoration."""
        mod = _load(VENDORED, "fl_roundm_payload")
        payload = mod._openai_vision_payload(b"notreallyjpeg", "p", "m", 1234)
        self.assertEqual(payload["max_tokens"], 1234)
        src = open(VENDORED, encoding="utf-8").read()
        self.assertIn("OPENROUTER_VISION_MAX_TOKENS", src)
        self.assertIn("MODAL_VISION_MAX_TOKENS", src)

    def test_gemini_budget_is_unchanged(self):
        """The Gemini stage worked; do not disturb it."""
        mod = _load(VENDORED, "fl_roundm_gemini_budget")
        self.assertEqual(
            mod._openai_vision_payload(b"x", "p", "m")["max_tokens"], 300)


class CoordinateSafetyTests(unittest.TestCase):
    """A probed model returned y_percent=950, which becomes a click far off-screen."""

    def setUp(self):
        self.mod = _load(VENDORED, "fl_roundm_point")

    def test_out_of_range_percentages_are_rejected(self):
        for bad in (950, 100.1, -1, 1e9):
            text = '{"found": true, "x_percent": 10, "y_percent": %s}' % bad
            self.assertIsNone(self.mod._point_from_text(text),
                              "accepted an out-of-range y_percent %s" % bad)

    def test_in_range_percentages_are_accepted(self):
        self.assertEqual(
            self.mod._point_from_text('{"found": true, "x_percent": 5, "y_percent": 98}'),
            (5.0, 98.0))

    def test_found_false_is_rejected(self):
        self.assertIsNone(self.mod._point_from_text('{"found": false}'))

    def test_booleans_are_not_treated_as_numbers(self):
        """True == 1 in Python; a bool coordinate must not become a click."""
        self.assertIsNone(
            self.mod._point_from_text('{"found": true, "x_percent": true, "y_percent": 50}'))

    def test_malformed_json_is_rejected(self):
        for junk in ("not json", "{broken", "", None, "[1,2,3]"):
            self.assertIsNone(self.mod._point_from_text(junk))

    def test_both_parse_sites_use_the_guard(self):
        """`locate()` previously inlined its own unchecked parse. One guard,
        two callers - or the hardening is only half applied."""
        src = open(VENDORED, encoding="utf-8").read()
        tree = ast.parse(src)
        inline = 0
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef) and node.name == "locate"):
                body = ast.unparse(node)
                if "x_percent" in body:
                    inline += 1
        self.assertEqual(inline, 0,
                         "locate() still parses coordinates inline, bypassing "
                         "_percent_in_range")
        self.assertEqual(src.count("_percent_in_range(x)"), 1)


class ModalFirstChainTests(unittest.TestCase):
    """Modal must lead, because AGENTS.md makes it the primary vision provider."""

    def setUp(self):
        self.src = open(VENDORED, encoding="utf-8").read()
        self.tree = ast.parse(self.src)

    def _chain_order(self, func_name):
        fn = next(n for n in self.tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == func_name)
        text = ast.unparse(fn)
        modal = text.find("_call_api(\n                modal_base")
        if modal == -1:
            modal = text.find("modal_base, key")
        gemini = text.find("GEMINI_BASE, key")
        openrouter = text.find("OPENROUTER_BASE, key")
        return modal, gemini, openrouter

    def test_locate_tries_modal_before_gemini_and_openrouter(self):
        modal, gemini, openrouter = self._chain_order("locate")
        self.assertNotEqual(modal, -1, "locate() has no Modal stage")
        self.assertLess(modal, gemini, "locate() must try Modal before Gemini")
        self.assertLess(modal, openrouter,
                        "locate() must try Modal before OpenRouter")

    def test_describe_image_tries_modal_first(self):
        modal, gemini, openrouter = self._chain_order("describe_image")
        self.assertNotEqual(modal, -1, "describe_image() has no Modal stage")
        self.assertLess(modal, gemini)
        self.assertLess(modal, openrouter)

    def test_modal_stage_is_skipped_when_not_configured(self):
        """Zero keys must mean zero wasted attempts, not a crash."""
        fn = next(n for n in self.tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "locate")
        text = ast.unparse(fn)
        self.assertIn("_modal_keys(creds) if modal_base else []", text)

    def test_gemini_fallback_is_preserved(self):
        """FRIDAY must still work with zero cloud keys - the local OCR path and
        the Gemini stage must not be removed by adding Modal."""
        self.assertIn("GEMINI_CHAIN", self.src)
        mod = _load(VENDORED, "fl_roundm_chain")
        self.assertEqual(len(mod.GEMINI_CHAIN), 5)

    def test_local_ocr_find_text_is_untouched(self):
        """`find_text_locally` is pure pytesseract and must make no remote call."""
        fn = next(n for n in self.tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "find_text_locally")
        text = ast.unparse(fn)
        self.assertNotIn("_call_api", text)
        self.assertIn("pytesseract", text)


class InstalledCopiesMatchTests(unittest.TestCase):
    """The kernel and the server must run the SAME patched file."""

    def test_installed_copies_are_byte_identical_to_the_vendored_source(self):
        want = open(VENDORED, "rb").read()
        for path in INSTALLED_COPIES:
            if not os.path.exists(path):
                continue
            with open(path, "rb") as handle:
                self.assertEqual(
                    handle.read(), want,
                    "%s differs from the vendored patched source; a re-apply of "
                    "the patch is required for the fix to take effect" % path)

    def test_the_module_the_service_imports_has_the_fix(self):
        """Import it the way the running service does, not by file path."""
        code = (
            "import interpreter.core.computer.display.friday_locate as m;"
            "print(m.OPENROUTER_VISION_MODEL);"
            "print('modal_stage', len(m._modal_keys()) > 0)"
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        self.assertIn("qwen/qwen3.8-27b:free", out.stdout)
        self.assertIn("modal_stage True", out.stdout)


if __name__ == "__main__":
    unittest.main()