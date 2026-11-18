"""Round M, Phase 2 - hand the brain the screen evidence FRIDAY already paid for.

THE DEFECT
----------
`server_state["last_screen_shot"]` - the structure that was ALWAYS intended to
carry screen evidence to the brain - was written in exactly one place, the
Telegram photo path. On the voice and /chat screen paths it was never written,
so the freshness check in `dispatch_text` could never see a fresh read and
always took its "my last screen read is N minutes old ... capture a FRESH
screenshot" branch.

The effect, measured live on 2026-10-03 with one real screen question:

    screen pipeline : 1 vision call (4.2s) + 1 composer call (407 chars)
    brain LLM calls : 13
    tool calls      : 12   (2 of them computer.vision.query -> vision http 404)
    total           : 172s
    delivered       : "Finished that - 12 steps were completed, but I produced
                      no spoken answer this turn."
    journalled      : the discarded 407-char composer answer

FRIDAY was told her own screen read was stale seconds after making it, so she
re-derived it through tools.

THE FIX
-------
Reuse `last_screen_shot`. `screen_understanding.describe_screen()` publishes what
it actually read via `take_screen_evidence()`; server.py stores that in
`last_screen_shot` and `_screen_context_prefix()` renders it as grounded context.

WHAT THESE TESTS MUST NOT ALLOW
-------------------------------
* Delivering the composer's text as the answer - the brain still authors it.
* Re-capturing when fresh evidence exists.
* Trusting evidence past the TTL.
* Changing non-screen turns at all.
"""

import ast
import os
import sys
import time
import unittest

sys.path.insert(0, os.getcwd())

from source.server import screen_understanding as su  # noqa: E402
from source.server.server import (  # noqa: E402
    _SCREEN_CONTEXT_TTL_SECONDS,
    _SCREEN_EVIDENCE_CHARS,
    _record_fresh_screen_evidence,
    _screen_context_prefix,
)

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")


def _state(**over):
    state = {"last_screen_shot": None}
    state.update(over)
    return state


def _fake_evidence(report="Visual description of the RIGHT screen:\nEdge is open.",
                   regions=("the RIGHT screen (this is the PRIMARY display)",),
                   vision_chars=1185, ocr=False, ocr_chars=0):
    return {
        "captured_at": time.time(),
        "question": "what am I doing?",
        "regions": list(regions),
        "region_count": len(regions),
        "region_detail": [],
        "ocr": ocr,
        "ocr_chars": ocr_chars,
        "vision_calls": 1,
        "vision_chars": vision_chars,
        "vision_ok": vision_chars > 0,
        "window": {"title": "Microsoft Edge", "process": "msedge.exe"},
        "report": report,
    }


class _PatchedEvidence:
    """Swap take_screen_evidence for a canned record."""

    def __init__(self, evidence):
        self.evidence = evidence

    def __enter__(self):
        import source.server.screen_understanding as mod
        self._mod = mod
        self._real = mod.take_screen_evidence
        evidence = self.evidence

        def _fake():
            ev = evidence
            if callable(ev):
                ev = ev()
            return ev

        mod.take_screen_evidence = _fake
        return self

    def __exit__(self, *exc):
        self._mod.take_screen_evidence = self._real
        return False


# ---------------------------------------------------------------------------
# A screen request on either path stores usable FRESH evidence
# ---------------------------------------------------------------------------


def _fresh_state(**kw):
    """A state dict holding evidence recorded exactly as production would."""
    state = _state()
    with _PatchedEvidence(_fake_evidence(**kw)):
        _record_fresh_screen_evidence(state)
    return state


def _unparsed(path, func_name):
    """ast.unparse normalises string quotes to single, so compare quote-blind."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == func_name)
    return ast.unparse(node)


SU_PY = os.path.join(os.getcwd(), "source", "server", "screen_understanding.py")


class StoreEvidenceTests(unittest.TestCase):
    def test_evidence_is_stored_in_the_existing_last_screen_shot_structure(self):
        state = _state()
        with _PatchedEvidence(_fake_evidence()):
            shot = _record_fresh_screen_evidence(state)
        self.assertIsNotNone(shot)
        # Reused the EXISTING structure, not a new parallel one.
        self.assertIs(state["last_screen_shot"], shot)
        self.assertEqual(shot["source"], "screen_capture")
        self.assertEqual(shot["vision_chars"], 1185)
        self.assertTrue(shot["vision_ok"])
        self.assertEqual(shot["region_count"], 1)
        self.assertIn("RIGHT screen", shot["regions"][0])

    def test_no_evidence_stores_nothing(self):
        state = _state()
        with _PatchedEvidence({}):
            self.assertIsNone(_record_fresh_screen_evidence(state))
        self.assertIsNone(state["last_screen_shot"],
                         "an empty capture must not overwrite a good shot")

    def test_a_failed_capture_does_not_clobber_a_previous_good_shot(self):
        state = _state(last_screen_shot={"at": time.time(), "description": "old but good"})
        with _PatchedEvidence({"captured_at": time.time(), "report": ""}):
            _record_fresh_screen_evidence(state)
        self.assertEqual(state["last_screen_shot"]["description"], "old but good")

    def test_the_structured_facts_are_recorded(self):
        """The brain must be told what was actually read, not just 'a screen'."""
        state = _state()
        with _PatchedEvidence(_fake_evidence(ocr=True, ocr_chars=640, vision_chars=1185)):
            shot = _record_fresh_screen_evidence(state)
        self.assertTrue(shot["ocr"])
        self.assertEqual(shot["ocr_chars"], 640)
        self.assertEqual(shot["vision_calls"], 1)
        self.assertIn("Microsoft Edge", shot["window"]["title"])


class ScreenPathWiringTests(unittest.TestCase):
    """Both entry points must record AND inject. One without the other is the
    original bug in a new place."""

    def setUp(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            self.src = handle.read()
        self.tree = ast.parse(self.src)

    def _entry_points(self):
        out = {}
        for name in ("_prepare_text_turn",):
            for node in ast.walk(self.tree):
                if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                        and node.name == name:
                    out[name] = node
                    break
        return out

    def test_both_entry_points_record_and_inject(self):
        found = self._entry_points()
        for name in ("_prepare_text_turn",):
            self.assertIn(name, found, "%s not found" % name)
            body = ast.unparse(found[name])
            self.assertIn("_record_fresh_screen_evidence(server_state)", body,
                          "%s never stores the fresh evidence" % name)
            self.assertIn("_screen_context_prefix(server_state)", body,
                          "%s never injects the screen context" % name)

    def test_both_entry_points_do_so_inside_the_screen_request_branch(self):
        """Otherwise every non-screen turn would be rewritten."""
        for name, node in self._entry_points().items():
            body = ast.unparse(node)
            self.assertIn("is_screen_request(content)", body)
            # The two calls must appear AFTER the screen gate.
            gate = body.find("is_screen_request(content)")
            record = body.find("_record_fresh_screen_evidence(server_state)")
            inject = body.find("_screen_context_prefix(server_state)")
            self.assertGreater(record, gate, "%s records before the gate" % name)
            self.assertGreater(inject, gate, "%s injects before the gate" % name)

    def test_the_dead_inline_freshness_block_is_gone(self):
        self.assertNotIn("last_shot", self.src)
        self.assertNotIn("the screenshot I just captured and sent you", self.src)


# ---------------------------------------------------------------------------
# The brain receives it, and is told not to re-capture
# ---------------------------------------------------------------------------


class ContextPrefixTests(unittest.TestCase):
    def _fresh(self, **kw):
        return _fresh_state(**kw)

    def test_the_prefix_carries_the_evidence_and_the_when_and_where(self):
        prefix = _screen_context_prefix(self._fresh())
        self.assertIn("Visual description of the RIGHT screen", prefix)
        self.assertIn("RIGHT screen", prefix)
        stamp = time.strftime("%H:%M:%S", time.localtime())
        self.assertIn(stamp, prefix)
        self.assertIn("seconds ago", prefix)

    def test_the_prefix_states_the_brain_should_not_re_capture(self):
        """This is the instruction whose absence produced 12 redundant tool calls."""
        prefix = _screen_context_prefix(self._fresh())
        self.assertIn("do not re-capture", prefix.lower())
        self.assertIn("computer.vision.query", prefix)
        self.assertIn("Answer Sir from THIS evidence", prefix)

    def test_the_prefix_reports_what_was_actually_available(self):
        state = self._fresh(ocr=True, ocr_chars=640, vision_chars=1185)
        prefix = _screen_context_prefix(state)
        self.assertIn("vision model read 1185 characters", prefix)
        self.assertIn("OCR read 640 characters", prefix)
        self.assertIn("Microsoft Edge", prefix)

    def test_an_ocr_free_screen_does_not_claim_ocr(self):
        prefix = _screen_context_prefix(self._fresh(ocr=False, ocr_chars=0))
        self.assertNotIn("OCR read", prefix)

    def test_the_prefix_is_bounded(self):
        state = self._fresh(report="X" * 50000)
        prefix = _screen_context_prefix(state)
        self.assertLessEqual(len(prefix), _SCREEN_EVIDENCE_CHARS + 2000,
                             "an oversized report must not bloat every screen prompt")

    def test_no_shot_at_all_yields_the_stale_branch(self):
        prefix = _screen_context_prefix(_state())
        self.assertIn("capture a FRESH screenshot", prefix)
        self.assertNotIn("FRESH SCREEN EVIDENCE", prefix)


# ---------------------------------------------------------------------------
# TTL: fresh evidence must not be trusted forever
# ---------------------------------------------------------------------------


class FreshnessTests(unittest.TestCase):
    def _fresh(self, **kw):
        return _fresh_state(**kw)

    def test_stale_evidence_is_not_reused(self):
        state = self._fresh()
        # Backdate past the TTL.
        state["last_screen_shot"]["at"] = time.time() - (_SCREEN_CONTEXT_TTL_SECONDS + 60)
        prefix = _screen_context_prefix(state)
        self.assertIn("may be stale", prefix)
        self.assertIn("capture a FRESH screenshot", prefix)
        self.assertNotIn("FRESH SCREEN EVIDENCE - already collected",
                         prefix)

    def test_evidence_just_inside_the_ttl_is_still_fresh(self):
        state = self._fresh()
        state["last_screen_shot"]["at"] = time.time() - (_SCREEN_CONTEXT_TTL_SECONDS - 5)
        self.assertIn("FRESH SCREEN EVIDENCE",
                      _screen_context_prefix(state))

    def test_a_seconds_old_read_is_never_called_stale(self):
        """The regression: a read taken 4 seconds ago must not be 'minutes old'."""
        state = self._fresh()
        state["last_screen_shot"]["at"] = time.time() - 4
        prefix = _screen_context_prefix(state)
        self.assertIn("FRESH SCREEN EVIDENCE", prefix)
        self.assertNotIn("may be stale", prefix)
        self.assertIn("4 seconds ago", prefix)


# ---------------------------------------------------------------------------
# The composer must NOT become the answer; the brain still authors it
# ---------------------------------------------------------------------------


class ComposerIsNotTheAnswerTests(unittest.TestCase):
    def _fresh(self, **kw):
        return _fresh_state(**kw)

    def test_the_composer_is_journalled_as_a_vision_read_not_as_the_answer(self):
        """Round M: now that the brain authors the reply, `response` is text Sir
        never receives. Journalling it as `ANSWERED` made outcomes.md disagree
        with what he was told - observed live as two different `ANSWERED` rows for
        one turn. It is still recorded, under a truthful label."""
        with open(SERVER_PY, encoding="utf-8") as handle:
            src = handle.read()
        # Quote-insensitive: the source may use either quote style.
        flat = src.replace("'", '"')
        self.assertIn('append_outcome(response, tag="vision_read")', flat)
        self.assertNotIn('append_outcome(response, tag="answered")', flat)
        self.assertNotIn('append_outcome(screen_result, tag="answered")', flat)

    def test_the_composer_result_is_journalled_not_injected(self):
        """`screen_response()`'s return value must never reach the brain prompt.

        The brain authors the answer (software/AGENTS.md Screen Policy). What is
        injected is `describe_screen()`'s EVIDENCE, which is a different object
        reached through `take_screen_evidence()`.
        """
        state = _state()
        composer_text = "You're knee-deep in the FRIDAY bug investigation, Sir."
        evidence = _fake_evidence(report="GROUNDED EVIDENCE, not the composed line.")
        with _PatchedEvidence(evidence):
            _record_fresh_screen_evidence(state)
        prefix = _screen_context_prefix(state)
        self.assertIn("GROUNDED EVIDENCE", prefix)
        self.assertNotIn(composer_text, prefix)

    def test_screen_response_still_returns_the_composed_answer(self):
        """Unchanged: the function's own contract is untouched by this round."""
        fn = next(n for n in su.__dict__ if n == "screen_response")
        with open(os.path.join(os.getcwd(), "source", "server",
                               "screen_understanding.py"), encoding="utf-8") as h:
            src = h.read()
        self.assertIn("def screen_response", src)
        tree = ast.parse(src)
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        self.assertIn("return answer", ast.unparse(node))

    def test_the_brain_still_receives_the_real_user_request(self):
        """Grounding must ADD context, not replace the request."""
        state = self._fresh()
        prefix = _screen_context_prefix(state)
        self.assertIn("]", prefix)
        # The caller concatenates; the request must survive.
        combined = prefix + "what do you think i'm doing right now?"
        self.assertIn("what do you think i'm doing right now?", combined)


# ---------------------------------------------------------------------------
# Non-screen turns must be untouched
# ---------------------------------------------------------------------------


class NonScreenUnchangedTests(unittest.TestCase):
    def test_the_helpers_are_only_called_from_screen_branches(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            src = handle.read()
        for name in ("_record_fresh_screen_evidence", "_screen_context_prefix"):
            # Definition and one shared voice/text call site.
            self.assertEqual(src.count(name + "(server_state)"), 2,
                             "%s must have one shared call site" % name)

    def test_no_prompt_text_changed_outside_the_screen_branch(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                    and node.name in ("_prepare_text_turn",):
                body = ast.unparse(node)
                gate = body.find("is_screen_request(content)")
                self.assertGreater(gate, 0)
                # Both helpers must live AFTER the gate textually.
                self.assertGreater(body.find("_screen_context_prefix(server_state)"), gate)


# ---------------------------------------------------------------------------
# describe_screen really publishes the evidence
# ---------------------------------------------------------------------------


class EvidencePublicationTests(unittest.TestCase):
    def test_describe_screen_populates_the_evidence_record(self):
        body = _unparsed(SU_PY, "describe_screen")
        for key in ("captured_at", "regions", "ocr_chars", "vision_chars",
                    "vision_ok", "report"):
            self.assertIn(key, body,
                          "describe_screen does not publish %s" % key)

    def test_take_screen_evidence_drains(self):
        """Two screen turns must not bleed into each other."""
        su._SCREEN_EVIDENCE.clear()
        su._SCREEN_EVIDENCE.update({"report": "first"})
        got = su.take_screen_evidence()
        self.assertEqual(got.get("report"), "first")
        self.assertEqual(su.take_screen_evidence(), {},
                         "evidence leaked into the next turn")

    def test_a_failed_capture_clears_stale_evidence(self):
        """`_select_regions` returning nothing must NOT leave the previous
        turn's evidence looking fresh."""
        su._SCREEN_EVIDENCE.clear()
        su._SCREEN_EVIDENCE.update({"report": "previous turn", "captured_at": time.time()})
        real = su._select_regions
        su._select_regions = lambda q: []
        try:
            out = su.describe_screen("what am I doing?")
        finally:
            su._select_regions = real
        self.assertIn("Screen capture failed", out)
        self.assertEqual(su.take_screen_evidence(), {},
                         "a failed capture left stale evidence published")

    def test_text_is_bounded_before_the_evidence_tally_reads_it(self):
        """`text` only exists inside the `want_ocr` branch; the tally must not
        NameError or read the previous region's OCR."""
        body = _unparsed(SU_PY, "describe_screen")
        loop = next(n for n in ast.walk(ast.parse(
            open(SU_PY, encoding="utf-8").read()))
            if isinstance(n, ast.For) and n.lineno > 0 and
            "evidence_regions.append" in ast.unparse(n))
        loop_src = ast.unparse(loop)
        self.assertIn("text = ''", loop_src,
                      "`text` is not initialised before the want_ocr branch")
        self.assertLess(loop_src.find("text = ''"),
                        loop_src.find("if want_ocr"),
                        "`text` must be initialised BEFORE the want_ocr branch")


class JournalFidelityTests(unittest.TestCase):
    """The durable "what did Sir ask" journal must record HIS words.

    Regression found live in this very round: after the evidence prefix was added,
    `outcomes.md` recorded
        ASKED: I captured and read the RIGHT screen ... (1 seconds ago)
    instead of the question, because `_request_words_only` strips only to the
    FIRST `]` and the evidence text contains its own brackets.
    """

    def _prefix(self, **kw):
        return _screen_context_prefix(_fresh_state(**kw))

    def test_the_real_request_survives_the_prefix(self):
        from source.server.server import _request_words_only

        real = "what do you think i'm doing right now?"
        got = _request_words_only(self._prefix() + real)
        self.assertEqual(got, real,
                         "the journal recorded FRIDAY's context, not the question")

    def test_it_survives_memory_context_nesting(self):
        """The /chat path wraps memory context around the prefixed content, so
        the screen block ends up nested one level deeper."""
        from source.server.server import _request_words_only

        real = "what is on my screen right now?"
        wrapped = "[memory]\n\nUser request:\n" + self._prefix() + real
        self.assertEqual(_request_words_only(wrapped), real)

    def test_the_prefix_is_delimited_by_an_explicit_sentinel(self):
        """Bracket counting cannot work here; the sentinel is what makes the
        strip exact."""
        prefix = self._prefix()
        self.assertTrue(prefix.startswith("[FRESH SCREEN EVIDENCE"))
        self.assertIn("[/FRESH SCREEN EVIDENCE]", prefix)
        close = prefix.index("[/FRESH SCREEN EVIDENCE]")
        # The sentinel must come AFTER the evidence body, so a `]` inside the
        # evidence cannot be mistaken for the end.
        self.assertGreater(close, prefix.index("Here is what was actually on screen:"))

    def test_evidence_containing_brackets_does_not_confuse_the_strip(self):
        from source.server.server import _request_words_only

        report = "Text visible on X (verbatim OCR):\n[Context: nested] ] [more]"
        real = "read my screen"
        got = _request_words_only(self._prefix(report=report) + real)
        self.assertEqual(got, real)

    def test_a_normal_turn_is_unaffected(self):
        from source.server.server import _request_words_only

        self.assertEqual(_request_words_only("hello there"), "hello there")
        self.assertEqual(_request_words_only("close notepad"), "close notepad")

    def test_an_unterminated_block_does_not_eat_the_request(self):
        """Defensive: a truncated prefix must not swallow the question."""
        from source.server.server import _request_words_only

        real = "what is on my screen"
        got = _request_words_only("[FRESH SCREEN EVIDENCE truncated..." + real)
        self.assertIn(real, got)


if __name__ == "__main__":
    unittest.main()