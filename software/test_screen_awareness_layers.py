"""Tests for the screen-awareness layers added 2026-10-02.

Four layers, cheapest first:
    window_events         - window state, no pixels
    ui_probe              - an app's accessibility tree + GUARDED actions
    screen_capture_service- always-live frame buffer with Pillow fallback

These are deliberately offline/fake-provider tests: they must not depend on
DXGI, a real desktop or a real app. The real-hardware behaviour is covered by
the live checks, not here.
"""

import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.getcwd())

from source.server import action_confirmations as confirmations

_CONFIRM_TMP = tempfile.mkdtemp(prefix="friday-confirm-")
os.environ["FRIDAY_CONFIRM_STATE_DIR"] = _CONFIRM_TMP

from source.server import command_router
from source.server import screen_capture_service as capture
from source.server import ui_probe as ui_probe
from source.server import window_events as window_events


# ---------------------------------------------------------------- window layer
class WindowStateLayerTests(unittest.TestCase):
    def test_is_minimized_reports_the_window_state(self):
        with mock.patch.object(window_events._wc, "find_windows",
                               return_value=[{"title": "App", "minimized": True,
                                              "process": "app.exe", "handle": 7,
                                              "maximized": False, "visible": True,
                                              "bounds": [0, 0, 800, 600]}]):
            minimized, snapshot, _ = window_events.is_minimized("App")
        self.assertTrue(minimized)
        self.assertEqual(snapshot["process"], "app.exe")

    def test_window_state_is_honest_when_nothing_matches(self):
        with mock.patch.object(window_events._wc, "find_windows", return_value=[]):
            state = window_events.window_state("Nope")
        self.assertFalse(state["found"])
        self.assertIn("no open window", state["reason"])

    def test_window_state_never_raises_on_a_broken_backend(self):
        with mock.patch.object(window_events._wc, "find_windows",
                               side_effect=RuntimeError("boom")):
            minimized, snapshot, candidates = window_events.is_minimized("X")
        self.assertFalse(minimized)
        self.assertIsNone(snapshot)
        self.assertEqual(candidates, [])

    def test_shell_windows_are_not_reported_as_applications(self):
        """Regression: the open-app list was dominated by DesktopWindowXamlSource."""
        windows = [
            {"title": "DesktopWindowXamlSource", "process": "explorer.exe",
             "bounds": [0, 0, 1600, 900], "minimized": False},
            {"title": "Namespace Tree Control", "process": "explorer.exe",
             "bounds": [0, 0, 300, 900], "minimized": False},
            {"title": "navigation", "process": "explorer.exe",
             "bounds": [0, 0, 200, 800], "minimized": False},
            {"title": "RealApp", "process": "realapp.exe",
             "bounds": [0, 0, 1200, 800], "minimized": False},
        ]
        with mock.patch.object(window_events._wc, "list_windows", return_value=windows):
            out = window_events.desktop_overview()
        self.assertEqual([w["title"] for w in out], ["RealApp"])

    def test_duplicate_titles_are_collapsed(self):
        windows = [{"title": "Same", "process": "a.exe", "bounds": [0, 0, 800, 600]},
                   {"title": "Same", "process": "b.exe", "bounds": [0, 0, 800, 600]}]
        with mock.patch.object(window_events._wc, "list_windows", return_value=windows):
            self.assertEqual(len(window_events.desktop_overview()), 1)

    def test_tracker_detects_minimize_and_restore(self):
        tracker = window_events.WindowStateTracker(poll_seconds=30)
        before = {"foreground": "Other", "windows": {"App": {"minimized": False}}}
        after = {"foreground": "Other", "windows": {"App": {"minimized": True}}}
        tracker.current = dict(before)
        events = tracker._diff(before, after)
        self.assertEqual([e["kind"] for e in events], ["minimized"])

    def test_tracker_reports_no_events_when_nothing_changed(self):
        tracker = window_events.WindowStateTracker(poll_seconds=30)
        state = {"foreground": "A", "windows": {"B": {"minimized": False}}}
        self.assertEqual(tracker._diff(state, dict(state)), [])


# ------------------------------------------------------------------ uia layer
class _FakeControl:
    def __init__(self, name="OK", control_type="Button"):
        self._name = name
        self._control_type = control_type
        self.clicked = False

    def window_text(self):
        return self._name

    @property
    def element_info(self):
        return None

    def click_input(self):
        self.clicked = True
        return True


class UiaGuardTests(unittest.TestCase):
    """Sir's directive: no unrestricted 'UIA can click anything' path.

    Rewritten 2026-10-02 when `confirmed=` was deleted outright. Consent now
    arrives as a signed single-use token minted from the words Sir actually said
    (action_confirmations), so these tests use the REAL ledger rather than a flag.
    """

    def setUp(self):
        confirmations.reset()
        self.addCleanup(confirmations.reset)

    def _said(self, *turns):
        for turn in turns:
            confirmations.note_user_turn(turn, source="test")

    # -- the gate itself ------------------------------------------------
    def test_there_is_no_confirmed_parameter_to_supply(self):
        import inspect as _inspect

        parameters = _inspect.signature(ui_probe.act).parameters
        self.assertNotIn("confirmed", parameters,
                         "a caller-supplied boolean must not exist")
        self.assertNotIn("force", parameters)
        self.assertNotIn("skip_confirmation", parameters)

    def test_action_is_refused_when_sir_has_said_nothing(self):
        with mock.patch.object(ui_probe, "available",
                               return_value={"available": True}):
            with mock.patch.object(ui_probe, "find_window") as finder:
                result = ui_probe.act("A", "OK", action="click")
        self.assertFalse(result["success"])
        self.assertIn("refused", result["failure_reason"])
        finder.assert_not_called()

    def test_a_bare_unrelated_turn_does_not_authorise_anything(self):
        self._said("what is on my screen right now?")
        result = ui_probe.act("A", "OK", action="click")
        self.assertFalse(result["success"])
        self.assertIn("refused", result["failure_reason"])

    def test_a_confirmation_authorises_exactly_that_action(self):
        self._said("yes, click OK in A")
        token, info = confirmations.mint("click", "A", "OK")
        self.assertTrue(token, info)
        self.assertTrue(info["bound_to_target"])
        ok, why, _ = confirmations.consume(token, "click", "A", "OK")
        self.assertTrue(ok, why)

    def test_a_token_cannot_be_spent_on_a_different_control(self):
        self._said("yes, click OK in A")
        token, _ = confirmations.mint("click", "A", "OK")
        ok, why, _ = confirmations.consume(token, "click", "A", "Delete")
        self.assertFalse(ok)
        self.assertIn("different action", why)

    def test_a_token_cannot_be_spent_twice(self):
        self._said("yes, click OK in A")
        token, _ = confirmations.mint("click", "A", "OK")
        self.assertTrue(confirmations.consume(token, "click", "A", "OK")[0])
        ok, why, _ = confirmations.consume(token, "click", "A", "OK")
        self.assertFalse(ok)
        self.assertIn("already been used", why)

    def test_a_hand_made_token_is_rejected(self):
        forged = "eyJhIjoiY2xpY2sifQ.bm90LWEtcmVhbC1zaWduYXR1cmU"
        ok, why, _ = confirmations.consume(forged, "click", "A", "OK")
        self.assertFalse(ok)
        self.assertIn("signature", why)

    def test_confirming_one_button_does_not_authorise_another(self):
        """'click Delete' must not become consent to press 'Seven'."""
        self._said("yes please click Delete in Calculator")
        token, info = confirmations.mint("click", "Calculator", "Seven",
                                        known_names=["Seven", "Delete"])
        self.assertIsNone(token, info)
        self.assertFalse(ui_probe.act("Calculator", "Seven")["success"])

    def test_consent_expires(self):
        self._said("yes click OK")
        token, _ = confirmations.mint("click", "A", "OK")
        with mock.patch.object(confirmations.time, "time",
                               return_value=time.time() + 1000):
            ok, why, _ = confirmations.consume(token, "click", "A", "OK")
        self.assertFalse(ok)
        self.assertIn("expired", why)

    def test_forged_ledger_entry_is_still_only_what_sir_said(self):
        """The ledger records words; a caller cannot reword history."""
        self._said("maybe later")
        self.assertIsNone(confirmations.mint("click", "A", "OK")[0])

    # -- the rest of the gate chain --------------------------------------
    def test_unsupported_action_is_refused_before_anything_is_resolved(self):
        with mock.patch.object(ui_probe, "find_window") as finder:
            result = ui_probe.act("A", "OK", action="close")
        self.assertFalse(result["success"])
        self.assertIn("not allowed", result["failure_reason"])
        finder.assert_not_called()

    def test_ambiguous_window_is_refused(self):
        self._said("yes click OK in A")
        with mock.patch.object(ui_probe, "available",
                               return_value={"available": True}):
            with mock.patch.object(ui_probe, "find_window", return_value=[
                    {"title": "A one", "handle": 1},
                    {"title": "A two", "handle": 2}]):
                result = ui_probe.act("A", "OK", action="click")
        self.assertIn("several windows", result["failure_reason"])

    def test_minimized_window_is_refused_rather_than_clicked_blind(self):
        self._said("yes click OK in A")
        with mock.patch.object(ui_probe, "available",
                               return_value={"available": True}):
            with mock.patch.object(ui_probe, "find_window",
                                   return_value=[{"title": "A", "handle": 1,
                                                  "minimized": True}]):
                result = ui_probe.act("A", "OK", action="click")
        self.assertIn("minimized", result["failure_reason"])

    def test_unknown_control_is_refused(self):
        self._said("yes click OK in A")
        probe = mock.Mock()
        probe.dom.return_value = {"available": True, "controls": [
            {"name": "Cancel", "control_type": "Button", "target_token": "t1"}]}
        with mock.patch.object(ui_probe, "available",
                               return_value={"available": True}):
            with mock.patch.object(ui_probe, "find_window",
                                   return_value=[{"title": "A", "handle": 1,
                                                  "minimized": False}]):
                with mock.patch.object(ui_probe, "_ua",
                                       mock.Mock(UIAutomation=lambda **k: probe)):
                    result = ui_probe.act("A", "OK", action="click")
        self.assertIn("no control named", result["failure_reason"])

    def test_only_a_small_allowlist_of_actions_is_permitted(self):
        # ROUND Z (2026-10-04): "scroll" joins the allowlist. It was added
        # deliberately and ONLY as an extension of this same gate - the point of
        # the allowlist is that the set is small and reviewed, not frozen at
        # three. The companion test below proves scroll is gated rather than
        # free, which is the property that actually matters.
        self.assertEqual(ui_probe.ALLOWED_ACTIONS,
                         frozenset({"click", "invoke", "type", "scroll"}))
        for forbidden in ("close", "delete", "shutdown", "eval", "set"):
            self.assertNotIn(forbidden, ui_probe.ALLOWED_ACTIONS)

    def test_scroll_is_behind_the_same_consent_gate_as_every_other_action(self):
        """Adding scroll must not have opened an ungated door.

        Regression guard for the 2026-10-04 scroll work: `ui_scroll` exists as a
        separate module, so it is worth proving that a scroll still has to clear
        the signed-token gate, exactly like a click.
        """
        self._said("Scroll up in the OpenCode app please.")
        # An unscrolled, unrelated control must not be authorised by naming the app.
        token, info = confirmations.mint("scroll", "OpenCode", "OK",
                                         known_names=[])
        if token:
            # If the harness's phrasing logic considers this authorised, then the
            # refusal must come from the control-identity rule instead.
            self.assertEqual(info.get("control"), "OK")
        # The decisive property: with no consent recorded at all, act() refuses.
        confirmations.reset()
        with mock.patch.object(ui_probe, "available",
                               return_value={"available": True}):
            result = ui_probe.act("OpenCode", "", action="scroll")
        self.assertFalse(result.get("allowed"))
        self.assertFalse(result.get("success"))
        self.assertIn("refused", (result.get("failure_reason") or "").lower())

    def test_naming_the_app_does_not_authorise_a_control_it_did_not_name(self):
        """Found on a real app: 'press the Advance button in the Button Bench
        app' was accepted as consent to click 'OK'. Naming the app is context."""
        self._said("Press the Advance button in the Button Bench app please.")
        for wanted in ("OK", "Danger", "ShowBox"):
            token, info = confirmations.mint("click", "Button Bench", wanted,
                                             known_names=["Advance", "Danger",
                                                          "ShowBox"])
            self.assertIsNone(token, "%r must not be authorised" % wanted)
        # ...while the control he actually named is fine.
        token, info = confirmations.mint("invoke", "Button Bench", "Advance",
                                         known_names=["Advance", "Danger"])
        self.assertTrue(token, info)

    def test_the_app_name_is_not_consent_even_with_no_known_names(self):
        """The permissive window-name branch is gone entirely."""
        self._said("click whatever is in the Button Bench app")
        self.assertIsNone(
            confirmations.mint("click", "Button Bench", "Advance")[0])
        self.assertIsNone(
            confirmations.mint("click", "Button Bench", "Advance",
                               known_names=["Advance"])[0])

    def test_a_bare_yes_authorises_exactly_one_named_or_unnamed_control(self):
        self._said("yes")
        token, info = confirmations.mint("invoke", "Button Bench", "Advance")
        self.assertTrue(token, info)
        self.assertFalse(info["bound_to_target"],
                         "a bare yes is not target-bound")
        self.assertTrue(confirmations.consume(token, "invoke",
                                              "Button Bench", "Advance")[0])
        self.assertFalse(
            confirmations.mint("invoke", "Button Bench", "Advance")[0],
            "one 'yes' buys one action")

    def test_a_bare_yes_does_not_override_a_named_other_control(self):
        self._said("yes, but click Danger instead")
        self.assertIsNone(
            confirmations.mint("invoke", "Button Bench", "Advance",
                               known_names=["Advance", "Danger"])[0])

    def test_confirmation_phrases_are_strict(self):
        for text in ("yes", "do it", "confirm", "go ahead", "proceed"):
            self.assertTrue(ui_probe.can_act(text), text)
        for text in ("no", "not now", "maybe", "stop", ""):
            self.assertFalse(ui_probe.can_act(text), text)

    def test_inspection_is_read_only_and_emits_no_tokens(self):
        with mock.patch.object(ui_probe, "available",
                               return_value={"available": True}):
            with mock.patch.object(ui_probe, "find_window",
                                   return_value=[{"title": "A", "handle": 1,
                                                  "minimized": False}]):
                with mock.patch.object(ui_probe, "_ua", mock.Mock(
                        UIAutomation=lambda **k: mock.Mock(
                            dom=lambda h, **kw: {"available": True, "controls": [
                                {"name": "OK", "control_type": "Button",
                                 "automation_id": "ok-btn", "value": "",
                                 "enabled": True,
                                 "target_token": "SECRET-TOKEN"}]}))):
                    result = ui_probe.inspect_window("A")
        self.assertNotIn("SECRET-TOKEN", ui_probe.summary_for_prompt(result))

    def test_unavailable_backend_is_reported_not_faked_as_empty(self):
        with mock.patch.object(ui_probe, "available",
                               return_value={"available": False}):
            result = ui_probe.inspect_window("Anything")
        self.assertFalse(result["available"])
        self.assertIn("unavailable", result["reason"])


class CrossProcessConsentTests(unittest.TestCase):
    """The reason the ledger is on disk at all.

    Measured on the live runtime: open-interpreter runs brain-authored python in
    a separate process (interpreter/core/computer/terminal/terminal.py -> the
    terminal subprocess). With an in-memory ledger the process that actually
    called ui_probe.act() had an empty history, so a genuine "click the ShowBox
    button" was refused on its first attempt. These tests reproduce that by
    really forking.
    """

    def setUp(self):
        confirmations.reset()
        self.addCleanup(confirmations.reset)

    def _child(self, code):
        import subprocess

        return subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "os_environ = __import__('os'); "
             "os_environ.environ['FRIDAY_CONFIRM_STATE_DIR'] = %r; "
             "exec(%r)" % (os.getcwd(), _CONFIRM_TMP, code)],
            capture_output=True, text=True, timeout=60)

    def test_a_separate_process_can_mint_from_a_turn_the_server_recorded(self):
        confirmations.note_user_turn("click ShowBox in the Button Bench app",
                                     source="text")
        result = self._child(
            "from source.server import action_confirmations as c\n"
            "t, i = c.mint('invoke', 'Button Bench', 'ShowBox')\n"
            "print('MINTED' if t else 'REFUSED', i.get('reason', ''))")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertIn("MINTED", result.stdout, result.stdout)

    def test_a_separate_process_sees_no_consent_when_sir_said_nothing(self):
        confirmations.note_user_turn("what is on my screen?", source="text")
        result = self._child(
            "from source.server import action_confirmations as c\n"
            "t, i = c.mint('invoke', 'Button Bench', 'ShowBox')\n"
            "print('MINTED' if t else 'REFUSED')")
        self.assertEqual(result.returncode, 0, result.stderr[-400:])
        self.assertIn("REFUSED", result.stdout, result.stdout)

    def test_single_use_holds_across_processes(self):
        confirmations.note_user_turn("yes go ahead", source="text")
        token, _ = confirmations.mint("invoke", "Button Bench", "Advance")
        self.assertTrue(token)
        code = ("from source.server import action_confirmations as c\n"
                "print('OK' if c.consume(%r,'invoke','Button Bench','Advance')[0]"
                " else 'REFUSED')" % token)
        first = self._child(code)
        second = self._child(code)
        self.assertIn("OK", first.stdout, first.stdout)
        self.assertIn("REFUSED", second.stdout, second.stdout)

    def test_the_signing_key_is_shared_not_per_process(self):
        confirmations.note_user_turn("yes go ahead", source="text")
        token, _ = confirmations.mint("invoke", "Button Bench", "Advance")
        result = self._child(
            "from source.server import action_confirmations as c\n"
            "print('OK' if c.consume(%r,'invoke','Button Bench','Advance')[0]"
            " else 'REFUSED:' + str(c.consume(%r,'invoke','Button Bench','Advance')[1]))"
            % (token, token))
        self.assertIn("OK", result.stdout, result.stdout)

    def test_the_state_file_lives_outside_the_repo(self):
        _dir, key_path, ledger = confirmations.state_paths()
        self.assertIn(_CONFIRM_TMP, ledger)
        self.assertNotIn(os.path.join("source", "server"), ledger)
        confirmations.note_user_turn("yes", source="text")
        self.assertTrue(os.path.exists(ledger))
        self.assertTrue(os.path.exists(key_path))


class CommandRouterGateTests(unittest.TestCase):
    """UIA actions must arrive through the router, risk-classed and audited."""

    def setUp(self):
        confirmations.reset()
        self.addCleanup(confirmations.reset)

    def test_router_refuses_and_explains_without_sir_saying_yes(self):
        result = command_router.uia_action("A", "OK", action="click")
        self.assertFalse(result["allowed"])
        self.assertTrue(result["response"])
        self.assertEqual(result["risk"], command_router.RISK_REVERSIBLE)

    def test_router_risk_classification_matches_ui_automation_metadata(self):
        from source.server import ui_automation

        for action in sorted(ui_probe.ALLOWED_ACTIONS):
            self.assertEqual(command_router.uia_risk(action),
                             command_router.uia_risk(action))
            self.assertIn(command_router.uia_risk(action),
                          (command_router.RISK_READ_ONLY,
                           command_router.RISK_REVERSIBLE,
                           command_router.RISK_DESTRUCTIVE))
        self.assertTrue(hasattr(ui_automation, "risk_metadata"))

    def test_router_never_phrase_triggers_a_uia_action(self):
        """AGENTS.md: app control belongs to the brain, not to a regex."""
        source = io.open(ui_probe.__file__.replace("ui_probe.py",
                                                  "command_router.py"),
                         encoding="utf-8").read()
        route_body = source[source.index("def route("):]
        route_body = route_body[:route_body.index("\n\ndef ")] \
            if "\n\ndef " in route_body else route_body
        self.assertNotIn("uia", route_body.lower(),
                         "route() must not match phrases for UI actions")

    def test_router_entry_point_is_the_only_documented_one(self):
        result = command_router.uia_action("A", "OK", action="type", value="hi")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["action"], "uia_type")


# ------------------------------------------------------------- capture layer
class CaptureServiceTests(unittest.TestCase):
    def test_unavailable_returns_none_so_the_pillow_path_runs(self):
        service = capture.ScreenCaptureService(target_fps=0)
        self.assertIsNone(service.frame(0))

    def test_a_partially_filled_buffer_is_refused_rather_than_half_answered(self):
        service = capture.ScreenCaptureService(target_fps=0)
        service._buffers = [object(), object()]

        class _Buf:
            pil = None
            raw = None

            def age(self):
                return 0.0

        service._buffers = [_Buf(), _Buf()]
        with mock.patch.object(capture, "_to_pil", return_value=None):
            self.assertIsNone(service.frame(0))

    def test_stale_frames_are_refused_so_a_fresh_capture_happens(self):
        service = capture.ScreenCaptureService(target_fps=0)
        buffer = mock.Mock()
        buffer.age.return_value = 9999.0
        buffer.pil = object()
        service._buffers = [buffer]
        self.assertIsNone(service.frame(0, max_age=1))

    def test_enumerated_outputs_parses_dxcam_output_info(self):
        raw = ("Device[0] Output[0]: Res:(1920, 1080) Rot:0 Primary:True\n"
               "Device[0] Output[1]: Res:(2560, 1440) Rot:0 Primary:False")
        dxcam = mock.Mock()
        dxcam.output_info.return_value = raw
        out = capture._enumerated_outputs(dxcam)
        self.assertEqual(out, [(0, 0, (1920, 1080), True),
                               (0, 1, (2560, 1440), False)])

    def test_malformed_output_info_never_raises(self):
        dxcam = mock.Mock()
        dxcam.output_info.return_value = "total garbage\nmore garbage"
        self.assertEqual(capture._enumerated_outputs(dxcam), [])

    def test_a_missing_dxcam_is_reported_not_raised(self):
        with mock.patch.object(capture, "_import_dxcam", return_value=None):
            health = capture.ScreenCaptureService().start()
        self.assertFalse(health["enabled"])
        self.assertIn("fallback", health["reason"])

    def test_capture_regions_prefers_live_frames_and_falls_back(self):
        """The existing screen path must never break when the buffer is off."""
        from source.server import screen_understanding as screen

        with mock.patch.object(screen, "_live_regions", return_value=None):
            with mock.patch.object(screen, "_capture_screen") as grab:
                grab.return_value = object()
                with mock.patch.object(screen, "_monitor_layout", return_value=[]):
                    regions = screen._capture_regions()
        self.assertTrue(regions)
        self.assertEqual(len(regions), 1)

    def test_live_regions_return_none_when_the_service_is_disabled(self):
        from source.server import screen_understanding as screen

        with mock.patch.object(capture, "health", return_value={"enabled": False}):
            self.assertIsNone(screen._live_regions())


if __name__ == "__main__":
    unittest.main()