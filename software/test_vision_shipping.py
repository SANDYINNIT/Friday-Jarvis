"""Round N - the vision fix must actually SHIP, and the screen fallback must be valid.

Two shipping defects were open at the end of Round M:

1. `tmp-oi/open_interpreter-0.4.3-py3-none-any.whl` still baked the OLD
   `friday_locate.py`, so a fresh install from the freeze could resurrect the
   retired OpenRouter slug `meta-llama/llama-3.2-11b-vision-instruct:free`.
2. `screen_understanding.py` still defaulted to
   `inclusionai/ling-3.0-flash-vl:free`, which is ALSO 404 - so the screen
   pipeline's cloud fallback was dead too.

These tests inspect the REAL wheel archive (not a mock) and the REAL module
defaults, and they lock the Round M screen-evidence architecture so it cannot be
undone while the vision paths are being repaired.
"""

import ast
import base64
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

# Anchor to THIS FILE, never to os.getcwd(): another test in the suite chdirs
# into a temp directory, so a cwd-relative path resolves to the wrong tree and
# these source/wheel assertions fail for a reason that has nothing to do with
# what they check.
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

RETIRED = (
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "inclusionai/ling-3.0-flash-vl:free",
)
LOCATE = "qwen/qwen3.8-27b:free"
DESCRIBE = "dots-studio/dots-3-note-preview:free"
WHEEL = os.path.join(ROOT, "tmp-oi", "open_interpreter-0.4.3-py3-none-any.whl")
TREE = os.path.join(ROOT, "vendor", "open-interpreter")
MEMBER = "interpreter/core/computer/display/friday_locate.py"
VENDORED = os.path.join(TREE, MEMBER.replace("/", os.sep))
SU_PY = os.path.join(ROOT, "source", "server", "screen_understanding.py")
KDP_PY = os.path.join(ROOT, "source", "server", "kernel_display_patch.py")
SUI_PY = os.path.join(ROOT, "source", "server", "status_ui.py")
SERVER_PY = os.path.join(ROOT, "source", "server", "server.py")


def _sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _src(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ===========================================================================
# A. WHEEL SHIPPING - inspect the real archive
# ===========================================================================


class WheelArchiveTests(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(WHEEL):
            self.skipTest("no vendored wheel at %s" % WHEEL)
        self.zf = zipfile.ZipFile(WHEEL)

    def tearDown(self):
        self.zf.close()

    def test_the_wheel_exists_and_is_a_readable_archive(self):
        bad = self.zf.testzip()
        self.assertIsNone(bad, "the wheel is corrupt at %s" % bad)
        self.assertGreater(len(self.zf.namelist()), 100)

    def test_the_wheel_ships_friday_locate(self):
        self.assertIn(MEMBER, self.zf.namelist(),
                      "the wheel does not contain %s" % MEMBER)

    def test_the_wheel_member_is_not_the_stale_implementation(self):
        """THE defect this round exists to close."""
        body = self.zf.read(MEMBER).decode("utf-8")
        for slug in RETIRED:
            self.assertNotIn('"%s",' % slug, body,
                             "the wheel still ships %s as an ACTIVE default" % slug)

    def test_the_wheel_member_matches_the_vendored_source_byte_for_byte(self):
        shipped = self.zf.read(MEMBER)
        with open(VENDORED, "rb") as handle:
            vendored = handle.read()
        self.assertEqual(_sha_bytes(shipped), _sha_bytes(vendored),
                         "wheel member and vendored source have drifted; the "
                         "wheel must be rebuilt from the tree")

    def test_the_wheel_ships_the_corrected_default_and_modal_stage(self):
        body = self.zf.read(MEMBER).decode("utf-8")
        self.assertIn(LOCATE, body)
        self.assertIn("MODAL_VISION_MODEL", body,
                      "the wheel does not ship the Modal-first stage")

    def test_the_wheel_records_the_retired_slug_only_in_a_comment(self):
        """A mention is fine - it documents the retirement. A default is not."""
        body = self.zf.read(MEMBER).decode("utf-8")
        for line in body.splitlines():
            if RETIRED[0] in line:
                self.assertTrue(line.lstrip().startswith("#"),
                                "the retired slug appears in live code: %r" % line)

    def test_the_wheel_record_is_self_consistent(self):
        """Every archive member listed in RECORD with a matching digest."""
        record_names = [n for n in self.zf.namelist() if n.endswith("/RECORD")]
        self.assertEqual(len(record_names), 1,
                         "expected exactly one RECORD, found %s" % record_names)
        record = self.zf.read(record_names[0]).decode("utf-8")
        listed = {}
        for line in record.splitlines():
            if not line.strip():
                continue
            arc, digest, size = line.rsplit(",", 2)
            listed[arc] = (digest, size)
        self.assertEqual(set(listed), set(self.zf.namelist()),
                         "RECORD and the archive disagree on their contents")
        for arc, (digest, size) in listed.items():
            if arc == record_names[0]:
                self.assertEqual((digest, size), ("", ""),
                                 "RECORD must list itself with no hash/size")
                continue
            data = self.zf.read(arc)
            raw = hashlib.sha256(data).digest()
            want = "sha256=" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
            self.assertEqual(digest, want, "digest mismatch for %s" % arc)
            self.assertEqual(int(size), len(data), "size mismatch for %s" % arc)

    def test_the_wheel_has_no_duplicate_entries(self):
        names = self.zf.namelist()
        self.assertEqual(len(names), len(set(names)),
                         "duplicate entries: %s"
                         % [n for n in set(names) if names.count(n) > 1])

    def test_the_wheel_ships_no_stale_bytecode(self):
        """The previous wheel carried 3 committed .pyc files (cpython-311 and
        cpython-312 caches). They are version-specific and must not ship."""
        junk = [n for n in self.zf.namelist()
                if n.endswith((".pyc", ".pyo")) or "__pycache__" in n]
        self.assertEqual(junk, [], "the wheel ships bytecode: %s" % junk)

    def test_the_wheel_carries_no_machine_specific_paths(self):
        """A shipped wheel must not embed this machine's usernames or drives."""
        leaks = []
        for name in self.zf.namelist():
            if not name.endswith((".py", ".txt", ".md", ".json", ".cfg")):
                continue
            body = self.zf.read(name).decode("utf-8", "replace")
            for needle in ("Z:\\private-runtime", "C:\\Users\\user", "private-deployment--"):
                if needle in body:
                    leaks.append((name, needle))
        self.assertEqual(leaks, [], "machine-specific content would ship: %s" % leaks)

    def test_the_wheel_still_declares_its_tags_and_generator(self):
        wheel_meta = [n for n in self.zf.namelist() if n.endswith("/WHEEL")][0]
        body = self.zf.read(wheel_meta).decode("utf-8")
        self.assertIn("Wheel-Version:", body)
        self.assertIn("Tag: py3-none-any", body)
        self.assertIn("Root-Is-Purelib: true", body)

    def test_pip_can_install_the_rebuilt_wheel_and_the_file_survives(self):
        """End-to-end: install into a throwaway target and compare bytes.

        This is the acceptance test that a successful build command alone cannot
        give - it inspects what pip actually wrote to disk."""
        target = os.path.join(tempfile.gettempdir(), "opencode", "roundn-wheeltest")
        if os.path.exists(target):
            import shutil
            shutil.rmtree(target)
        os.makedirs(target, exist_ok=True)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps",
                 "--target", target, WHEEL],
                capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0,
                             "pip rejected the wheel: %s" % (r.stderr[-400:],))
            installed = os.path.join(target, MEMBER.replace("/", os.sep))
            self.assertTrue(os.path.exists(installed),
                            "pip did not install %s" % MEMBER)
            with open(installed, "rb") as handle:
                got = handle.read()
            with open(VENDORED, "rb") as handle:
                want = handle.read()
            self.assertEqual(_sha_bytes(got), _sha_bytes(want),
                             "the INSTALLED friday_locate.py differs from the "
                             "vendored source")
            text = got.decode("utf-8")
            for slug in RETIRED:
                self.assertNotIn('"%s",' % slug, text,
                                 "pip installed the retired slug as a default")
        finally:
            import shutil
            shutil.rmtree(target, ignore_errors=True)


class ThreeCopyInvariantTests(unittest.TestCase):
    """vendored source == venv copy == Python311 kernel copy."""

    def _copies(self):
        return [
            VENDORED,
            os.path.join(ROOT, ".venv", "Lib", "site-packages",
                         "interpreter", "core", "computer", "display",
                         "friday_locate.py"),
            os.path.expanduser(
                r"~\AppData\Local\Programs\Python\Python311\Lib\site-packages"
                r"\interpreter\core\computer\display\friday_locate.py"),
        ]

    def test_all_three_copies_are_byte_identical(self):
        digests = {}
        for path in self._copies():
            if not os.path.exists(path):
                continue
            with open(path, "rb") as handle:
                digests[path] = _sha_bytes(handle.read())
        self.assertGreaterEqual(len(digests), 2,
                                "expected at least two copies on this machine")
        self.assertEqual(len(set(digests.values())), 1,
                         "the friday_locate.py copies have diverged: %s" % digests)

    def test_every_copy_matches_the_wheel(self):
        """The strongest form: what runs, what ships, and what is vendored are
        all the same bytes."""
        with zipfile.ZipFile(WHEEL) as zf:
            shipped = zf.read(MEMBER)
        want = _sha_bytes(shipped)
        for path in self._copies():
            if not os.path.exists(path):
                continue
            with open(path, "rb") as handle:
                self.assertEqual(_sha_bytes(handle.read()), want,
                                 "%s does not match the shipped wheel" % path)


# ===========================================================================
# B. VISION FALLBACK CONTRACT
# ===========================================================================


class ScreenOpenRouterFallbackTests(unittest.TestCase):
    """screen_understanding.py consumes FREE TEXT, not coordinates."""

    def setUp(self):
        from source.server import screen_understanding as su
        self.su = su

    def test_the_dead_slug_is_not_the_default(self):
        for slug in RETIRED:
            self.assertNotEqual(self.su.OPENROUTER_VISION_MODEL, slug,
                                "screen_understanding still defaults to the "
                                "retired %s" % slug)

    def test_the_default_is_the_probed_description_model(self):
        self.assertEqual(self.su.OPENROUTER_VISION_MODEL, DESCRIBE)

    def test_it_is_not_the_locate_model(self):
        """The two consumers were probed separately and MUST NOT share a
        default by accident: qwen3.8-27b answers JSON coordinates but burns the
        whole budget on reasoning for a description, returning empty content."""
        self.assertNotEqual(self.su.OPENROUTER_VISION_MODEL, LOCATE)

    def test_the_env_override_still_wins(self):
        import importlib
        os.environ["FRIDAY_OPENROUTER_VISION_MODEL"] = "my/override:free"
        os.environ["FRIDAY_OPENROUTER_VISION_MAX_TOKENS"] = "1234"
        try:
            reloaded = importlib.reload(self.su)
            self.assertEqual(reloaded.OPENROUTER_VISION_MODEL, "my/override:free")
            self.assertEqual(reloaded.OPENROUTER_VISION_MAX_TOKENS, 1234)
        finally:
            os.environ.pop("FRIDAY_OPENROUTER_VISION_MODEL", None)
            os.environ.pop("FRIDAY_OPENROUTER_VISION_MAX_TOKENS", None)
            importlib.reload(self.su)

    def test_the_openrouter_stage_gets_its_own_token_budget(self):
        """At the shared 300 this model returns HTTP 200 with EMPTY content
        (measured 0/4), which is indistinguishable from a broken model."""
        self.assertGreaterEqual(self.su.OPENROUTER_VISION_MAX_TOKENS, 2000)
        grown = self.su._openai_vision_payload(
            b"jpeg", "prompt", "model", self.su.OPENROUTER_VISION_MAX_TOKENS)
        self.assertEqual(grown["max_tokens"], self.su.OPENROUTER_VISION_MAX_TOKENS)

    def test_the_gemini_budget_is_unchanged(self):
        """Gemini worked at 300; do not disturb it."""
        default = self.su._openai_vision_payload(b"jpeg", "prompt", "model")
        self.assertEqual(default["max_tokens"], 300)

    def test_the_budget_is_actually_passed_at_the_call_site(self):
        """A constant that is never wired through is decoration."""
        body = _src(SU_PY)
        self.assertIn("OPENROUTER_VISION_MAX_TOKENS)", body,
                      "the OpenRouter stage does not receive its budget")

    def test_modal_is_still_first_and_the_order_is_unchanged(self):
        body = _src(SU_PY)
        tree = ast.parse(body)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_cloud_vision")
        text = ast.unparse(fn)
        modal = text.find("_modal_vision(")
        gemini = text.find("_gemini_vision(")
        openrouter = text.find("VISION_OPENROUTER_POOL")
        self.assertNotEqual(modal, -1, "no Modal stage")
        self.assertLess(modal, gemini, "Modal must stay first")
        self.assertLess(gemini, openrouter, "Gemini must stay before OpenRouter")

    def test_the_local_fallback_is_still_present(self):
        body = _src(SU_PY)
        self.assertIn("_local_vision", body)
        self.assertIn("pytesseract.image_to_string", body)

    def test_the_gemini_trial_stage_is_intact(self):
        body = _src(SU_PY)
        self.assertIn("GEMINI_VISION_TRIAL_TIMEOUT", body)
        self.assertIn("VISION_GEMINI_CHAIN", body)

    def test_it_remains_a_fallback_not_a_reorder(self):
        """The CHAIN order is the contract, and it is asserted on the call
        sequence inside `_cloud_vision` above.

        Definition order in the file is deliberately NOT asserted: `_openrouter_vision`
        happens to be defined before `_modal_vision`, which says nothing about
        which one runs first, and asserting it would be a test that passes for
        the wrong reason."""
        tree = ast.parse(_src(SU_PY))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_cloud_vision")
        calls = [sub.func.id for sub in ast.walk(fn)
                 if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)]
        self.assertIn("_modal_vision", calls)
        self.assertLess(calls.index("_modal_vision"), calls.index("_gemini_vision"),
                        "Modal must be attempted before Gemini")


class KernelDisplayFallbackTests(unittest.TestCase):
    """kernel_display_patch.py is friday_locate's in-tree twin (JSON locate)."""

    def setUp(self):
        from source.server import kernel_display_patch as kdp
        self.kdp = kdp

    def test_the_dead_slug_is_not_its_default(self):
        for slug in RETIRED:
            self.assertNotEqual(self.kdp.OPENROUTER_VISION_MODEL, slug)

    def test_it_uses_the_locate_validated_model(self):
        self.assertEqual(self.kdp.OPENROUTER_VISION_MODEL, LOCATE)

    def test_it_gets_a_sufficient_token_budget(self):
        self.assertGreaterEqual(self.kdp.OPENROUTER_VISION_MAX_TOKENS, 2000)

    def test_its_env_override_still_works(self):
        import importlib
        os.environ["FRIDAY_OPENROUTER_VISION_MODEL"] = "my/override:free"
        try:
            reloaded = importlib.reload(self.kdp)
            self.assertEqual(reloaded.OPENROUTER_VISION_MODEL, "my/override:free")
        finally:
            os.environ.pop("FRIDAY_OPENROUTER_VISION_MODEL", None)
            importlib.reload(self.kdp)

    def test_the_gemini_default_is_untouched(self):
        self.assertEqual(self.kdp.GEMINI_VISION_MODEL, "gemini-3.1-flash-lite")

    def test_it_matches_the_shipped_friday_locate_default(self):
        """Both are the same consumer, so they must not drift apart again."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("fl_n", VENDORED)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(self.kdp.OPENROUTER_VISION_MODEL,
                         mod.OPENROUTER_VISION_MODEL)


class NoStaleSlugAnywhereTests(unittest.TestCase):
    """Nothing may keep a retired slug as a LIVE default."""

    SOURCES = (
        SU_PY, KDP_PY, SUI_PY, SERVER_PY, VENDORED,
    )

    def test_no_source_file_has_a_retired_slug_as_a_literal_default(self):
        offenders = []
        for path in self.SOURCES:
            if not os.path.exists(path):
                continue
            for number, line in enumerate(_src(path).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for slug in RETIRED:
                    if '"%s"' % slug in line or "'%s'" % slug in line:
                        offenders.append("%s:%d" % (os.path.basename(path), number))
        self.assertEqual(offenders, [],
                         "retired slug still used as a live value at: %s" % offenders)

    def test_the_hud_shows_the_configured_model_not_a_hardcoded_one(self):
        body = _src(SUI_PY)
        self.assertIn("_openrouter_vision_model_display()", body)
        for slug in RETIRED:
            self.assertNotIn('"%s"' % slug, body,
                             "the HUD still hardcodes the retired %s" % slug)
        from source.server import status_ui
        shown = status_ui._openrouter_vision_model_display()
        self.assertEqual(shown, DESCRIBE,
                         "the HUD would advertise a model the code does not use")

    def test_the_hud_helper_never_raises(self):
        from source.server import status_ui
        self.assertIsInstance(status_ui._openrouter_vision_model_display(), str)
        self.assertTrue(status_ui._openrouter_vision_model_display())

    def test_the_wheel_carries_no_retired_default(self):
        with zipfile.ZipFile(WHEEL) as zf:
            offenders = []
            for name in zf.namelist():
                if not name.endswith(".py"):
                    continue
                for number, line in enumerate(
                        zf.read(name).decode("utf-8", "replace").splitlines(), 1):
                    if line.strip().startswith("#"):
                        continue
                    for slug in RETIRED:
                        if '"%s"' % slug in line:
                            offenders.append("%s:%d" % (name, number))
        self.assertEqual(offenders, [],
                         "the shipped wheel still uses a retired default: %s"
                         % offenders)


# ===========================================================================
# C. ROUND M SCREEN EVIDENCE MUST REMAIN INTACT
# ===========================================================================


class ScreenEvidenceStillIntactTests(unittest.TestCase):
    """Round N touches screen_understanding.py; the Round M hand-off must not
    have been collateral damage."""

    def setUp(self):
        self.body = _src(SU_PY)
        self.server = _src(SERVER_PY)

    def test_the_architecture_chain_is_intact(self):
        """describe_screen -> evidence -> last_screen_shot -> _screen_context_prefix
        -> the brain authors the answer."""
        self.assertIn("def take_screen_evidence", self.body)
        self.assertIn("_SCREEN_EVIDENCE.update", self.body)
        self.assertIn("def _record_fresh_screen_evidence", self.server)
        self.assertIn('server_state["last_screen_shot"]', self.server)
        self.assertIn("def _screen_context_prefix", self.server)
        self.assertIn("_screen_context_prefix(server_state) + content", self.server)

    def test_shared_admission_path_still_records_and_injects(self):
        functions = {node.name: ast.unparse(node) for node in ast.walk(ast.parse(self.server))
                     if isinstance(node, ast.AsyncFunctionDef)}
        self.assertIn("dispatch_text(clean_content, source='voice')", functions["new_input"])
        self.assertIn("_run_text_turn(request, owner, source=source)", functions["dispatch_text"])
        self.assertIn("_prepare_text_turn(request, owner, source=source)", functions["_run_text_turn"])
        body = functions["_prepare_text_turn"]
        self.assertIn("_record_fresh_screen_evidence(server_state)", body)
        self.assertIn("_screen_context_prefix(server_state)", body)

    def test_the_composer_is_still_not_the_answer(self):
        flat = self.server.replace("'", '"')
        self.assertIn('append_outcome(response, tag="vision_read")', flat)
        self.assertNotIn('append_outcome(response, tag="answered")', flat)

    def test_the_do_not_recapture_instruction_survives(self):
        from source.server.server import _screen_context_prefix
        from source.server import screen_understanding as su
        su._SCREEN_EVIDENCE.update({
            "captured_at": __import__("time").time(),
            "report": "GROUNDED EVIDENCE",
            "regions": ["the RIGHT screen"], "region_count": 1, "ocr": False,
            "ocr_chars": 0, "vision_calls": 1, "vision_chars": 100,
            "vision_ok": True, "window": {"title": "Edge"}, "question": "q"})
        try:
            from source.server.server import _record_fresh_screen_evidence
            state = {"last_screen_shot": None}
            _record_fresh_screen_evidence(state)
            prefix = _screen_context_prefix(state)
        finally:
            su.take_screen_evidence()
        self.assertIn("GROUNDED EVIDENCE", prefix)
        self.assertIn("do not re-capture", prefix.lower())
        self.assertIn("computer.vision.query", prefix)

    def test_the_freshness_ttl_is_still_applied(self):
        from source.server.server import (_SCREEN_CONTEXT_TTL_SECONDS,
                                          _screen_context_prefix)
        import time
        state = {"last_screen_shot": {
            "at": time.time() - (_SCREEN_CONTEXT_TTL_SECONDS + 60),
            "description": "old", "regions": ["x"], "ocr": False,
            "ocr_chars": 0, "vision_calls": 1, "vision_chars": 1,
            "vision_ok": True, "window": {}}}
        prefix = _screen_context_prefix(state)
        self.assertIn("may be stale", prefix)
        self.assertNotIn("FRESH SCREEN EVIDENCE - already collected", prefix)

    def test_a_fresh_read_is_never_called_stale(self):
        from source.server.server import _screen_context_prefix
        import time
        state = {"last_screen_shot": {
            "at": time.time() - 4, "description": "new", "regions": ["x"],
            "ocr": False, "ocr_chars": 0, "vision_calls": 1,
            "vision_chars": 1, "vision_ok": True, "window": {}}}
        prefix = _screen_context_prefix(state)
        self.assertIn("FRESH SCREEN EVIDENCE", prefix)
        self.assertNotIn("may be stale", prefix)

    def test_the_journal_still_records_the_real_request(self):
        """The Round M sentinel strip must survive."""
        from source.server.server import _request_words_only
        real = "what do you think i'm doing right now?"
        polluted = ("[FRESH SCREEN EVIDENCE - already collected]\n"
                    "body with [brackets] inside\n"
                    "[/FRESH SCREEN EVIDENCE]\n\n" + real)
        self.assertEqual(_request_words_only(polluted), real)

    def test_measurement_instrumentation_survived(self):
        """Round L's screen call ledger must not have been lost."""
        self.assertIn("def take_call_ledger", self.body)
        self.assertIn("_note_call", self.body)
        self.assertIn("_log_screen_call_ledger", self.server)


if __name__ == "__main__":
    unittest.main()
