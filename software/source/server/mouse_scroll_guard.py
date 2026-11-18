"""ROUND ZB-L3 L3-3: stop `computer.mouse.scroll` bypassing the scroll architecture.

THE BYPASS THIS CLOSES
-----------------------
Round Z built `ui_scroll.py` on OBSERVE -> LOCATE -> ACT -> VERIFY, with the
semantic path going through the EXISTING HMAC single-use consent gate and the
wheel fallback bounded by three hard rules: the target must be the real
foreground window, the cursor must be moved INSIDE the target rect, and success
is only reported when the viewport demonstrably changed.

`ui_scroll.py:43` deliberately gave the wheel path no consent gate of its own.
That decision is fine INSIDE `scroll_window` - the bounds are checked there.
The problem is that `computer.mouse.scroll(clicks)` was never routed through
it at all. On 2026-10-07, live, against a healthy Modal provider, FRIDAY used
exactly that bypass on OpenCode: she hand-rolled a digest before/after and
called `computer.mouse.scroll(-5)`. The viewport moved (`pre: 6571c867 ->
post: 9ed7f552`) with no consent check, no foreground proof and no
architectural verification, and the turn then died at the tool cap having told
Sir nothing.

A refusal alone was rejected as the fix: a lower-level route to the same OS
call would still exist. So this INTERPOSES. `computer.mouse.scroll(...)` now
routes through the real architecture, and the same rules apply.

WHAT IS PRESERVED (all of it)
-----------------------------
  * foreground mismatch          -> refused before any input
  * missing authorisation        -> refused (ui_probe.act mints from a real
                                    recorded user turn, or denies)
  * stale / reused authorisation -> refused (single-use token consumed once)
  * ambiguity                    -> refused (ui_scroll's own resolver)
  * no observable state change   -> reported as failure, not success
  * percent-based verification   -> untouched
  * terminal refusal semantics   -> a refused SEMANTIC scroll is never
                                    converted into an unchecked wheel input

SAFE FAILURE
------------
The guard never raises. A caller that scrolls without a resolvable foreground
window, or in an environment where the FRIDAY package cannot be imported, gets
a printed refusal and NO OS input at all. `ui_scroll.scroll_window` reaches
pyautogui directly, so routing through it cannot recurse back into this guard.
"""

from __future__ import annotations

# Wheel notches -> the amount ui_scroll accepts. ui_scroll normalises and
# clamps to 1..25; clamping here too keeps the call honest rather than letting
# an absurd value reach the architecture.
MIN_NOTCHES = 1
MAX_NOTCHES = 25


def _clamp(clicks):
    try:
        value = int(clicks)
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    amount = abs(value)
    return max(MIN_NOTCHES, min(MAX_NOTCHES, amount))


def _refuse(reason, extra=""):
    """Refuse without raising, and without touching the OS.

    Raising here would surface as an opaque crash inside model-authored code
    whose only mistake was calling a convenience wrapper; a printed refusal is
    both safer and readable in the tool output the model actually sees.
    """
    message = "[scroll] REFUSED: %s" % reason
    if extra:
        message += " (%s)" % extra
    try:
        print(message, flush=True)
    except Exception:
        pass
    return None


def scroll_via_verified_path(clicks, *, computer=None, actor="kernel"):
    """Route a raw wheel scroll through the verified architecture.

    Returns the `ui_scroll` result dict on success, or None on any refusal.
    """
    amount = _clamp(clicks)
    if amount is None:
        return _refuse("scroll needs a non-zero notch count",
                       "got %r" % (clicks,))

    try:
        from . import ui_scroll
    except Exception as import_error:
        # Fail SAFE. Without the architecture there is no way to verify a
        # scroll, so no scroll happens - this is the whole point of L3-3.
        return _refuse(
            "the verified scroll path is unavailable, so no wheel event was "
            "sent", type(import_error).__name__)

    direction = "up" if int(clicks) > 0 else "down"

    # The foreground window is the only target that can be identified without
    # Sir naming one, and it is the same window a human would be scrolling.
    try:
        handle = ui_scroll.foreground_handle()
    except Exception as foreground_error:
        return _refuse("could not read the foreground window",
                       type(foreground_error).__name__)
    if not handle:
        return _refuse("foreground window unknown, so there is nothing I can "
                       "prove I am scrolling")

    try:
        result = ui_scroll.scroll_window(
            {"handle": int(handle)},
            direction=direction,
            amount=amount,
            # "auto" tries the consent-gated semantic path first and falls back
            # to the bounded wheel path. It deliberately does NOT do the reverse:
            # a refused semantic act is terminal inside ui_scroll (Round Z S17),
            # so a denied consent can never become an unchecked wheel input.
            mechanism="auto",
            actor=actor,
        )
    except Exception as scroll_error:
        return _refuse("the verified scroll path raised, so no wheel event "
                       "was sent", "%s: %s" % (type(scroll_error).__name__,
                                              str(scroll_error)[:120]))

    if not isinstance(result, dict):
        return _refuse("the verified scroll path returned nothing usable")

    if result.get("success"):
        try:
            print(
                "[scroll] verified %s via %s (%s) - window %r"
                % (direction, result.get("mechanism"),
                   result.get("evidence"), result.get("window")),
                flush=True,
            )
        except Exception:
            pass
        return result

    reason = result.get("failure_reason") or "the viewport did not change"
    return _refuse(reason, "mechanism=%s" % (result.get("mechanism") or "none"))


def install_mouse_scroll_guard(computer):
    """Patch `computer.mouse.scroll` on a live computer object. Idempotent."""
    mouse = getattr(computer, "mouse", None)
    if mouse is None:
        return False
    if getattr(mouse, "_friday_scroll_verified", False) is True:
        return False
    try:
        original = mouse.scroll
    except AttributeError:
        return False

    def scroll(clicks):
        # Never fall back to `original`: that raw wheel call is the bypass.
        # A refusal is the correct outcome when the architecture declines.
        return scroll_via_verified_path(clicks, computer=computer)

    scroll.__doc__ = (
        "FRIDAY (ZB-L3): routed through ui_scroll's verified architecture.\n\n"
        "Raw pyautogui scrolling is no longer reachable from here. Use\n"
        "source.server.ui_scroll.scroll_window({...}, direction=..., amount=...)\n"
        "directly, or this wrapper, which targets the foreground window."
    )
    scroll._friday_original = original
    mouse.scroll = scroll
    try:
        mouse._friday_scroll_verified = True
    except Exception:
        pass
    return True


SITE_PACKAGES_SHIM = '''

# ---------------------------------------------------------------------------
# FRIDAY patch (ROUND ZB-L3 L3-3): computer.mouse.scroll must NOT bypass the
# verified scroll architecture. Re-applied at boot by
# source/server/display_patch.py::_ensure_mouse_scroll_guard if this marker is
# missing (wheel reinstall, pip upgrade, fresh .venv).
#
# Fails SAFE: if the FRIDAY package cannot be imported, this REFUSES and sends
# no wheel event at all, rather than falling back to a raw scroll.
# ---------------------------------------------------------------------------
def _friday_verified_scroll(self, clicks):
    try:
        from source.server import mouse_scroll_guard as _friday_guard
    except Exception as _friday_import_error:
        print("[scroll] REFUSED: verified scroll path unavailable (%s); no "
              "wheel event was sent. Use "
              "source.server.ui_scroll.scroll_window instead."
              % type(_friday_import_error).__name__, flush=True)
        return None
    return _friday_guard.scroll_via_verified_path(clicks, computer=self.computer)


Mouse.scroll = _friday_verified_scroll
Mouse.scroll.__doc__ = (
    "FRIDAY (ZB-L3): routed through ui_scroll's verified architecture."
)
'''
