"""ROUND Z BUILD 1: scroll / navigation / verification tests.

WHAT IS REAL HERE
-----------------
* `ui_automation._scroll_state`, `normalize_scroll`, `find_scrollable`,
  `perform(action="scroll")` and `ui_scroll.viewport_digest` / `rect_of` /
  `action_history` are used as-is from the production modules, never
  reimplemented.
* The live tests drive REAL Windows UIA objects. pywinauto is a real dependency
  of the project, so these are genuine integration tests, not mocks. They skip
  (rather than silently pass) when no suitable window is open, so a skip can
  never be mistaken for a proof.
* The fakes are confined to input delivery (`pyautogui`), because the point of
  those tests is the DECISION logic - refuse / choose mechanism / verify - not
  the Win32 call. The live tests above prove the input itself works.

THE MEASUREMENT TRAPS THESE TESTS PIN
-------------------------------------
1. `viewport_digest` MUST capture with all_screens=True. Without it Pillow
   measures only the primary monitor, and on this two-monitor desktop every
   negative-coordinate window digests as a constant, so every scroll looks like
   a no-op. That produced a completely wrong PHASE 0 gate result.
2. pywinauto pattern properties RAISE `NoPatternInterfaceError` rather than
   returning None, so `getattr(el, "iface_scroll", None)` reports every control
   as scrollable. `_scroll_state` must classify "exposes" separately from
   "can actually scroll".
"""

import ast
import inspect
import os
import re
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from source.server import ui_automation as ua  # noqa: E402
from source.server import ui_probe  # noqa: E402
from source.server import ui_scroll  # noqa: E402
from source.server import windows_control as wc  # noqa: E402

# Captured before any test patches it, so the harness can build real
# UIAutomation instances even while a test patches the class on the module.
_REAL_UIAutomation = ua.UIAutomation

# Anchored to __file__ at import time on purpose. os.getcwd() is NOT safe here:
# another test in the full suite chdirs into a temp directory without restoring,
# which made this test pass alone and fail in full-suite order. The same trap bit
# Round W's suite and Round N's vision tests.
_PROFILES_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "source", "server", "profiles", "default.py")


# --------------------------------------------------------------------------
# Fakes for the SCROLL PATTERN contract, mirroring what pywinauto really does.
# --------------------------------------------------------------------------
# IMPORTANT: these mirror the interface as MEASURED on the real OpenCode window,
# not as imagined. The first version of this file used `VerticallyScrollable`
# / `VerticalScrollPercent` and passed every test while the production code
# could never have worked. Three real details are reproduced here:
#   * every property is `Current`-prefixed;
#   * UIA booleans arrive as COM ints (0/1), so `value is True` is False;
#   * "no valid percentage" is -1, a real number that must not be compared.
# There is deliberately NO unprefixed property, so any regression to those
# names fails loudly instead of silently reporting "nothing is scrollable".
class FakePattern:
    """Behaves like pywinauto's real `iface_scroll` COM wrapper."""

    def __init__(self, vertically=1, horizontally=0, percent=0.0,
                 percent_h=-1.0):
        self.CurrentVerticallyScrollable = vertically
        self.CurrentHorizontallyScrollable = horizontally
        self.CurrentVerticalScrollPercent = percent
        self.CurrentHorizontalScrollPercent = percent_h

    def Scroll(self, horizontal_amount, vertical_amount):
        self.CurrentVerticalScrollPercent = max(
            0.0, min(100.0, self.CurrentVerticalScrollPercent + vertical_amount))


class RaisesPattern:
    """An element that exposes ScrollPattern but cannot scroll. pywinauto
    raises for every property - measured on the real OpenCode 'Document'
    element, whose Current*ScrollPercent reads -1 and Current*Scrollable 0."""

    def __getattr__(self, name):
        raise AttributeError("not vertically scrollable")


class FakeControl:
    def __init__(self, name="", ctype="Document", pattern=None, handle=10,
                 runtime_id="rt-1", value=""):
        self._name = name
        self._ctype = ctype
        self._pattern = pattern
        self.element_info = types.SimpleNamespace(
            name=name, control_type=ctype, automation_id="",
            runtime_id=runtime_id)
        # pywinauto exposes `handle` as a plain int ATTRIBUTE on UIA wrappers,
        # and ui_automation reads it with bare getattr() when recording the
        # owning window. A method here would make every token look stale.
        self.handle = handle
        self._value = value
        self.scrolled = []

    # pywinauto raises NoPatternInterfaceError (an Exception, NOT
    # AttributeError) when the pattern is absent - which is why the naive
    # getattr(..., None) probe is wrong.
    @property
    def iface_scroll(self):
        from pywinauto.uia_defines import NoPatternInterfaceError

        if self._pattern is None:
            raise NoPatternInterfaceError()
        return self._pattern

    def scroll(self, direction, amount, count=1, retry_interval=0.1):
        """Mirrors pywinauto's UIAWrapper.scroll(direction, amount, count,
        retry_interval). `amount` is "line" or "page" - NOT a notch count."""
        if amount not in ("line", "page"):
            raise ValueError(
                "Wrong arguments: amount can be only \"line\" or \"page\"")
        self.scrolled.append((direction, amount, count))
        if self._pattern is not None:
            step = {"up": 5, "down": -5, "left": -5, "right": 5}[direction]
            if amount == "page":
                step *= 10
            for _ in range(count):
                self._pattern.Scroll(0, step)

    def handle(self):
        return self.handle

    def get_value(self):
        return self._value

    def window_text(self):
        return self._name

    def is_enabled(self):
        return True

    def is_visible(self):
        return True

    def rectangle(self):
        return types.SimpleNamespace(left=0, top=0, right=800, bottom=600)

    def descendants(self):
        return []


def _automation(controls, window_handle=99):
    """A UIAutomation bound to a fake window holding the given controls.

    `_resolve_window` is replaced because its real implementation matches on
    window properties that a control-shaped fake does not carry; the production
    matching logic is not what these tests are about.
    """
    window = FakeControl(name="Win", ctype="Window", handle=window_handle,
                        runtime_id="win")
    a = _REAL_UIAutomation()
    a._windows = lambda: [window]
    a._controls = lambda w: controls
    a._resolve_window = lambda target: (window, None, [])
    return a


class ScrollIntrospectionTests(unittest.TestCase):
    def test_a_missing_pattern_is_reported_as_none_not_scrollable(self):
        state = ua._scroll_state(FakeControl(pattern=None))
        self.assertFalse(state["exposes_scroll"])
        self.assertFalse(state["can_scroll_v"])
        self.assertEqual(state["scroll_state"], "none")

    def test_a_working_container_is_reported_scrollable(self):
        pattern = FakePattern(vertically=True, percent=0.0)
        state = ua._scroll_state(FakeControl(pattern=pattern))
        self.assertTrue(state["exposes_scroll"])
        self.assertTrue(state["can_scroll_v"])
        self.assertFalse(state["can_scroll_h"])
        self.assertEqual(state["scroll_state"], "scrollable")
        self.assertEqual(state["scroll_percent_v"], 0.0)

class ScrollPropertyNameContractTests(unittest.TestCase):
    """Bind our property names to pywinauto's REAL contract, not to a fake.

    Round Z shipped `_scroll_state` reading `VerticallyScrollable` and
    `VerticalScrollPercent`. Neither exists. Every test passed because the
    hand-written fake invented the same wrong names, so the semantic scroll
    path could never have worked on this machine and the container was
    wrongly reported inert. pywinauto's own `UIAWrapper.scroll` is the
    authority: this reads the property names out of its real source and
    requires ours to match, so a rename upstream breaks a test instead of
    silently reporting "nothing here can scroll".
    """

    @staticmethod
    def _names_read_by_pywinauto():
        import inspect
        from pywinauto.controls.uiawrapper import UIAWrapper

        source = inspect.getsource(UIAWrapper.scroll)
        return set(re.findall(r"scroll_if\.(\w+)", source))

    @staticmethod
    def _live_iface():
        """A real ScrollPattern COM object from this machine, or None.

        Top-level windows rarely carry the pattern themselves; it lives on a
        descendant (measured: OpenCode's 'scrollable content' Group). Looking
        only at top-level windows silently skipped the one assertion that
        checks our names against a real COM object.
        """
        try:
            from pywinauto import Desktop
            for window in Desktop(backend="uia").windows():
                try:
                    candidates = list(window.descendants())[:60]
                except Exception:
                    candidates = []
                for control in [window] + candidates:
                    try:
                        return control.iface_scroll
                    except Exception:
                        continue
        except Exception:
            return None
        return None

    def test_pywinauto_really_exposes_the_names_we_read(self):
        ours = set(re.findall(r'_read\("(\w+)"\)',
                              inspect.getsource(ua._scroll_state)))
        theirs = self._names_read_by_pywinauto()
        self.assertTrue(ours, "no property names found in _scroll_state")
        # The scrollability flags must be exactly the ones pywinauto itself
        # reads on iface_scroll - those are the Round Z bug.
        self.assertLessEqual(
            {"CurrentVerticallyScrollable", "CurrentHorizontallyScrollable"},
            theirs)
        # The percentages are real but pywinauto's scroll() has no use for
        # them, so they are checked against a LIVE COM object instead.
        percentages = ours - theirs
        self.assertEqual(percentages,
                         {"CurrentVerticalScrollPercent",
                          "CurrentHorizontalScrollPercent"})
        iface = self._live_iface()
        if iface is None:
            self.skipTest("no window on this machine exposes ScrollPattern")
        live = set(dir(iface))
        for name in ours:
            self.assertIn(name, live,
                          "%s is not a real iface_scroll property" % name)

    def test_the_unprefixed_names_this_bug_used_do_not_exist(self):
        """Guard the exact mistake: `VerticallyScrollable` is not a real name."""
        from pywinauto.controls.uiawrapper import UIAWrapper

        source = inspect.getsource(UIAWrapper.scroll)
        for wrong in ("scroll_if.VerticallyScrollable",
                      "scroll_if.VerticalScrollPercent",
                      "scroll_if.HorizontalScrollPercent"):
            self.assertNotIn(wrong, source)

    def test_a_com_int_boolean_is_treated_as_true(self):
        """UIA flags arrive as VARIANT_BOOL 0/1, so `value is True` is False."""
        state = ua._scroll_state(
            FakeControl(name="v", pattern=FakePattern(vertically=1)))
        self.assertTrue(state["exposes_scroll"])
        self.assertTrue(state["can_scroll_v"])
        self.assertEqual(state["scroll_state"], "scrollable")

    def test_a_minus_one_percentage_is_treated_as_no_percentage(self):
        """UIA reports -1 for 'no valid percentage'; it is a real number."""
        state = ua._scroll_state(
            FakeControl(name="v", pattern=FakePattern(vertically=0,
                                                       percent=-1.0)))
        self.assertIsNone(state["scroll_percent_v"])
        self.assertFalse(state["can_scroll_v"])

    def test_the_real_scroll_call_uses_line_amount_and_a_count(self):
        """pywinauto's amount is "line"/"page", never a notch count."""
        control = FakeControl(name="viewport", pattern=FakePattern(percent=50.0))
        automation = _automation([control])
        token = automation.dom(1)["controls"][0]["target_token"]
        result = automation.perform("scroll", token,
                                    value={"direction": "down", "amount": 4})
        self.assertTrue(result["success"], result.get("failure_reason"))
        self.assertEqual(control.scrolled, [("down", "line", 4)])

    def test_a_notch_count_would_have_raised_and_is_now_mapped(self):
        """Direct proof of the old bug: an int amount is a ValueError."""
        control = FakeControl(name="viewport", pattern=FakePattern(percent=50.0))
        with self.assertRaises(ValueError):
            control.scroll("down", 5)  # what the old code did
        control.scroll("down", "line", count=5)  # what it does now
        self.assertEqual(control.scrolled, [("down", "line", 5)])

class ConsentRefusalIsTerminalTests(unittest.TestCase):
    """A denied consent must come back as a denial, in EVERY mode.

    The bug: when a scrollable container was found but the consent-gated
    `ui_probe.act` refused, the refusal was only terminal for mechanism="uia".
    In "auto" it fell through to the verify block, which set allowed=True and
    success=True whenever the viewport digest happened to differ. A turn that
    had been DENIED consent came back claiming a verified scroll - the exact
    lie this module exists to prevent. `mechanism` also stayed
    "uia_scroll_pattern" although nothing semantic had moved anything.
    """

    def _run(self, mechanism):
        window = _Window()
        resolve, snapshot = _patch_resolution(window)
        control = FakeControl(name="viewport", pattern=FakePattern(percent=50.0))
        automation = _automation([control])
        # ui_scroll imports ui_probe INSIDE the function, so the patch target is
        # the module object itself, not an attribute of ui_scroll.
        with resolve, snapshot, \
             mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
             mock.patch.object(ui_scroll._ua, "UIAutomation",
                               lambda **k: automation), \
             mock.patch.object(ui_probe, "act", side_effect=lambda *a, **k: {
                 "success": False, "changed": False, "verified": False,
                 "allowed": False, "consent": None,
                 "failure_reason": "refused: no recorded user turn authorises "
                                   "'scroll' on 'viewport'"}), \
             mock.patch("pyautogui.moveTo") as move, \
             mock.patch("pyautogui.scroll") as wheel, \
             mock.patch.object(ui_scroll, "viewport_digest",
                               side_effect=["before-digest", "AFTER-digest"]):
            result = ui_scroll.scroll_window({"handle": 42}, direction="up",
                                             amount=5, mechanism=mechanism)
        return result, move, wheel

    def test_auto_does_not_turn_a_refusal_into_a_success(self):
        result, move, wheel = self._run("auto")
        # The digest DID change, which is exactly what used to fake success.
        self.assertIn("refused", result["failure_reason"])
        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["allowed"])
        self.assertIsNone(result["mechanism"],
                          "mechanism must not claim a scroll that never ran")

    def test_a_refusal_never_falls_back_to_the_wheel(self):
        _, move, wheel = self._run("auto")
        self.assertEqual(wheel.call_count, 0,
                         "consent denial is not a reason to try the wheel")
        self.assertEqual(move.call_count, 0)

    def test_uia_refusal_is_unchanged(self):
        result, _, _ = self._run("uia")
        self.assertFalse(result["success"])
        self.assertFalse(result["allowed"])
        self.assertIn("refused", result["failure_reason"])

    def test_a_performed_action_reports_allowed_true(self):
        """The other direction: a real wheel scroll must not report allowed=False.

        The refusal early-return protects `allowed`, so the verify block sets it
        True for anything that actually ran. Removing that line left a
        successful wheel scroll reporting success=True next to allowed=False,
        which is just as dishonest as the reverse.
        """
        window = _Window()
        resolve, snapshot = _patch_resolution(window)
        with resolve, snapshot, \
             mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
             mock.patch.object(ui_scroll._ua, "UIAutomation",
                               lambda **k: _automation([FakeControl(
                                   pattern=RaisesPattern())])), \
             mock.patch("pyautogui.moveTo"), \
             mock.patch("pyautogui.scroll"), \
             mock.patch.object(ui_scroll, "viewport_digest",
                               side_effect=["a", "b"]):
            result = ui_scroll.scroll_window({"handle": 42}, direction="up",
                                             amount=3, mechanism="wheel")
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["failure_reason"])

    def test_the_opencode_failure_shape_is_classified_honestly(self):
        """The real measured case: exposes the pattern, raises on every property.

        This must NOT be reported as scrollable, or a semantic-scroll path would
        claim to work on Chromium apps where it demonstrably does not.
        """
        state = ua._scroll_state(FakeControl(pattern=RaisesPattern()))
        self.assertTrue(state["exposes_scroll"])
        self.assertFalse(state["can_scroll_v"])
        self.assertFalse(state["can_scroll_h"])
        self.assertIsNone(state["scroll_percent_v"])
        self.assertEqual(state["scroll_state"], "exposed_not_scrollable")

    def test_the_naive_getattr_probe_would_have_been_wrong(self):
        """Documents trap 2 so it cannot be reintroduced.

        A broad `except` that assigns a placeholder string makes every control
        look scroll-capable; that bug produced a false "0 of 44" reading in the
        first PHASE 0 probe.
        """
        control = FakeControl(pattern=None)
        naive_hits = 0
        for element in (control, FakeControl(pattern=FakePattern())):
            try:
                marker = getattr(element, "iface_scroll", None)
            except Exception:
                marker = "<exc>"
            if marker is not None:          # what a broad except leaves behind
                naive_hits += 1
        self.assertEqual(naive_hits, 2, "sanity: the naive idiom really is broken")
        # The real helper tells the truth:
        self.assertFalse(ua._scroll_state(control)["exposes_scroll"])

    def test_snapshot_carries_scroll_facts_for_discovery(self):
        snap = ua._control_snapshot(FakeControl(pattern=FakePattern()), 3)
        for key in ("can_scroll", "can_scroll_v", "can_scroll_h",
                    "scroll_percent_v", "scroll_state", "rect"):
            self.assertIn(key, snap)
        self.assertTrue(snap["can_scroll"])
        self.assertEqual(snap["rect"], [0, 0, 800, 600])

    def test_find_scrollable_separates_usable_from_reported_only(self):
        usable = FakeControl(name="viewport", pattern=FakePattern(percent=10.0))
        inert = FakeControl(name="chrome", pattern=RaisesPattern())
        plain = FakeControl(name="label", pattern=None)
        found = _automation([usable, inert, plain]).find_scrollable(1)
        self.assertEqual([c["name"] for c in found["candidates"]], ["viewport"])
        self.assertEqual([c["name"] for c in found["reported_only"]], ["chrome"])


class ScrollRequestCoercionTests(unittest.TestCase):
    def test_direction_and_amount_are_bounded(self):
        self.assertEqual(ua.normalize_scroll({"direction": "up", "amount": 3}),
                         ("up", 3))
        self.assertEqual(ua.normalize_scroll({"direction": "down", "amount": 0}),
                         ("down", 1))
        self.assertEqual(ua.normalize_scroll({"direction": "down", "amount": 9999}),
                         ("down", 25))
        self.assertEqual(ua.normalize_scroll({"direction": "sideways"}),
                         ("up", 3))
        self.assertEqual(ua.normalize_scroll({"direction": "top"}), ("up", 3))
        self.assertEqual(ua.normalize_scroll({"direction": "next"}), ("down", 3))
        self.assertEqual(ua.normalize_scroll("up"), ("up", 3))
        self.assertEqual(ua.normalize_scroll(None), ("up", 3))


class SemanticScrollActionTests(unittest.TestCase):
    def test_semantic_scroll_moves_the_container_and_verifies(self):
        # Start mid-range: a real ScrollPattern percent is clamped to 0..100, so
        # scrolling DOWN from 0 cannot move it and would prove nothing.
        control = FakeControl(name="viewport", pattern=FakePattern(percent=50.0))
        automation = _automation([control])
        token = automation.dom(1)["controls"][0]["target_token"]
        result = automation.perform("scroll", token, value={"direction": "down",
                                                            "amount": 5})
        self.assertTrue(result["success"], result.get("failure_reason"))
        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["mechanism"], "uia_scroll_pattern")
        self.assertEqual(result["evidence"], "scroll_percent")
        self.assertTrue(result["scroll"]["percent_moved"])
        # pywinauto's real contract: amount is "line"/"page" and the notch
        # count travels as `count`.
        self.assertEqual(control.scrolled, [("down", "line", 5)])

    def test_a_container_that_cannot_scroll_is_refused_not_attempted(self):
        """The real OpenCode shape: refuse, never fake it, never fall back."""
        control = FakeControl(name="chrome", pattern=RaisesPattern())
        automation = _automation([control])
        token = automation.dom(1)["controls"][0]["target_token"]
        result = automation.perform("scroll", token, value={"direction": "up"})
        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])
        self.assertIn("no working ScrollPattern", result["failure_reason"])
        self.assertEqual(control.scrolled, [], "nothing may be attempted")

    def test_a_frozen_percentage_beats_a_coincidental_digest_change(self):
        """The provider's own scroll position is the stronger evidence.

        A window digest can change for reasons that have nothing to do with the
        scroll - a clock ticking, a caret blinking, a background repaint. If the
        container reports a scroll percentage and that percentage did NOT move,
        then the viewport did not move, and a digest change must not be allowed
        to claim the scroll worked.
        """
        class FrozenPattern(FakePattern):
            def Scroll(self, horizontal_amount, vertical_amount):
                pass  # the event is sent, the position does not move

        control = FakeControl(name="viewport", pattern=FrozenPattern(percent=50.0))
        automation = _automation([control])
        # A window whose digest DOES change across the call, to prove the digest
        # is genuinely different and the refusal is not accidental.
        changing = {"n": 0}

        def digest(window, limit=200):
            changing["n"] += 1
            return "digest-%d" % changing["n"]

        import source.server.ui_automation as autom
        original = autom._window_digest
        autom._window_digest = digest
        try:
            token = automation.dom(1)["controls"][0]["target_token"]
            result = automation.perform("scroll", token,
                                        value={"direction": "down", "amount": 5})
        finally:
            autom._window_digest = original
        self.assertGreaterEqual(changing["n"], 2, "the digest must have changed")
        self.assertFalse(result["verified"],
                         "a frozen scroll percent must not be overridden by a "
                         "coincident window digest change")
        self.assertFalse(result["success"])
        self.assertFalse(result["scroll"]["percent_moved"])

    def test_horizontal_scroll_is_refused_on_a_vertical_only_container(self):
        control = FakeControl(pattern=FakePattern(vertically=True,
                                                  horizontally=False))
        automation = _automation([control])
        token = automation.dom(1)["controls"][0]["target_token"]
        result = automation.perform("scroll", token, value={"direction": "right"})
        self.assertFalse(result["success"])
        self.assertIn("horizontally scrollable", result["failure_reason"])

    def test_scroll_is_token_bound_and_single_use(self):
        control = FakeControl(pattern=FakePattern(percent=50.0))
        automation = _automation([control])
        token = automation.dom(1)["controls"][0]["target_token"]
        first = automation.perform("scroll", token, value={"direction": "down"})
        self.assertTrue(first["success"])
        # A consumed token must not be reusable - the existing single-use rule.
        second = automation.perform("scroll", token, value={"direction": "down"})
        self.assertFalse(second["success"])
        self.assertIn("stale", second["failure_reason"])

    def test_scroll_is_classified_reversible_not_confirmation_required(self):
        risk = ua.risk_metadata("scroll")
        self.assertFalse(risk["confirmation_required"])
        self.assertTrue(risk["reversible"])


class ViewportDigestTests(unittest.TestCase):
    def test_digest_uses_all_screens(self):
        """Trap 1. Without all_screens=True the wrong monitor is measured."""
        seen = {}

        class FakeGrab:
            def __init__(self, bbox=None, all_screens=False):
                seen["bbox"] = bbox
                seen["all_screens"] = all_screens
                self.size = (10, 10)

            def convert(self, mode):
                return self

            def resize(self, size):
                return self

            def tobytes(self):
                return b"abc"

        import PIL.ImageGrab as real_grab
        with mock.patch.object(real_grab, "grab", FakeGrab):
            digest = ui_scroll.viewport_digest((-1926, -6, -377, 871))
        self.assertIsNotNone(digest)
        self.assertTrue(seen["all_screens"],
                        "all_screens=True is load-bearing for negative coords")
        self.assertEqual(seen["bbox"], (-1926, -6, -377, 871))

    def test_an_empty_rect_yields_no_digest_rather_than_a_false_one(self):
        self.assertIsNone(ui_scroll.viewport_digest((10, 10, 10, 10)))


class ActionRecordTests(unittest.TestCase):
    def setUp(self):
        ui_scroll.reset_action_history()

    def tearDown(self):
        ui_scroll.reset_action_history()

    def test_the_record_is_bounded(self):
        for index in range(ui_scroll.MAX_ACTION_RECORDS + 8):
            ui_scroll._record({"action": "scroll up %d" % index,
                               "mechanism": "wheel", "verified": False})
        history = ui_scroll.action_history()
        self.assertEqual(len(history), ui_scroll.MAX_ACTION_RECORDS)
        self.assertEqual(history[-1]["action"],
                         "scroll up %d" % (ui_scroll.MAX_ACTION_RECORDS + 7))

    def test_reset_clears_the_record(self):
        ui_scroll._record({"action": "scroll up", "mechanism": "wheel"})
        self.assertEqual(len(ui_scroll.action_history()), 1)
        ui_scroll.reset_action_history()
        self.assertEqual(ui_scroll.action_history(), [])

    def test_a_record_carries_the_evidence_a_report_needs(self):
        ui_scroll._record({
            "window": "OpenCode", "control": "scrollable content",
            "mechanism": "wheel", "action": "scroll up 5",
            "pre_state": {"viewport_digest": "a"}, "post_state": {"viewport_digest": "b"},
            "verified": True})
        entry = ui_scroll.action_history()[0]
        for key in ("window", "mechanism", "action", "pre_state", "post_state",
                    "verified"):
            self.assertIn(key, entry)
        self.assertNotEqual(entry["pre_state"]["viewport_digest"],
                            entry["post_state"]["viewport_digest"])


class _Window:
    """Enough of a windows_control snapshot for scroll_window's OBSERVE step."""

    def __init__(self, title="OpenCode", handle=42, minimized=False,
                 bounds=(-1000, -10, 800, 700)):
        self._t = title
        self._h = handle
        self._m = minimized
        self._b = bounds

    def handle(self):
        return self._h

    def window_text(self):
        return self._t

    def is_minimized(self):
        return self._m

    def left(self):
        return self._b[0]

    def top(self):
        return self._b[1]

    def width(self):
        return self._b[2]

    def height(self):
        return self._b[3]


def _patch_resolution(window, title="OpenCode", handle=42):
    from source.server import windows_control as wc

    snapshot = {"title": title, "handle": handle, "minimized": False,
                "bounds": [window.left(), window.top(), window.width(),
                           window.height()]}
    return (mock.patch.object(wc, "_resolve", return_value=((window, snapshot), None)),
            mock.patch.object(wc, "_snapshot", return_value=snapshot))


class ForegroundRefusalTests(unittest.TestCase):
    """The exact 2026-10-04 failure: FRIDAY believed she was on OpenCode while
    the foreground was the System tray overflow window."""

    def setUp(self):
        ui_scroll.reset_action_history()

    def tearDown(self):
        ui_scroll.reset_action_history()

    def _run(self, foreground, window, **kwargs):
        from source.server import windows_control as wc

        resolve, snapshot = _patch_resolution(window)
        with resolve, snapshot, \
                mock.patch.object(ui_scroll, "foreground_handle",
                                  return_value=foreground), \
                mock.patch("pyautogui.moveTo"), \
                mock.patch("pyautogui.scroll") as wheel:
            result = ui_scroll.scroll_window({"handle": 42}, **kwargs)
        return result, wheel

    def test_a_non_foreground_target_is_refused_before_any_input(self):
        window = _Window()
        result, wheel = self._run(foreground=999, window=window,
                                  direction="up", amount=5)
        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])
        self.assertIn("not the foreground window", result["failure_reason"])
        wheel.assert_not_called()
        wheel.moveTo.assert_not_called() if hasattr(wheel, "moveTo") else None

    def test_the_refusal_is_recorded_as_unverified(self):
        window = _Window()
        self._run(foreground=999, window=window)
        history = ui_scroll.action_history()
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0]["verified"])

    def test_foreground_match_proceeds_to_the_fallback(self):
        window = _Window()
        digests = iter(["aaa", "bbb"])
        with mock.patch.object(ui_scroll, "viewport_digest",
                               side_effect=lambda rect: next(digests)):
            result, wheel = self._run(foreground=42, window=window,
                                      direction="up", amount=5, mechanism="wheel")
        self.assertTrue(result["success"], result.get("failure_reason"))
        self.assertTrue(result["verified"])
        self.assertEqual(result["mechanism"], "wheel")
        self.assertEqual(result["evidence"], "viewport_digest")
        wheel.assert_called_once_with(5)

    def test_a_minimized_window_is_refused(self):
        from source.server import windows_control as wc

        window = _Window()
        snapshot = {"title": "OpenCode", "handle": 42, "minimized": True,
                    "bounds": [0, 0, 100, 100]}
        with mock.patch.object(wc, "_resolve", return_value=((window, snapshot), None)), \
                mock.patch.object(wc, "_snapshot", return_value=snapshot), \
                mock.patch.object(ui_scroll, "foreground_handle", return_value=42):
            result = ui_scroll.scroll_window({"handle": 42}, direction="up")
        self.assertFalse(result["success"])
        self.assertIn("minimized", result["failure_reason"])

    def test_a_bare_window_name_is_refused_as_ambiguous(self):
        result = ui_scroll.scroll_window("OpenCode", direction="up")
        self.assertFalse(result["success"])
        self.assertIn("exact window target", result["failure_reason"])


class NoChangeHonestyTests(unittest.TestCase):
    def setUp(self):
        ui_scroll.reset_action_history()

    def tearDown(self):
        ui_scroll.reset_action_history()

    def test_an_unchanged_viewport_is_reported_as_failure_not_success(self):
        window = _Window()
        resolve, snapshot = _patch_resolution(window)
        with resolve, snapshot, \
                mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "viewport_digest",
                                  return_value="same"), \
                mock.patch("pyautogui.moveTo"), \
                mock.patch("pyautogui.scroll"):
            result = ui_scroll.scroll_window({"handle": 42}, direction="up",
                                            amount=5, mechanism="wheel")
        self.assertFalse(result["success"],
                         "an unchanged view must never be reported as success")
        self.assertFalse(result["verified"])
        self.assertIn("did not change", result["failure_reason"])
        self.assertEqual(result["changed"], False)

    def test_an_unobservable_viewport_is_not_claimed_as_success(self):
        window = _Window()
        resolve, snapshot = _patch_resolution(window)
        with resolve, snapshot, \
                mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "viewport_digest", return_value=None), \
                mock.patch("pyautogui.moveTo"), \
                mock.patch("pyautogui.scroll"):
            result = ui_scroll.scroll_window({"handle": 42}, direction="up",
                                            amount=5, mechanism="wheel")
        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])
        self.assertIn("could not be observed", result["failure_reason"])

    def test_uia_only_never_silently_falls_back_to_the_wheel(self):
        window = _Window()
        resolve, snapshot = _patch_resolution(window)
        with resolve, snapshot, \
                mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll._ua, "UIAutomation",
                                  lambda **k: _automation(
                                      [FakeControl(pattern=RaisesPattern())])), \
                mock.patch("pyautogui.moveTo"), \
                mock.patch("pyautogui.scroll") as wheel:
            result = ui_scroll.scroll_window({"handle": 42}, direction="up",
                                            amount=5, mechanism="uia")
        wheel.assert_not_called()
        self.assertFalse(result["success"])
        self.assertIsNone(result["mechanism"])
        self.assertIn("no container", result["failure_reason"])


class ScrollPromptDiscoverabilityTests(unittest.TestCase):
    def test_the_probe_summary_advertises_a_real_scrollable_container(self):
        result = {
            "available": True, "found": True,
            "window": {"title": "Doc", "process": "notepad.exe",
                       "minimized": False},
            "controls": [
                {"name": "viewport", "control_type": "Document",
                 "can_scroll": True, "can_scroll_v": True,
                 "scroll_percent_v": 42.0, "scroll_state": "scrollable"},
            ],
        }
        text = ui_probe.summary_for_prompt(result)
        self.assertIn("SCROLLABLE", text)
        self.assertIn("viewport", text)
        self.assertIn("42", text)
        self.assertIn("%", text)
        self.assertIn("ui_scroll", text)

    def test_the_summary_stays_bounded(self):
        controls = [{"name": "c%d" % i, "control_type": "Text",
                     "can_scroll": i == 0, "can_scroll_v": i == 0,
                     "scroll_percent_v": 0.0 if i == 0 else None,
                     "scroll_state": "scrollable" if i == 0 else "none"}
                    for i in range(200)]
        text = ui_probe.summary_for_prompt(
            {"available": True, "found": True,
             "window": {"title": "W", "process": "p", "minimized": False},
             "controls": controls})
        self.assertLessEqual(len(text), 1800)

    def test_an_inert_pattern_is_reported_as_such_in_the_summary(self):
        result = {
            "available": True, "found": True,
            "window": {"title": "OpenCode", "process": "OpenCode.exe",
                       "minimized": False},
            "controls": [{"name": "OpenCode", "control_type": "Document",
                          "can_scroll": False, "can_scroll_v": False,
                          "scroll_state": "exposed_not_scrollable"}],
        }
        text = ui_probe.summary_for_prompt(result)
        self.assertNotIn("SCROLLABLE in this window", text)
        self.assertIn("not scrollable", text)

    def test_every_target_form_the_prompt_shows_really_resolves(self):
        """The playbook is executable documentation, and must stay that way.

        Round Z first shipped a playbook line advertising
        scroll_window("app name", direction="up", amount=3) while the function
        refuses every bare string as ambiguous. A model following our own
        instructions could therefore only ever get a refusal back. Every unit
        test passed, because nothing ever ran the example.

        So this pulls each concrete target form out of the real playbook and
        runs it through the REAL windows_control._resolve against a controlled
        window universe. A documented form that cannot resolve now fails here
        instead of silently in front of Sir.
        """
        with open(_PROFILES_DEFAULT, encoding="utf-8") as handle:
            body = handle.read()
        block = body[body.index("=== SCROLLING"):][:4000]
        forms = re.findall(r"scroll_window\(\s*(\{[^}]*\})", block)
        self.assertTrue(forms, "the playbook must show at least one concrete target")

        universe = []
        for form in forms:
            target = ast.literal_eval(form)
            # Build the universe so the documented form MATCHES something. The
            # placeholder numbers in a playbook are not real handles; what is
            # under test is whether the FORM is one the resolver accepts.
            if target.get("handle"):
                handle = int(target["handle"])
                title, process = "Notepad", "notepad"
            else:
                handle = 777
                title = target.get("title")
                process = target.get("process")
            snapshot = {"title": title, "handle": handle, "process": process,
                        "class_name": "Notepad", "bounds": [0, 0, 800, 600]}
            universe = [_Window(title=title, handle=handle)]
            with mock.patch.object(wc, "_windows_status",
                                   return_value=(universe, None)), \
                 mock.patch.object(wc, "_snapshot", return_value=snapshot):
                match, failure = wc._resolve(target)
            self.assertIsNotNone(
                match, "playbook shows %s but it is refused: %r"
                % (form, (failure or {}).get("reason")))
            self.assertIsNone(failure)
            self.assertEqual(match[1]["handle"], handle)

    def test_the_prompt_must_not_advertise_a_bare_window_name(self):
        """The specific lie Round Z shipped: a bare name can never work."""
        with open(_PROFILES_DEFAULT, encoding="utf-8") as handle:
            body = handle.read()
        block = body[body.index("=== SCROLLING"):][:4000]
        bare = re.findall(r"scroll_window\(\s*\"([^\"]+)\"", block)
        self.assertEqual(
            bare, [],
            "the playbook must not show scroll_window(<bare string>): every bare "
            "name is refused as ambiguous, so those examples always fail")

    def test_the_prompt_playbook_offers_scrolling(self):
        with open(_PROFILES_DEFAULT, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("=== SCROLLING", body)
        self.assertIn("scroll_window", body)
        # The old text told her to give up or relaunch; scroll must be offered.
        self.assertIn("SCROLL it to bring the content into view", body)
        # Safety language must survive.
        self.assertIn("do NOT click anything blind", body)
        self.assertIn("never click blind coordinates", body)


# --------------------------------------------------------------------------
# LIVE tests against REAL Windows UIA objects.
# --------------------------------------------------------------------------
def _live_windows():
    try:
        from pywinauto import Desktop

        return Desktop(backend="uia").windows(top_level_only=True)
    except Exception:
        return []


class LiveUiaScrollTests(unittest.TestCase):
    """Real integration. Skips loudly rather than passing vacuously."""

    def setUp(self):
        ui_scroll.reset_action_history()

    def tearDown(self):
        ui_scroll.reset_action_history()

    def _any_window(self):
        for window in _live_windows():
            try:
                if (window.window_text() or "").strip():
                    return window
            except Exception:
                continue
        return None

    def test_live_probe_can_walk_a_real_window_without_raising(self):
        window = self._any_window()
        if window is None:
            self.skipTest("no UIA window available on this desktop")
        result = ui_probe.inspect_window(window.window_text())
        if not result.get("found"):
            result = ua.UIAutomation().dom(window.handle)
        self.assertTrue(result.get("available"))
        self.assertIn("controls", result)
        # Every control must carry the scroll facts, even when not scrollable.
        for control in (result.get("controls") or [])[:10]:
            self.assertIn("can_scroll", control)
            self.assertIn("scroll_state", control)

    def test_live_scroll_state_classification_is_honest(self):
        """On a real tree, `exposes_scroll` must never imply `can_scroll` for
        every element - that was the broken naive probe."""
        window = self._any_window()
        if window is None:
            self.skipTest("no UIA window available on this desktop")
        try:
            descendants = list(window.descendants())[:40]
        except Exception:
            self.skipTest("window exposes no descendants")
        for element in descendants:
            state = ua._scroll_state(element)
            if state["exposes_scroll"]:
                self.assertIn(state["scroll_state"],
                              ("scrollable", "reported_but_not_scrollable",
                               "exposed_not_scrollable"))
            else:
                self.assertFalse(state["can_scroll_v"])
                self.assertFalse(state["can_scroll_h"])

    def test_live_viewport_digest_of_a_real_window_is_stable(self):
        """DETERMINISM of the digest function, proven against fixed pixels.

        This used to pick the first window with a non-empty title and assert two
        captures 0.4s apart matched. That window is usually the Windows 11
        **Taskbar**, which carries a live clock - so the assertion was inherently
        racy, failing roughly once a minute when the displayed time changed. A
        changing digest is CORRECT behaviour: the digest exists to notice that
        pixels moved. The real invariant is "identical pixels give an identical
        digest", which is testable without a static desktop.

        all_screens=True and the degenerate-rect case are already covered
        deterministically by ViewportDigestTests; this adds only the
        pixels-in / digest-out determinism and sensitivity.
        """
        from PIL import Image

        fixed = Image.new("RGB", (240, 160), (12, 34, 56))
        with mock.patch("PIL.ImageGrab.grab", return_value=fixed):
            first = ui_scroll.viewport_digest((0, 0, 240, 160))
            second = ui_scroll.viewport_digest((0, 0, 240, 160))
        self.assertIsNotNone(first)
        self.assertEqual(first, second,
                         "identical pixels must digest identically")

        changed = Image.new("RGB", (240, 160), (200, 200, 200))
        with mock.patch("PIL.ImageGrab.grab", return_value=changed):
            third = ui_scroll.viewport_digest((0, 0, 240, 160))
        self.assertNotEqual(first, third,
                            "different pixels must NOT digest identically")

    def test_live_viewport_digest_returns_a_shape_for_a_real_window(self):
        """The live path still produces a usable fingerprint. Only the SHAPE is
        asserted, because whether two captures of an arbitrary live window agree
        depends on that window actually being static."""
        window = self._any_window()
        if window is None:
            self.skipTest("no UIA window available on this desktop")
        rect = ui_scroll.rect_of(window)
        if not rect:
            self.skipTest("window has no usable rect")
        digest = ui_scroll.viewport_digest(rect)
        self.assertIsNotNone(digest)
        self.assertEqual(len(digest), 32)
        int(digest, 16)

    def test_live_foreground_handle_matches_a_real_foreground_window(self):
        handle = ui_scroll.foreground_handle()
        self.assertIsNotNone(handle)
        self.assertGreater(int(handle), 0)


class InspectWindowScrollFactTests(unittest.TestCase):
      """ROUND ZB-L1: `inspect_window` must not drop the scroll facts.

      It rebuilds a minimal, prompt-bounded record from the rich `dom()`
      snapshot. That rebuild originally kept only name/type/automation_id/
      value/enabled, so a caller inspecting a window could not tell a scrollable
      container from a static one - the opposite of the discoverability this
      round exists to provide.

      This stayed hidden because the only test that asserted the facts picked
      its window by "first non-empty title", which happened to fall through to
      `dom()` (whose records DO carry them). Once a Chromium window with a
      non-empty title came first, `inspect_window` succeeded and its own
      stripped records surfaced. These tests are deterministic on purpose.
      """

      def _inspect(self, dom_result):
          with mock.patch.object(ui_probe, "available",
                                 return_value={"available": True}), \
               mock.patch.object(ui_probe, "find_window",
                                 return_value=[{"handle": 42,
                                                "title": "App"}]), \
               mock.patch.object(ui_probe._ua, "UIAutomation",
                                 lambda **k: mock.Mock(dom=lambda h: dom_result)):
              return ui_probe.inspect_window("App")

      def test_a_scrollable_container_is_visible_through_inspect_window(self):
          result = self._inspect({
              "available": True,
              "controls": [{
                  "name": "scrollable content", "control_type": "Group",
                  "automation_id": "", "value": "", "enabled": True,
                  "can_scroll": True, "can_scroll_v": True,
                  "can_scroll_h": False, "scroll_state": "scrollable",
                  "scroll_percent_v": 100.0,
              }],
          })
          control = result["controls"][0]
          self.assertTrue(control["can_scroll"])
          self.assertTrue(control["can_scroll_v"])
          self.assertFalse(control["can_scroll_h"])
          self.assertEqual(control["scroll_state"], "scrollable")
          self.assertEqual(control["scroll_percent_v"], 100.0)

      def test_every_control_carries_the_facts_even_when_not_scrollable(self):
          result = self._inspect({
              "available": True,
              "controls": [{"name": "Button", "control_type": "Button",
                            "automation_id": "", "value": "", "enabled": True,
                            "can_scroll": False, "can_scroll_v": False,
                            "can_scroll_h": False, "scroll_state": "none",
                            "scroll_percent_v": None}],
          })
          for control in result["controls"]:
              for key in ("can_scroll", "can_scroll_v", "can_scroll_h",
                          "scroll_state", "scroll_percent_v"):
                  self.assertIn(key, control)

      def test_missing_scroll_keys_degrade_instead_of_raising(self):
          """An older/absent snapshot must still produce a usable record."""
          result = self._inspect({
              "available": True,
              "controls": [{"name": "Pane", "control_type": "Pane",
                            "automation_id": "", "value": "", "enabled": True}],
          })
          control = result["controls"][0]
          self.assertFalse(control["can_scroll"])
          self.assertEqual(control["scroll_state"], "none")
          self.assertIsNone(control["scroll_percent_v"])



class ScrollEvidencePropagationTests(unittest.TestCase):
      """ROUND ZB-L1: `ui_probe.act` must forward perform()'s evidence block.

      It copied only success / changed / failure_reason, so a caller could see
      THAT something happened but never WHY: a scroll always reported
      `percent_moved = None` even when the container had published a scroll
      percentage, and the per-turn action record could not show pre/post UIA
      position. The verdict was never wrong - perform() remained authoritative -
      but the evidence required to audit it was thrown away.
      """

      def _act(self, outcome):
          """Drive the real act() with perform() stubbed to `outcome`."""
          probe = mock.Mock()
          probe.dom.return_value = {
              "available": True,
              "controls": [{"name": "scrollable content",
                            "target_token": "tok-1"}],
          }
          probe.perform.return_value = outcome
          uia = mock.Mock()
          uia.UIAutomation.return_value = probe
          uia.risk_metadata.return_value = {"read_only": False,
                                            "destructive": False,
                                            "reversible": True}
          import source.server.action_confirmations as confirm
          with mock.patch.object(ui_probe, "available",
                                 return_value={"available": True}), \
               mock.patch.object(ui_probe, "find_window",
                                 return_value=[{"handle": 42, "title": "App",
                                                "minimized": False}]), \
               mock.patch.object(ui_probe, "_ua", uia), \
               mock.patch.object(confirm, "mint",
                                 return_value=("signed-token", None)), \
               mock.patch.object(confirm, "consume",
                                 return_value=(True, "",
                                               {"granted_by": "yes"})):
              return ui_probe.act("App", "scrollable content", action="scroll",
                                  value={"direction": "up", "amount": 3})

      def test_scroll_block_and_evidence_are_forwarded(self):
          result = self._act({
              "success": True, "changed": True, "verified": True,
              "mechanism": "uia_scroll_pattern", "evidence": "scroll_percent",
              "failure_reason": None,
              "scroll": {"direction": "up", "amount": 3,
                         "before": {"scroll_percent_v": 100.0},
                         "after": {"scroll_percent_v": 99.7},
                         "percent_moved": True},
          })
          self.assertEqual(result["evidence"], "scroll_percent")
          self.assertEqual(result["scroll"]["before"]["scroll_percent_v"], 100.0)
          self.assertEqual(result["scroll"]["after"]["scroll_percent_v"], 99.7)
          self.assertTrue(result["scroll"]["percent_moved"])

      def test_a_failed_scroll_keeps_its_no_change_verdict_and_evidence(self):
          result = self._act({
              "success": False, "changed": False, "verified": False,
              "mechanism": "uia_scroll_pattern", "evidence": "scroll_percent",
              "failure_reason": "action produced no observable state change",
              "scroll": {"direction": "down", "amount": 3,
                         "before": {"scroll_percent_v": 100.0},
                         "after": {"scroll_percent_v": 100.0},
                         "percent_moved": False},
          })
          self.assertFalse(result["success"])
          self.assertFalse(result["scroll"]["percent_moved"])
          self.assertEqual(result["evidence"], "scroll_percent")
          self.assertIn("no observable state change", result["failure_reason"])

      def test_outcomes_without_a_scroll_block_are_left_alone(self):
          """A click outcome has no scroll evidence; nothing may be invented."""
          result = self._act({"success": True, "changed": True,
                              "failure_reason": None})
          self.assertNotIn("scroll", result)
          self.assertNotIn("evidence", result)
          self.assertTrue(result["success"])



class PinnedControlTargetTests(unittest.TestCase):
      """ROUND ZB-L1: a resolved control must be pinned by identity, not name.

      OpenCode's TUI exposes FIVE distinct controls all named "scrollable
      content". `scroll_window` correctly picks the largest usable container,
      but `ui_probe.act` re-resolved by NAME and hit its ambiguity guard, so a
      correctly-chosen target was refused with "5 controls are named ...
      refusing to guess". The fix lets a caller that already holds the identity
      pin the snapshot index. Consent is untouched: the signed token is still
      minted for (action, window, control NAME).
      """

      def _act(self, control_index, indices):
          probe = mock.Mock()
          probe.dom.return_value = {
              "available": True,
              "controls": [{"name": "scrollable content", "index": i,
                            "target_token": "tok-%s" % i} for i in indices],
          }
          probe.perform.return_value = {"success": True, "changed": True,
                                        "failure_reason": None}
          uia = mock.Mock()
          uia.UIAutomation.return_value = probe
          uia.risk_metadata.return_value = {"read_only": False,
                                            "destructive": False,
                                            "reversible": True}
          import source.server.action_confirmations as confirm
          with mock.patch.object(ui_probe, "available",
                                 return_value={"available": True}), \
               mock.patch.object(ui_probe, "find_window",
                                 return_value=[{"handle": 42, "title": "App",
                                                "minimized": False}]), \
               mock.patch.object(ui_probe, "_ua", uia), \
               mock.patch.object(confirm, "mint",
                                 return_value=("t", None)), \
               mock.patch.object(confirm, "consume",
                                 return_value=(True, "", {"granted_by": "yes"})):
              return ui_probe.act("App", "scrollable content", action="scroll",
                                  value={"direction": "up", "amount": 3},
                                  control_index=control_index), probe

      def test_pinned_index_selects_that_exact_control(self):
          result, probe = self._act(35, [26, 35, 41, 47, 53])
          self.assertTrue(result["success"])
          probe.perform.assert_called_once()
          self.assertEqual(probe.perform.call_args[0][1], "tok-35")

      def test_without_a_pin_duplicates_are_still_refused(self):
          """The ambiguity guard must still hold for callers that have NOT
          resolved an identity - this is a safety property, not a nuisance."""
          result, probe = self._act(None, [26, 35, 41])
          self.assertFalse(result["success"])
          self.assertIn("refusing to guess", result["failure_reason"])
          probe.perform.assert_not_called()

      def test_a_stale_pin_is_refused_rather_than_guessed(self):
          result, probe = self._act(999, [26, 35, 41])
          self.assertFalse(result["success"])
          self.assertIn("not present at index", result["failure_reason"])
          probe.perform.assert_not_called()

      def test_a_unique_name_still_needs_no_pin(self):
          result, probe = self._act(None, [26])
          self.assertTrue(result["success"])
          self.assertEqual(probe.perform.call_args[0][1], "tok-26")


if __name__ == "__main__":
    unittest.main()

