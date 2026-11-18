"""ROUND ZB-L3 L3-3: `computer.mouse.scroll` must not bypass verification.

THE BYPASS
----------
Round Z built OBSERVE -> LOCATE -> ACT -> VERIFY in `ui_scroll.py`: the
semantic path goes through the existing HMAC single-use consent gate, and the
wheel fallback is bounded by foreground proof, cursor placement and a
demonstrated viewport change. Those bounds live INSIDE `scroll_window`.

`computer.mouse.scroll(clicks)` was never routed through it. Live on
2026-10-07, against a healthy Modal provider, FRIDAY used exactly that
bypass on OpenCode - `computer.mouse.scroll(-5)`, no consent, no foreground
proof, no architectural verification - and the turn then died at the tool cap
having told Sir nothing.

A refusal was rejected as the fix, because a lower-level route to the same OS
call would remain. So this is INTERPOSITION, and these tests hold all seven
properties the build requires.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from source.server import mouse_scroll_guard as guard  # noqa: E402
from source.server import ui_scroll  # noqa: E402


class _FakeMouse:
    def __init__(self, computer=None):
        self.computer = computer
        self.raw_calls = []
        self.released = False

    def scroll(self, clicks):
        """The ORIGINAL vendored behaviour, standing in for pyautogui.scroll."""
        self.raw_calls.append(clicks)

    def release(self):
        self.released = True


class _FakeComputer:
    def __init__(self):
        self.mouse = _FakeMouse(self)


def _fake_result(**kwargs):
    base = {
        "handled": True, "action": "ui_scroll", "mechanism": "wheel",
        "risk": "reversible", "allowed": True, "changed": True,
        "success": True, "verified": True, "scroll_verified": True,
        "failure_reason": None, "evidence": "viewport_digest",
        "window": "OpenCode",
        "scroll": {"direction": "up", "amount": 3},
    }
    base.update(kwargs)
    return base


class NotchingTests(unittest.TestCase):
    def test_positive_notches_scroll_up_and_negative_scroll_down(self):
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=99), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  return_value=_fake_result()) as spy:
            guard.scroll_via_verified_path(3)
            self.assertEqual(spy.call_args.kwargs["direction"], "up")
            guard.scroll_via_verified_path(-3)
            self.assertEqual(spy.call_args.kwargs["direction"], "down")

    def test_amount_is_the_absolute_notch_count(self):
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=99), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  return_value=_fake_result()) as spy:
            guard.scroll_via_verified_path(-7)
            self.assertEqual(spy.call_args.kwargs["amount"], 7)

    def test_absurd_notch_counts_are_clamped_into_the_legal_range(self):
        for clicks, expected in ((999, 25), (-999, 25), (1, 1), (-1, 1)):
            with mock.patch.object(ui_scroll, "foreground_handle",
                                   return_value=99), \
                    mock.patch.object(ui_scroll, "scroll_window",
                                      return_value=_fake_result()) as spy:
                guard.scroll_via_verified_path(clicks)
                self.assertEqual(spy.call_args.kwargs["amount"], expected)

    def test_zero_and_garbage_are_refused_without_touching_the_os(self):
        computer = _FakeComputer()
        with mock.patch.object(ui_scroll, "scroll_window") as spy:
            for bad in (0, None, "abc", object()):
                self.assertIsNone(guard.scroll_via_verified_path(
                    bad, computer=computer))
        spy.assert_not_called()
        self.assertEqual(computer.mouse.raw_calls, [])


class BypassClosureTests(unittest.TestCase):
    """1. raw scroll no longer bypasses verification.
       7. no direct unverified wheel event reaches the OS through this path."""

    def test_a_raw_scroll_never_reaches_pyautogui_directly(self):
        computer = _FakeComputer()
        self.assertTrue(guard.install_mouse_scroll_guard(computer))
        computer.mouse.scroll(5)
        self.assertEqual(computer.mouse.raw_calls, [],
                         "the raw wheel call must never be reached")

    def test_the_installed_scroll_goes_through_ui_scroll(self):
        computer = _FakeComputer()
        guard.install_mouse_scroll_guard(computer)
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  return_value=_fake_result()) as spy:
            result = computer.mouse.scroll(-4)
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[0], {"handle": 42})
        self.assertTrue(result["success"])

    def test_installation_is_idempotent(self):
        computer = _FakeComputer()
        self.assertTrue(guard.install_mouse_scroll_guard(computer))
        first = computer.mouse.scroll
        self.assertFalse(guard.install_mouse_scroll_guard(computer))
        self.assertIs(computer.mouse.scroll, first,
                      "a second install must not double-wrap")

    def test_the_original_is_kept_but_never_called(self):
        computer = _FakeComputer()
        guard.install_mouse_scroll_guard(computer)
        patched = computer.mouse.scroll
        self.assertTrue(hasattr(patched, "_friday_original"))
        computer.mouse.scroll(3)
        self.assertEqual(computer.mouse.raw_calls, [])

    def test_ui_scroll_is_asked_for_auto_mechanism(self):
        """auto = gated semantic first, bounded wheel second. Never the reverse."""
        computer = _FakeComputer()
        guard.install_mouse_scroll_guard(computer)
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=7), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  return_value=_fake_result()) as spy:
            computer.mouse.scroll(2)
        self.assertEqual(spy.call_args.kwargs["mechanism"], "auto")


class LegitimateScrollTests(unittest.TestCase):
    """2. legitimate verified scrolling still works."""

    def test_a_verified_scroll_reports_its_mechanism_and_evidence(self):
        computer = _FakeComputer()
        guard.install_mouse_scroll_guard(computer)
        result_payload = _fake_result(mechanism="uia_scroll_pattern",
                                      evidence="scroll_percent")
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  return_value=result_payload):
            result = computer.mouse.scroll(-3)
        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["mechanism"], "uia_scroll_pattern")
        self.assertEqual(result["evidence"], "scroll_percent")

    def test_the_architecture_remains_the_single_authority(self):
        """The guard never decides success itself; ui_scroll does."""
        computer = _FakeComputer()
        guard.install_mouse_scroll_guard(computer)
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  return_value=_fake_result()) as spy:
            computer.mouse.scroll(3)
        called_with = spy.call_args.kwargs
        self.assertIn("actor", called_with)
        self.assertEqual(called_with["actor"], "kernel")


class RefusalTests(unittest.TestCase):
    """3,4,5,6. Each refusal must surface, and none may scroll."""

    def _install(self):
        computer = _FakeComputer()
        guard.install_mouse_scroll_guard(computer)
        return computer

    def test_missing_authorisation_is_refused(self):
        """ui_scroll refuses a semantic act with no consent; that refusal must
        reach us and must NOT be converted into a wheel input."""
        computer = self._install()
        denied = _fake_result(success=False, verified=False, allowed=False,
                              changed=False, mechanism=None,
                              failure_reason=(
                                  "refused: no confirmation in Sir's own words "
                                  "for this turn"))
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window", return_value=denied):
            result = computer.mouse.scroll(-3)
        self.assertIsNone(result)
        self.assertEqual(computer.mouse.raw_calls, [],
                         "a denied consent must never become a raw wheel event")

    def test_a_stale_or_reused_authorisation_is_refused(self):
        """ui_scroll reports the spent token as a refusal; we honour it."""
        computer = self._install()
        stale = _fake_result(success=False, verified=False, allowed=False,
                             changed=False,
                             failure_reason="refused: confirmation already used")
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window", return_value=stale):
            self.assertIsNone(computer.mouse.scroll(3))
        self.assertEqual(computer.mouse.raw_calls, [])

    def test_foreground_mismatch_is_refused(self):
        computer = self._install()
        mismatch = _fake_result(success=False, verified=False, allowed=False,
                                changed=False,
                                failure_reason=(
                                    "refused: not the foreground window"))
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=1), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  return_value=mismatch):
            self.assertIsNone(computer.mouse.scroll(3))
        self.assertEqual(computer.mouse.raw_calls, [])

    def test_an_unknown_foreground_window_is_refused_before_any_call(self):
        computer = self._install()
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=None), \
                mock.patch.object(ui_scroll, "scroll_window") as spy:
            self.assertIsNone(computer.mouse.scroll(3))
        spy.assert_not_called()
        self.assertEqual(computer.mouse.raw_calls, [])

    def test_a_true_no_op_is_reported_as_failure_not_success(self):
        computer = self._install()
        noop = _fake_result(success=False, verified=False, allowed=True,
                            changed=False, mechanism="wheel",
                            failure_reason=("scroll was sent but 'OpenCode' did "
                                            "not change, so the view did not move"))
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window", return_value=noop):
            self.assertIsNone(computer.mouse.scroll(3))
        self.assertEqual(computer.mouse.raw_calls, [])

    def test_an_ambiguous_or_unresolved_target_is_refused(self):
        computer = self._install()
        ambiguous = _fake_result(success=False, verified=False, allowed=False,
                                 changed=False,
                                 failure_reason="window target resolved to an unusable shape")
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  return_value=ambiguous):
            self.assertIsNone(computer.mouse.scroll(3))

    def test_an_exception_inside_the_architecture_refuses_safely(self):
        """Never raise into model-authored code, never send the wheel event."""
        computer = self._install()
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window",
                                  side_effect=RuntimeError("uia exploded")):
            self.assertIsNone(computer.mouse.scroll(3))
        self.assertEqual(computer.mouse.raw_calls, [])

    def test_a_non_dict_result_is_refused(self):
        computer = self._install()
        with mock.patch.object(ui_scroll, "foreground_handle", return_value=42), \
                mock.patch.object(ui_scroll, "scroll_window", return_value=None):
            self.assertIsNone(computer.mouse.scroll(3))

    def test_a_refusal_prints_a_reason_the_model_can_read(self):
        """The model must be able to see WHY, or it will retry the same call."""
        import io
        import contextlib

        computer = self._install()
        denied = _fake_result(success=False, verified=False, allowed=False,
                              failure_reason="refused: no consent")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with mock.patch.object(ui_scroll, "foreground_handle",
                                   return_value=42), \
                    mock.patch.object(ui_scroll, "scroll_window",
                                      return_value=denied):
                computer.mouse.scroll(3)
        self.assertIn("REFUSED", buffer.getvalue())
        self.assertIn("no consent", buffer.getvalue())


class SitePackagesReachTests(unittest.TestCase):
    """The kernel is a SEPARATE process importing the same file."""

    def test_the_shim_exists_and_refuses_without_the_friday_package(self):
        shim = guard.SITE_PACKAGES_SHIM
        self.assertIn("def _friday_verified_scroll(", shim)
        self.assertIn("Mouse.scroll = _friday_verified_scroll", shim)

    def test_the_shim_fails_safe_when_the_package_cannot_be_imported(self):
        shim = guard.SITE_PACKAGES_SHIM
        self.assertIn("except Exception as _friday_import_error", shim)
        # The message is split across adjacent string literals in the source,
        # so compare against the joined runtime text rather than raw source.
        joined = " ".join(
            part.strip().strip('"')
            for part in shim.split("print(")[1].split("\n")[:3]
        )
        self.assertIn("no wheel event was sent", shim.replace('" "', "")
                      .replace('"\n              "', ""))
        self.assertIn("REFUSED", joined)
        # It must never mention pyautogui as a fallback.
        self.assertNotIn("pyautogui.scroll", shim)

    def test_the_shim_keeps_the_original_oi_signature(self):
        """`scroll(self, clicks)` must stay callable the way OI calls it."""
        shim = guard.SITE_PACKAGES_SHIM
        self.assertIn("def _friday_verified_scroll(self, clicks)", shim)

    def test_the_display_patch_installs_the_guard_at_boot(self):
        here = os.path.dirname(os.path.abspath(__file__))
        source = open(os.path.join(here, "source", "server", "display_patch.py"),
                      encoding="utf-8").read()
        self.assertIn("_ensure_mouse_scroll_guard", source)
        self.assertIn("install_mouse_scroll_guard", source)

    def test_the_boot_heal_is_idempotent_by_marker(self):
        here = os.path.dirname(os.path.abspath(__file__))
        source = open(os.path.join(here, "source", "server", "display_patch.py"),
                      encoding="utf-8").read()
        self.assertIn('marker = "FRIDAY patch (ROUND ZB-L3 L3-3)"', source)
        self.assertIn("if marker in text", source)

    def test_the_heal_repairs_an_interrupted_write(self):
        """A previous append without the marker must not double-apply."""
        here = os.path.dirname(os.path.abspath(__file__))
        source = open(os.path.join(here, "source", "server", "display_patch.py"),
                      encoding="utf-8").read()
        self.assertIn('if "def _friday_verified_scroll(" in base:', source)


class NoSecondConsentSystemTests(unittest.TestCase):
    """The build must not weaken or duplicate the consent model."""

    def test_the_guard_creates_no_token_of_its_own(self):
        source_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "source", "server", "mouse_scroll_guard.py")
        source = open(source_path, encoding="utf-8").read()
        for forbidden in ("action_confirmations", "mint(", "consume(",
                          "hmac", "confirmed="):
            self.assertNotIn(forbidden, source,
                             "the guard must not be a second consent system")

    def test_it_never_passes_a_confirmation_of_its_own(self):
        source_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "source", "server", "mouse_scroll_guard.py")
        source = open(source_path, encoding="utf-8").read()
        self.assertNotIn("confirmation=", source)

    def test_the_existing_consent_gate_is_untouched(self):
        here = os.path.dirname(os.path.abspath(__file__))
        source = open(os.path.join(here, "source", "server",
                                   "action_confirmations.py"),
                      encoding="utf-8").read()
        self.assertIn("def mint(", source)
        self.assertIn("def consume(", source)


if __name__ == "__main__":
    unittest.main()
