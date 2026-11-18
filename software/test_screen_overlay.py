"""Display-only on-screen annotation overlay (ROUND ZB-OV).

Proves the hard properties without anyone watching the screen:

* it can never take focus or swallow a click (WS_EX_NOACTIVATE / WS_EX_TRANSPARENT);
* it refuses to draw without Sir's own words behind it, and there is no
  `confirmed=` bypass anywhere in the signature;
* it clears itself on TTL, focus loss and target change;
* it is deliberately NOT added to `ui_probe.ALLOWED_ACTIONS`, so the existing
  mouse-moving action boundary is untouched;
* a failed blend is reported as a refusal, never as a claimed success.

The render tests use a real layered window (this is a Windows machine) but a
MOCKED target resolver, so no application is opened, focused or clicked.
"""

import os
import sys
import time

sys.path.insert(0, os.getcwd())

import pytest

from source.server import action_confirmations as confirmations
from source.server import screen_overlay as overlay
from source.server import ui_probe

# Captured before any fixture replaces it, so one test can still exercise the
# real hook (it must never raise, whatever the audit log is doing).
_REAL_AUDIT = overlay._audit


@pytest.fixture(autouse=True)
def _audit_events(monkeypatch):
    """Record audit calls instead of writing into FRIDAY's live audit log.

    Test events would otherwise land in ~/.friday/audit next to real ones and
    pollute a later incident read with `operation=overlay_annotate`.
    """
    events = []
    monkeypatch.setattr(overlay, "_audit", lambda **kw: events.append(kw))
    yield events
    overlay.reset()


@pytest.fixture
def confirm_state(tmp_path, monkeypatch):
    """A private consent ledger per test.

    Without this the module-level `FRIDAY_CONFIRM_STATE_DIR` set by another test
    module would be shared, and a stray "yes" over there could authorise an
    annotation in here - a false pass in exactly the test that must refuse.
    """
    monkeypatch.setenv("FRIDAY_CONFIRM_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def target_env(monkeypatch):
    """The desktop window stands in for 'the app Sir is pointing at'.

    Real HWND and real rectangle (so bounds checking and the watcher run their
    real code), but nothing is opened, focused or clicked.
    """
    import win32gui

    hwnd = win32gui.GetDesktopWindow()
    rect = tuple(int(v) for v in win32gui.GetWindowRect(hwnd))
    inner = (rect[0] + 10, rect[1] + 10, rect[0] + 110, rect[1] + 110)

    def _resolve(query):
        if not query:
            return None, "no open window matched %r" % (query,), None
        return hwnd, None, rect

    monkeypatch.setattr(overlay, "_resolve_target", _resolve)
    # Focus is sampled when we draw and re-checked by the watcher. Pin it so a
    # background focus change cannot clear the annotation mid-assertion.
    monkeypatch.setattr(overlay, "_foreground", lambda: 4242)
    return {"hwnd": hwnd, "rect": rect, "inner": inner}


# --------------------------------------------------------------- pure helpers
class TestGeometryHelpers:
    def test_ttl_is_clamped_both_ways(self):
        assert overlay._clamp_ttl(0) == 1.0
        assert overlay._clamp_ttl(9999) == overlay.MAX_TTL
        assert overlay._clamp_ttl("nonsense") == overlay.DEFAULT_TTL
        assert overlay._clamp_ttl(None) == overlay.DEFAULT_TTL

    def test_rect_inside_accepts_and_rejects(self):
        assert overlay._rect_inside((10, 10, 50, 50), (0, 0, 100, 100))
        assert not overlay._rect_inside((0, 0, 500, 500), (0, 0, 100, 100))
        assert not overlay._rect_inside(None, (0, 0, 100, 100))

    def test_bounds_match_tolerates_a_pixel_but_not_a_move(self):
        assert overlay._bounds_match((0, 0, 100, 100), (0, 2, 100, 102))
        assert not overlay._bounds_match((0, 0, 100, 100), (0, 9, 100, 109))
        assert not overlay._bounds_match(None, (0, 0, 1, 1))


class TestRendering:
    @pytest.mark.parametrize("shape", ["rect", "arrow", "label"])
    def test_every_shape_renders_with_a_clear_background(self, shape):
        image = overlay._draw(shape, 80, 60, "e5", (255, 0, 0), False)
        assert image.mode == "RGBA"
        assert image.size == (80, 60)
        alpha = image.getchannel("A")
        assert max(alpha.getdata()) >= 200, "nothing was drawn"
        if shape != "label":
            # A label floods its own background; rect/arrow must leave the
            # corner transparent or the overlay would be an opaque slab.
            assert alpha.getpixel((0, 0)) == 0

    def test_a_filled_rect_still_has_alpha(self):
        image = overlay._draw("rect", 40, 40, "", (0, 128, 255), True)
        assert max(image.getchannel("A").getdata()) >= overlay.FILL_ALPHA

    def test_an_unknown_shape_is_an_error_not_a_blank_frame(self):
        with pytest.raises(ValueError):
            overlay._draw("sparkle", 10, 10, "", (1, 2, 3), False)

    def test_an_oversized_label_does_not_break_the_draw(self):
        image = overlay._draw("label", 300, 60, "y" * 5000, (255, 0, 0), False)
        assert image.size == (300, 60)


# ------------------------------------------------------- validation / refusal
class TestRefusals:
    def test_an_unsupported_shape_is_refused(self):
        result = overlay.annotate("Chess", shape="sparkle", rect=(1, 1, 50, 50))
        assert result["ok"] is False
        assert "shape" in result["reason"]

    def test_a_degenerate_rect_is_refused(self):
        result = overlay.annotate("Chess", shape="rect", rect=(10, 10, 10, 10))
        assert result["ok"] is False
        assert "positive" in result["reason"]

    def test_an_unknown_window_reports_why_and_offers_alternatives(self):
        result = overlay.annotate("zzz-definitely-no-such-window",
                                  shape="rect", rect=(1, 1, 50, 50))
        assert result["ok"] is False
        assert "no open window" in result["reason"]
        assert isinstance(result["candidates"], list)

    @pytest.mark.parametrize("kwargs", [
        {"shape": None, "rect": None},
        {"shape": "rect", "rect": "not a tuple"},
        {"shape": "rect", "rect": (1, 2)},
        {"shape": "arrow", "rect": None, "points": "junk"},
        {"shape": "label", "rect": (0, 0, -5, -5)},
    ])
    def test_garbage_arguments_never_raise(self, kwargs):
        result = overlay.annotate("Chess", **kwargs)
        assert result["ok"] is False
        assert isinstance(result["reason"], str) and result["reason"]
        assert "status" in result


# ------------------------------------------------------------------- consent
class TestConsent:
    def test_without_his_words_it_refuses(self, target_env, confirm_state):
        result = overlay.annotate("Chess", shape="rect",
                                  rect=target_env["inner"], label="e5")
        assert result["ok"] is False, "drew without Sir saying anything"
        assert "refused" in result["reason"]
        assert overlay.status()["active"] is False

    def test_with_his_words_it_draws(self, target_env, confirm_state):
        confirmations.note_user_turn("please highlight e5", source="voice")
        result = overlay.annotate("Chess", shape="rect",
                                  rect=target_env["inner"], label="e5")
        assert result["ok"] is True, result
        assert overlay.status()["active"] is True

    def test_naming_a_different_target_is_a_refusal(self, target_env,
                                                    confirm_state):
        confirmations.note_user_turn("please highlight e4", source="voice")
        result = overlay.annotate("Chess", shape="rect",
                                  rect=target_env["inner"], label="e5",
                                  known_names=("e4", "e5"))
        assert result["ok"] is False, "consent for e4 was spent on e5"

    def test_one_permission_covers_the_follow_up_draws(self, target_env,
                                                       confirm_state):
        confirmations.note_user_turn("highlight e5", source="voice")
        first = overlay.annotate("Chess", shape="rect",
                                 rect=target_env["inner"], label="e5")
        second = overlay.annotate("Chess", shape="arrow",
                                  rect=target_env["inner"], label="e5")
        assert first["ok"] and second["ok"], (first, second)
        assert overlay.status()["stats"]["drawn"] == 2

    def test_the_session_ends_when_it_expires(self, target_env, confirm_state):
        confirmations.note_user_turn("highlight e5", source="voice")
        assert overlay.annotate("Chess", shape="rect",
                                rect=target_env["inner"], label="e5")["ok"]
        # Run the session clock out; the next draw must ask again.
        overlay._session["deadline"] = time.monotonic() - 1
        result = overlay.annotate("Chess", shape="rect",
                                  rect=target_env["inner"], label="e5")
        assert result["ok"] is False
        assert "refused" in result["reason"]

    def test_the_signature_offers_no_confirmed_bypass(self):
        import inspect

        assert "confirmed" not in inspect.signature(overlay.annotate).parameters

    def test_a_failed_blend_is_a_refusal_not_a_success(self, target_env,
                                                       confirm_state,
                                                       monkeypatch):
        confirmations.note_user_turn("highlight e5", source="voice")
        monkeypatch.setattr(overlay, "_render",
                            lambda *a, **k: "render failed: Fake")
        result = overlay.annotate("Chess", shape="rect",
                                  rect=target_env["inner"], label="e5")
        assert result["ok"] is False
        assert "render failed" in result["reason"]


# --------------------------------------------------------------- lifecycle
class TestLifecycle:
    def test_clear_stops_showing(self, target_env, confirm_state):
        confirmations.note_user_turn("highlight e5", source="voice")
        overlay.annotate("Chess", shape="rect",
                         rect=target_env["inner"], label="e5")
        assert overlay.status()["active"] is True

        outcome = overlay.clear("test")
        assert outcome["cleared"] is True
        assert overlay.status()["active"] is False

    @pytest.mark.parametrize("observed", [None, False, True])
    def test_status_reports_shapes_and_capture_exclusion(self, monkeypatch, observed):
        monkeypatch.setitem(overlay._window, "capture_excluded", observed)
        status = overlay.status()
        assert status["active"] is False
        assert status["shapes"] == list(overlay.SHAPES)
        assert status["excluded_from_capture"] is observed
        assert set(status["stats"]) == {"drawn", "cleared", "refused",
                                        "superseded"}

    def test_every_draw_and_clear_is_audited(self, target_env, confirm_state,
                                             _audit_events):
        confirmations.note_user_turn("highlight e5", source="voice")
        overlay.annotate("Chess", shape="rect",
                         rect=target_env["inner"], label="e5")
        overlay.clear("done")
        operations = [event.get("operation") for event in _audit_events]
        assert "overlay_annotate" in operations
        assert "overlay_clear" in operations
        annotate_event = next(e for e in _audit_events
                              if e.get("operation") == "overlay_annotate")
        assert annotate_event.get("allowed") is True
        assert annotate_event.get("risk") == "reversible"

    def test_the_real_audit_hook_never_raises(self, monkeypatch, tmp_path):
        from source.server import audit

        monkeypatch.setenv("FRIDAY_AUDIT_DIR", str(tmp_path))
        monkeypatch.setattr(audit, "_log", None)   # fresh singleton, temp dir
        monkeypatch.setattr(overlay, "_audit", _REAL_AUDIT)
        # Whatever happens inside, drawing must not be blocked by logging.
        overlay._audit(operation="overlay_annotate", risk="reversible",
                       allowed=True, outcome="ok", target="Chess")
        assert list(tmp_path.glob("friday-audit-*.jsonl")), "nothing was logged"
        _REAL_AUDIT(operation="overlay_clear", risk="reversible",
                    allowed=True, outcome="ok", reason="self-test")


class TestWatcher:
    """`_watch_once` is the whole expiry policy, split out so it is testable."""

    @staticmethod
    def _annotation(target_env, **overrides):
        annotation = {
            "query": "Chess", "shape": "rect",
            "rect": list(target_env["inner"]), "label": "e5",
            "target_handle": target_env["hwnd"],
            "target_rect": list(target_env["rect"]),
            "focus_at_start": 4242, "watch_foreground": True,
            "created_at": time.monotonic() - 99,
            "deadline": time.monotonic() - 1,
        }
        annotation.update(overrides)
        return annotation

    def test_no_annotation_means_nothing_to_do(self, target_env):
        overlay.reset()
        assert overlay._watch_once() is None

    def test_a_stale_annotation_expires(self, target_env, monkeypatch):
        monkeypatch.setattr(overlay, "_active",
                            self._annotation(target_env))
        outcome = overlay._watch_once()
        assert outcome is not None and outcome["reason"] == "ttl_expired"
        assert overlay.status()["active"] is False

    def test_focus_loss_clears_the_drawing(self, target_env, monkeypatch):
        monkeypatch.setattr(overlay, "_foreground", lambda: 9999)
        monkeypatch.setattr(
            overlay, "_active",
            self._annotation(target_env, deadline=time.monotonic() + 30))
        outcome = overlay._watch_once()
        assert outcome is not None and outcome["reason"] == "focus_lost"

    def test_a_vanished_target_clears_the_drawing(self, target_env,
                                                  monkeypatch):
        monkeypatch.setattr(overlay, "_window_rect", lambda h: None)
        monkeypatch.setattr(
            overlay, "_active",
            self._annotation(target_env, deadline=time.monotonic() + 30))
        outcome = overlay._watch_once()
        assert outcome is not None and outcome["reason"] == "target_gone"

    def test_a_moved_target_clears_the_drawing(self, target_env, monkeypatch):
        monkeypatch.setattr(overlay, "_window_rect", lambda h: (0, 0, 50, 50))
        monkeypatch.setattr(
            overlay, "_active",
            self._annotation(target_env, deadline=time.monotonic() + 30))
        outcome = overlay._watch_once()
        assert outcome is not None and outcome["reason"] == "target_changed"

    def test_a_live_annotation_is_left_alone(self, target_env, monkeypatch):
        monkeypatch.setattr(
            overlay, "_active",
            self._annotation(target_env, deadline=time.monotonic() + 30))
        assert overlay._watch_once() is None
        assert overlay.status()["active"] is True


# ------------------------------------------------------- the real window
class TestRealWindow:
    def test_it_draws_click_through_and_never_takes_focus(self, target_env,
                                                          confirm_state):
        import win32con
        import win32gui

        foreground_before = win32gui.GetForegroundWindow()
        confirmations.note_user_turn("highlight e5", source="voice")

        result = overlay.annotate("Chess", shape="rect",
                                  rect=target_env["inner"], label="e5")
        assert result["ok"] is True, result

        hwnd = overlay._window["hwnd"]
        assert hwnd and win32gui.IsWindow(hwnd), "no overlay window"

        exstyle = win32gui.GetWindowLong(hwnd, -20)
        assert exstyle & win32con.WS_EX_TRANSPARENT, "would swallow a click"
        assert exstyle & win32con.WS_EX_NOACTIVATE, "would steal focus"
        assert exstyle & win32con.WS_EX_TOOLWINDOW, "would show in the taskbar"

        assert win32gui.IsWindowVisible(hwnd), "never became visible"
        # The single most important property: showing a box must not move focus.
        assert win32gui.GetForegroundWindow() == foreground_before, \
            "the overlay stole focus"

        with overlay._PerMonitorDPI():
            assert win32gui.GetWindowRect(hwnd) == tuple(target_env["inner"]), \
                "drawn in the wrong place"

        overlay.clear("done")
        assert not win32gui.IsWindowVisible(hwnd), "did not hide again"

    def test_the_window_is_hidden_again_after_reset(self, target_env,
                                                    confirm_state):
        import win32gui

        confirmations.note_user_turn("highlight e5", source="voice")
        overlay.annotate("Chess", shape="rect",
                         rect=target_env["inner"], label="e5")
        hwnd = overlay._window["hwnd"]
        assert win32gui.IsWindowVisible(hwnd)

        overlay.reset()
        assert not win32gui.IsWindowVisible(hwnd)


class TestHideOnTheOwningThread:
    """MEASURED: ShowWindow(SW_HIDE) from a thread other than the window's owner
    does nothing here, so the TTL watcher would clear the state and leave the box
    stuck on screen. The hide must travel back to the owning thread."""

    def test_a_hide_on_the_owning_thread_stays_inline(self, monkeypatch):
        import threading

        import win32con
        import win32gui

        overlay._ensure_window()          # we become the owner thread
        hwnd = overlay._window["hwnd"]
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)   # _ensure_window only creates
        assert win32gui.IsWindowVisible(hwnd)

        def _must_not_run(fn):            # pragma: no cover - guard only
            pytest.fail("owner-thread hide must not be marshalled")

        monkeypatch.setattr(overlay, "_window_executor", _must_not_run)
        overlay._hide_window()
        assert not win32gui.IsWindowVisible(hwnd), "inline hide did not work"
        assert overlay._window_owner_tid == threading.get_ident()

    def test_a_hide_from_another_thread_goes_through_the_executor(self,
                                                                  monkeypatch):
        import win32con
        import win32gui

        overlay._ensure_window()
        hwnd = overlay._window["hwnd"]
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        assert win32gui.IsWindowVisible(hwnd)

        marshalled = []
        monkeypatch.setattr(overlay, "_window_executor",
                            lambda fn: marshalled.append(fn))
        # Pretend the window lives on some other thread, as it does in the
        # server's TTL watcher.
        monkeypatch.setattr(overlay, "_window_owner_tid", 1)

        overlay._hide_window()
        assert len(marshalled) == 1, "hide did not travel to the owning thread"
        assert callable(marshalled[0])
        # Nothing ran yet - the box must still be up until the owner hides it.
        assert win32gui.IsWindowVisible(hwnd), "hide ran on the wrong thread"


# -------------------------------------------------------------- the boundary
class TestBoundaryUnchanged:
    def test_annotate_is_not_part_of_the_mouse_moving_action_set(self):
        assert "annotate" not in ui_probe.ALLOWED_ACTIONS

    def test_annotate_has_its_own_verb(self):
        assert "annotate" in confirmations._VERBS
        assert "highlight" in confirmations._VERBS["annotate"]

    def test_the_module_exports_only_its_documented_surface(self):
        assert set(overlay.__all__) == {"annotate", "clear", "status",
                                        "is_active", "reset",
                                        "set_window_executor", "SHAPES",
                                        "DEFAULT_TTL", "SESSION_TTL"}
