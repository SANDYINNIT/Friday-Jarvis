"""UI Automation for FRIDAY: read-only inspection first, guarded actions second.

Layer 2 of the screen-awareness stack (window state is layer 1, pixels are
layer 3). It answers questions about an application's *interface* rather than
about its pixels, which is both cheaper and more reliable than a vision model
when the app exposes a real accessibility tree.

DESIGN RULES (Sir's directive, 2026-10-02)
------------------------------------------
1. Read-only inspection is the NORMAL first step. Acting is a separate,
   explicit decision FRIDAY makes, never a side effect of looking.
2. Every action goes through the existing risk/confirmation machinery and is
   logged with `audit.log_action` exactly like a deterministic router action.
   There is deliberately NO unrestricted "UIA can click anything" path.
3. Prefer an accessibility PATTERN (`invoke`) over synthetic mouse input. The
   pattern is immune to UI privilege isolation, which silently swallows injected
   input into a higher-integrity window, and it does not move the real cursor.

WHAT UIA IS GOOD FOR (measured on this machine)
-----------------------------------------------
Calculator exposed 53 controls / 36 named buttons in 0.24s. Invoking a button
took ~0.42s and was verified by the window's display actually changing.

WHAT UIA CANNOT SEE
-------------------
Canvas-drawn content: games, most video players, terminal grids, and Discord's
message list are only partly exposed. That is why this is a LAYER, not a
replacement - OCR and the vision chain still back it up.
"""

from __future__ import annotations

import os
import re
import time

from .file_logger import friday_logger as _friday_logger
from . import ui_automation as _ua
from . import windows_control as _wc

_log = _friday_logger()

# Bounds. UI Automation is not free, and an unbounded tree walk on a large app
# is exactly how a turn turns into a 20-second freeze. The existing
# ui_automation module already caps these; these are the probe-level caps.
MAX_WINDOWS = int(os.getenv("FRIDAY_UIA_WINDOWS", "12"))
MAX_CONTROLS = int(os.getenv("FRIDAY_UIA_CONTROLS", "120"))
MAX_TEXT = int(os.getenv("FRIDAY_UIA_TEXT", "160"))

# Only these verbs may be used through this module. Anything else is refused
# before a window is even resolved - this list IS the boundary.
ALLOWED_ACTIONS = frozenset({"click", "invoke", "type", "scroll"})


def _backend_available():
    try:
        return _ua._DEFAULT._backend() is not None
    except Exception:
        return False


def available():
    return {"available": _backend_available(), "backend": "pywinauto/uia"}


# --------------------------------------------------------------------------
# READ-ONLY inspection (the normal path)
# --------------------------------------------------------------------------
def find_window(query, limit=8):
    """Resolve an app name to an open window, reusing the existing matcher.

    Deliberately NOT a second fuzzy matcher: windows_control already resolves
    app names for the deterministic router, and two matchers would drift.
    """
    try:
        return _wc.find_windows(query, limit=limit)
    except Exception as error:
        _log.warning("uia window lookup failed: %s", type(error).__name__)
        return []


def inspect_window(query, *, limit=MAX_WINDOWS, control_limit=MAX_CONTROLS):
    """Read an app's accessibility tree without touching anything.

    Returns a bounded, prompt-friendly summary: window facts plus the visible
    controls, with values truncated. `available=False` means UIA could not be
    used at all, which the caller must treat as "unknown", never as "empty".
    """
    if not available().get("available"):
        return {"available": False, "reason": "UI Automation backend unavailable"}
    candidates = find_window(query)
    if not candidates:
        return {"available": True, "found": False,
                "reason": "no open window matched that name"}
    best = candidates[0]
    probe = _ua.UIAutomation(window_limit=MAX_WINDOWS, control_limit=control_limit)
    try:
        snapshot = probe.dom(best["handle"])
    except Exception as error:
        return {"available": False,
                "reason": "dom read failed: %s" % type(error).__name__}
    if not snapshot.get("available"):
        return {"available": False,
                "reason": snapshot.get("failure_reason") or "dom unavailable"}
    controls = []
    for control in snapshot.get("controls") or []:
        name = str(control.get("name") or "")[:MAX_TEXT]
        value = str(control.get("value") or "")[:MAX_TEXT]
        if not name and not value:
            continue
        controls.append({
            "name": name,
            "control_type": control.get("control_type") or "",
            "automation_id": control.get("automation_id") or "",
            "value": value,
            "enabled": control.get("enabled"),
            # ROUND ZB-L1 (2026-10-07): the rebuilt record dropped every
            # scroll fact, so a caller inspecting a window through
            # `inspect_window` could not tell a scrollable container from a
            # static one. It stayed hidden while `_any_window()` happened
            # to return a window whose probe fell through to `dom()`; once a
            # Chromium window with a non-empty title came first, the missing
            # keys surfaced. Four bounded scalars, already computed by
            # ui_automation - no extra UIA cost and no tree dump.
            "can_scroll": bool(control.get("can_scroll")),
            "can_scroll_v": bool(control.get("can_scroll_v")),
            "can_scroll_h": bool(control.get("can_scroll_h")),
            "scroll_state": control.get("scroll_state") or "none",
            "scroll_percent_v": control.get("scroll_percent_v"),
            })
        if len(controls) >= control_limit:
            break
    return {
        "available": True,
        "found": True,
        "window": {
            "title": str(best.get("title") or "")[:MAX_TEXT],
            "process": best.get("process") or "",
            "minimized": bool(best.get("minimized")),
            "handle": best.get("handle"),
        },
        "control_count": len(controls),
        "controls": controls,
        "probe": probe,          # NOT for prompt use; holds the token table
        "ambiguous": len(candidates) > 1,
        "other_candidates": [c.get("title") or "" for c in candidates[1:4]],
    }


def _resolve_one(query):
    """Find exactly one window for a read, or return an honest failure dict."""
    if not available().get("available"):
        return None, {"available": False,
                      "reason": "UI Automation backend unavailable"}
    candidates = find_window(query)
    if not candidates:
        return None, {"available": True, "found": False,
                      "reason": "no open window matched that name"}
    return candidates[0], None


def read_window_text(query, *, offset=0, limit=_ua.READ_CHUNK):
    """Read a window's own text in bounded chunks - no screenshot, no scrolling.

    This is the "read long app output" path. It pulls the accessibility text
    (TextPattern) instead of scrolling a viewport and re-shooting, so output
    that is off-screen is still readable and nothing moves.

    Returns `text`, `offset`, `total`, `complete` (keep calling with
    `offset+length` until True), and `mechanism`:
      * `uia_textpattern` / `uia_text_nodes` - usable;
      * `none` - this window exposes NO accessible text; use screenshot/OCR/
        vision for it. `fallback` says so explicitly, because an empty `text`
        from this function must never be mistaken for an empty window.

    `captured_at` / `age_seconds` are the staleness half of the fusion: the
    caller can tell how old the text is before acting on it, and the window
    identity (handle/title/process) stops a later action from being applied to
    a different window that happens to have the same name now.
    """
    window, failure = _resolve_one(query)
    if failure is not None:
        return failure
    probe = _ua.UIAutomation(window_limit=MAX_WINDOWS, control_limit=MAX_CONTROLS)
    try:
        result = probe.read_text(window["handle"], offset=offset, limit=limit)
    except Exception as error:
        return {"available": False,
                "reason": "read failed: %s" % type(error).__name__}
    if not result.get("available"):
        return {"available": False,
                "reason": result.get("failure_reason") or "read unavailable"}
    if result.get("mechanism") == "unavailable":
        # The adapter resolved no window (not found / ambiguous). Surface it as
        # `reason` + `found=False` rather than leaking a different key shape,
        # so every caller can report "that window isn't there" uniformly.
        return {"available": True, "found": False,
                "reason": result.get("failure_reason") or "window not found",
                "candidates": result.get("candidates") or []}
    captured = float(result.get("captured_at") or time.time())
    result.update(found=True, query=query,
                  age_seconds=max(0.0, time.time() - captured))
    return result


def find_inputs(query, *, name=None):
    """The controls in a window you could actually type into, by NAME.

    Locating an input box by name is what stops typing into the wrong place.
    Each entry carries `index`, which is what `act(..., control_index=...)`
    pins to - that path re-resolves the control under its own fresh, single-use
    token, so this read mints no authority of its own (a read never creates
    permission to act).

    `target_token` is included as the element's identity reference from the
    snapshot; consent is still minted by `act` from Sir's own words.
    """
    window, failure = _resolve_one(query)
    if failure is not None:
        return failure
    probe = _ua.UIAutomation(window_limit=MAX_WINDOWS, control_limit=MAX_CONTROLS)
    try:
        snapshot = probe.find_inputs(window["handle"], name=name)
    except Exception as error:
        return {"available": False,
                "reason": "find_inputs failed: %s" % type(error).__name__}
    if not snapshot.get("available"):
        return {"available": False,
                "reason": snapshot.get("failure_reason") or "unavailable"}
    controls = []
    for control in snapshot.get("controls") or []:
        controls.append({
            "name": str(control.get("name") or "")[:MAX_TEXT],
            "control_type": control.get("control_type") or "",
            "automation_id": str(control.get("automation_id") or "")[:MAX_TEXT],
            "value": str(control.get("value") or "")[:MAX_TEXT],
            "enabled": control.get("enabled"),
            "visible": control.get("visible"),
            "index": control.get("index"),
            "target_token": control.get("target_token"),
        })
    captured = float(snapshot.get("captured_at") or time.time())
    return {
        "available": True,
        "found": bool(controls),
        "query": query,
        "window": snapshot.get("window"),
        "captured_at": captured,
        "age_seconds": max(0.0, time.time() - captured),
        "count": len(controls),
        "controls": controls,
        # Honest when nothing matched: "no input box here" is different from
        # "UIA could not read this window".
        "reason": "" if controls else "no editable control matched",
    }


def summary_for_prompt(result, max_chars=1800):
    """Render an inspection result as bounded prompt context.

    Read-only by construction: no token is included, so nothing found here can
    ever be turned into an action by accident.
    """
    if not result:
        return "(UI Automation produced nothing)"
    if not result.get("available"):
        return "(UI Automation unavailable: %s)" % result.get("reason")
    if not result.get("found"):
        return "(no open window matched: %s)" % result.get("reason")
    window = result.get("window") or {}
    lines = [
        "UI Automation read of %r (process %s, minimized=%s):"
        % (window.get("title") or "", window.get("process"),
           window.get("minimized"))
    ]
    for control in (result.get("controls") or [])[:40]:
        bits = [control.get("name") or ""]
        if control.get("value"):
            bits.append("= %s" % control["value"])
        if not control.get("enabled", True):
            bits.append("(disabled)")
        line = "  - [%s] %s" % (control.get("control_type") or "?", " ".join(bits))
        lines.append(line[:200])
    # ROUND Z (2026-10-04): scrollability is DISCOVERABILITY. Before this, a
    # window could contain a perfectly good scroll container and the model had no
    # way to learn that, so "not visible" could only ever mean "give up or
    # relaunch". Bounded to a handful of lines and still read-only.
    scrollable = [c for c in (result.get("controls") or []) if c.get("can_scroll")]
    if scrollable:
        lines.append("  SCROLLABLE in this window (use ui_scroll to bring content "
                     "into view; it refuses if the window is not foreground):")
        for control in scrollable[:6]:
            percent = control.get("scroll_percent_v")
            where = ("at %s%%" % percent) if isinstance(percent, (int, float)) else ""
            axis = "v" if control.get("can_scroll_v") else "h"
            lines.append("    - [%s] %s (%s) %s"
                         % (control.get("control_type") or "?",
                            control.get("name") or "(unnamed container)",
                            axis, where))
    else:
        exposed = [c for c in (result.get("controls") or [])
                   if c.get("scroll_state") not in (None, "none")]
        if exposed:
            lines.append("  (no control here really scrolls - %d expose the "
                         "ScrollPattern but report themselves as not scrollable; "
                         "use the wheel fallback via ui_scroll)"
                         % len(exposed))
    if result.get("ambiguous"):
        lines.append("  (other matching windows: %s)"
                     % ", ".join(result.get("other_candidates") or []))
    return "\n".join(lines)[:max_chars]


# --------------------------------------------------------------------------
# GUARDED actions
# --------------------------------------------------------------------------
def _confirm_phrase_present(text):
    """Did the user actually say 'yes'?

    Deliberately strict. An action is refused unless the confirmation is
    unambiguous, which is the same standard the deterministic router uses for
    destructive commands.
    """
    return bool(re.search(
        r"\b(?:yes|yeah|yep|confirm|confirmed|do it|go ahead|please do|"
        r"proceed|that'?s right|correct)\b",
        str(text or "").lower()))


def act(query, button_name, *, action="click", value=None,
        confirmation=None, known_names=(), command_text="", actor="voice",
        control_index=None):
    """Perform ONE UIA action on ONE control, behind the risk gate.

    There is deliberately no `confirmed=` flag: a boolean is not evidence, and
    FRIDAY's own model-authored code can supply any boolean it likes. Consent has
    to arrive as a signed, single-use token minted from the words Sir actually
    said this turn (see `action_confirmations`). If no such token exists, this
    refuses - it does not fall back to a permissive default.

    Returns a dict shaped like a command_router action result, so the caller can
    report it and `log_action` it through exactly the same path as every other
    action FRIDAY takes.
    """
    result = {"handled": True, "action": "uia_%s" % action,
              "risk": None, "allowed": False, "changed": False,
              "success": False, "failure_reason": None}

    def _finish(outcome, **extra):
        from .audit import log_action

        try:
            log_action(actor=actor, risk=result.get("risk_level") or "reversible",
                       operation="uia_%s" % action, outcome=outcome,
                       allowed=result["allowed"], command=command_text,
                       target=button_name,
                       **{k: v for k, v in extra.items()
                          if k in ("error", "changed")})
        except Exception:
            pass
        return result

    if action not in ALLOWED_ACTIONS:
        result["failure_reason"] = ("action %r is not allowed through UIA "
                                    "(permitted: %s)"
                                    % (action, ", ".join(sorted(ALLOWED_ACTIONS))))
        return _finish("refused", error=result["failure_reason"])

    risk = _ua.risk_metadata(action)
    result["risk"] = risk
    # log_action expects a risk LEVEL string, so derive it from the same metadata
    # the perform() gate uses rather than inventing one here.
    result["risk_level"] = ("read_only" if risk.get("read_only")
                            else "destructive" if risk.get("destructive")
                            else "reversible")

    # -- CONSENT GATE -------------------------------------------------------
    # Resolved BEFORE the backend or any window, so an unauthorised call costs
    # nothing and touches nothing. A caller cannot skip this: `confirmation` is a
    # signed token, not a flag.
    from . import action_confirmations as _confirm

    if not confirmation:
        token, denied = _confirm.mint(action, query, button_name,
                                      known_names=known_names)
        if not token:
            result["failure_reason"] = (
                "refused: %s; no confirmation in Sir's own words for this turn"
                % denied.get("reason"))
            return _finish("refused", error=result["failure_reason"])
        confirmation = token
        result["minted"] = True
    else:
        result["minted"] = False

    target_window_title = query
    ok, why, info = _confirm.consume(confirmation, action, target_window_title,
                                     button_name)
    if not ok:
        result["failure_reason"] = "refused: %s" % why
        return _finish("refused", error=result["failure_reason"])
    result["consent"] = info

    if not available().get("available"):
        result["failure_reason"] = "UI Automation backend unavailable"
        return _finish("failed", error=result["failure_reason"])

    candidates = find_window(query)
    if not candidates:
        result["failure_reason"] = "no open window matched %r" % query
        return _finish("failed", error=result["failure_reason"])
    if len(candidates) > 1:
        result["failure_reason"] = ("%r matches several windows: %s"
                                    % (query, ", ".join(
                                        c.get("title") or "" for c in candidates[:4])))
        result["candidates"] = [c.get("title") or "" for c in candidates[:8]]
        return _finish("ambiguous", error=result["failure_reason"])
    target_window = candidates[0]
    if target_window.get("minimized"):
        # A minimized window has no realisable controls; saying so is more
        # honest than clicking blind.
        result["failure_reason"] = ("%r is minimized - restore it first"
                                    % (target_window.get("title") or query))
        return _finish("failed", error=result["failure_reason"])

    probe = _ua.UIAutomation(window_limit=MAX_WINDOWS, control_limit=MAX_CONTROLS)
    try:
        snapshot = probe.dom(target_window["handle"])
    except Exception as error:
        result["failure_reason"] = "dom read failed: %s" % type(error).__name__
        return _finish("failed", error=result["failure_reason"])
    matches = [c for c in (snapshot.get("controls") or [])
               if (c.get("name") or "").strip().lower() == str(button_name).strip().lower()]
    if not matches:
        result["failure_reason"] = ("no control named %r in that window" % button_name)
        return _finish("failed", error=result["failure_reason"])
    # ROUND ZB-L1 (2026-10-07): a caller that has ALREADY resolved an exact
    # control may pin it by its snapshot index. Without this the ambiguity
    # guard below fires on a real application: OpenCode's TUI exposes FIVE
    # distinct controls all named 'scrollable content' (transcript, todos,
    # ...), so a caller that had correctly picked one by size still got
    # '5 controls are named ... refusing to guess'. Consent is unaffected -
    # the token is still minted for (action, window, control NAME); this
    # only stops re-resolving by name when the caller already holds the
    # identity, which is exactly what the per-control target_token is for.
    chosen = matches[0]
    if control_index is not None:
        pinned = [c for c in matches if c.get("index") == control_index]
        if not pinned:
            result["failure_reason"] = (
                "control %r is not present at index %s in that window"
                % (button_name, control_index))
            return _finish("failed", error=result["failure_reason"])
        chosen = pinned[0]
    elif len(matches) > 1:
        result["failure_reason"] = ("%d controls are named %r - refusing to guess"
                                    % (len(matches), button_name))
        return _finish("ambiguous", error=result["failure_reason"])

    outcome = probe.perform(action, chosen["target_token"], value=value,
                            confirmation=True)
    result["changed"] = bool(outcome.get("changed"))
    result["success"] = bool(outcome.get("success"))
    result["failure_reason"] = outcome.get("failure_reason")
    # ROUND ZB-L1 (2026-10-07): propagate perform()'s EVIDENCE block.
    # Only success/changed/failure_reason were copied, so a caller could
    # never see WHY an action did or did not move anything: a scroll
    # reported `percent_moved = None` even when the container had published
    # a scroll percentage, and the per-turn action record could not show
    # pre/post UIA position. The VERDICT is unchanged - perform() is
    # authoritative and its success/verified/failure are already copied
    # verbatim above; this only carries evidence that was discarded.
    if isinstance(outcome.get("scroll"), dict):
        result["scroll"] = outcome["scroll"]
    if outcome.get("evidence") is not None:
        result["evidence"] = outcome["evidence"]
    result["target"] = {"window": target_window.get("title"),
                        "control": button_name}
    from .audit import log_action

    try:
        log_action(actor=actor, risk=result.get("risk_level") or "reversible",
                   operation="uia_%s" % action,
                   outcome="ok" if result["success"] else "failed",
                   allowed=True, command=command_text,
                   target="%s/%s" % (target_window.get("title"), button_name),
                   changed=result["changed"], error=result["failure_reason"])
    except Exception:
        pass
    return result


def can_act(text):
    """True when the user's own words contain a confirmation.

    Diagnostic only. It does NOT authorise anything: `act()` derives consent from
    the server-recorded turn, never from a string a caller hands in.
    """
    return _confirm_phrase_present(text)
