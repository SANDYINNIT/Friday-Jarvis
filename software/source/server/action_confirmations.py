"""Confirmation tokens for interface actions.

THE PROBLEM THIS SOLVES
-----------------------
`ui_probe.act(..., confirmed=True)` let **any** caller claim Sir had confirmed.
That is not a safety gate, it is a boolean. FRIDAY's brain authors python that
calls into this package, so `confirmed=True` was something a model could simply
type - meaning the safeguard existed only until the model found it.

THE GATE
--------
A token can only be minted from the **current turn's real user words**, recorded
by the server at its single dispatch chokepoint (`server._mark_turn_dispatch`).
Code that the brain authors cannot write that ledger, so it cannot manufacture
consent. On top of that:

* tokens are **HMAC-signed** with a per-installation secret;
* every token is **single-use**, and a turn authorises **one action in total**;
* a token is **bound** to one exact (action, window, control) triple, so consent
  for one button cannot be spent on another;
* if Sir named a control, it must be the control being requested.

WHY THIS IS ON DISK, NOT IN MEMORY
-----------------------------------
Measured on the live runtime: open-interpreter executes brain-authored python in
a **separate process** (`interpreter/core/computer/terminal/terminal.py` runs it
through the terminal subprocess). An in-memory ledger therefore had an empty
history inside the very process that needed it, and a live "click the ShowBox
button" was refused on its first attempt for exactly that reason. So the ledger,
the spent set and the signing key all live in a small state file that any process
can read, guarded by an atomic replace.

HONEST LIMIT
------------
This is a *provenance* gate, not a sandbox. Anything that can run python on this
machine can read the state file and replay a genuine confirmation, or edit it.
What this genuinely prevents is the failure mode that actually occurs: an action
firing because a default was True, because a helper was called twice, because a
model read "confirmed=True" as "this is how you call it", or because consent for
one control was quietly reused for another.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time

from .file_logger import friday_logger as _friday_logger

_log = _friday_logger()

__all__ = [
    "ConfirmationRefused",
    "consume",
    "mint",
    "note_user_turn",
    "reset",
    "state_paths",
    "status",
]

# How long a token stays usable, and how long a recorded user turn can author
# one. A confirmation is consent for *now*, not for the rest of the session.
TOKEN_TTL_SECONDS = float(os.getenv("FRIDAY_CONFIRM_TTL", "45"))
TURN_TTL_SECONDS = float(os.getenv("FRIDAY_CONFIRM_TURN_TTL", "300"))
# Bounded: this is a gate, not an archive.
MAX_TURNS = int(os.getenv("FRIDAY_CONFIRM_MAX_TURNS", "8"))

_LOCK = threading.RLock()

# Explicit confirmation. Same standard the deterministic router applies to
# destructive commands, and deliberately not broader: "probably", "maybe" and
# "sounds good I guess" are not consent to press a button.
_CONFIRM_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|confirm(?:ed)?|do it|go ahead|please do|"
    r"proceed|that'?s right|that'?s correct|correct|sounds good|"
    r"carry on|continue|do that|press it|hit it|click it)\b")

# A direct imperative is itself consent, so "click Seven in Calculator" needs no
# second round trip. Per-action verbs, because typing is not clicking.
_VERBS = {
    "click": r"\b(?:click|press|tap|push|select|hit)\b",
    "invoke": r"\b(?:click|press|tap|push|select|hit|invoke|trigger)\b",
    "type": r"\b(?:type|enter|input|write|fill|put)\b",
    # POINTING, not acting: "highlight e5", "circle that button", "point out
    # the error". Display only - it cannot click anything - but it still draws
    # on Sir's screen, so it still needs his words behind it.
    "annotate": (r"\b(?:highlight|circle|mark|point|outline|box|draw|arrow|"
                 r"annotate|show\s+me|point\s+out|put\s+an?\s+arrow)\b"),
}

_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


class ConfirmationRefused(Exception):
    """Raised when an action has no verifiable consent behind it."""


# --------------------------------------------------------------------------
# State on disk (must survive into the process that runs brain-authored code)
# --------------------------------------------------------------------------
def state_paths():
    """(directory, key file, ledger file).

    Resolved per call rather than at import so a test can point it somewhere
    harmless. `FRIDAY_CONFIRM_STATE_DIR` overrides the default.
    """
    override = os.getenv("FRIDAY_CONFIRM_STATE_DIR")
    if override:
        base = override
    else:
        base = os.path.join(os.path.expanduser("~"), ".friday", "state")
    return base, os.path.join(base, "action_confirm.key"), \
        os.path.join(base, "action_confirm.json")


def _key():
    """Per-installation HMAC secret, created once and reused.

    An in-memory key would differ in every process, so a token minted by the
    server could never be verified by the code the brain actually runs.
    """
    _dir, key_path, _ledger = state_paths()
    try:
        os.makedirs(_dir, exist_ok=True)
    except Exception:
        pass
    try:
        with open(key_path, "rb") as handle:
            data = handle.read()
        if len(data) >= 32:
            return data[:32]
    except Exception:
        pass
    secret = os.urandom(32)
    try:
        temporary = "%s.%d.tmp" % (key_path, os.getpid())
        with open(temporary, "wb") as handle:
            handle.write(secret)
        os.replace(temporary, key_path)
    except Exception as error:
        # No writable state dir: fall back to a process-local key, which still
        # works for in-process callers and simply will not authorise a
        # brain-authored subprocess. Refusing is safer than allowing.
        _log.warning("action confirmation key not persisted (%s); "
                     "cross-process consent unavailable",
                     type(error).__name__)
        return secret
    return secret


def _empty_state():
    return {"seq": 0, "turns": [], "consumed": {}, "spent": []}


def _load():
    _dir, _key_path, ledger_path = state_paths()
    try:
        with open(ledger_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception:
        return _empty_state()
    if not isinstance(state, dict):
        return _empty_state()
    base = _empty_state()
    base.update({k: state.get(k) for k in base if k in state})
    if not isinstance(base.get("turns"), list):
        base["turns"] = []
    if not isinstance(base.get("consumed"), dict):
        base["consumed"] = {}
    if not isinstance(base.get("spent"), list):
        base["spent"] = []
    return base


def _save(state):
    """Atomic, so a concurrent reader never sees half a ledger."""
    _dir, _key_path, ledger_path = state_paths()
    try:
        os.makedirs(_dir, exist_ok=True)
        temporary = "%s.%d.tmp" % (ledger_path, os.getpid())
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, ledger_path)
        return True
    except Exception as error:
        _log.warning("action confirmation ledger not saved (%s)",
                     type(error).__name__)
        return False


def _norm(text):
    text = str(text or "").lower()
    text = _WORD_RE.sub(" ", text)
    return " ".join(text.split())


def _mentions(text, name):
    """Does `text` refer to `name`?

    Exact-token match for short/ambiguous names ("7", "OK"), substring match for
    longer ones ("Seven", "Equals"). Numbers need care: "press 7" must not be
    satisfied by "press 17".
    """
    name = _norm(name)
    if not name or not text:
        return False
    if " " in name or len(name) >= 3:
        return name in text
    return re.search(r"(?<!\w)%s(?!\w)" % re.escape(name), text) is not None


def _prune(state, now=None):
    now = time.time() if now is None else now
    turns = state.get("turns") or []
    spent = set(state.get("spent") or [])
    kept = []
    for turn in turns:
        if (now - float(turn.get("at") or 0)) > TURN_TTL_SECONDS:
            spent.discard(turn.get("seq"))
        else:
            kept.append(turn)
    state["turns"] = kept[-MAX_TURNS:]
    state["spent"] = sorted(x for x in spent if x is not None)
    cutoff = now - TOKEN_TTL_SECONDS * 4
    state["consumed"] = {k: v for k, v in (state.get("consumed") or {}).items()
                         if float(v or 0) > cutoff}
    return state


# --------------------------------------------------------------------------
# Server-side: record what Sir actually said
# --------------------------------------------------------------------------
def note_user_turn(text, source=""):
    """Record what Sir ACTUALLY said. Server-side only.

    Called from `_mark_turn_dispatch`, the one place every request passes through
    (typed, voice and Telegram follow-ups alike), so consent cannot be smuggled in
    another way.
    """
    words = _norm(text)
    if not words:
        return False
    with _LOCK:
        state = _prune(_load())
        state["seq"] = int(state.get("seq") or 0) + 1
        state.setdefault("turns", []).append(
            {"seq": state["seq"], "text": words, "source": str(source or ""),
             "at": time.time()})
        return _save(state)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------
def _authorizing_phrase(turn, action, window, control, known_names=()):
    """The words in `turn` that authorise this exact action, or None.

    Three accepted shapes, and one rule that overrides all of them:

    * Sir named THIS control and asked for the action
      ("click Seven in Calculator") - consent in the first place.
    * Sir named this control and confirmed ("yes, click Seven").
    * Sir confirmed without naming anything ("yes", "go ahead") - authorises
      exactly ONE action, because the turn is spent once used.

    THE RULE: if Sir named a control, it must be the control being requested.
    Two earlier versions got this wrong in opposite directions - one accepted
    "yes please click Delete in Calculator" as consent to press *Seven* (the
    WINDOW was named and the window was treated as the target), and the next one
    accepted "press the Advance button in the Button Bench app" as consent to
    click *OK* (naming the app plus a verb authorised anything inside it). Both
    were found by testing against a real app, not by reading the code.

    So: naming the app is context, never consent.
    """
    text = turn.get("text") or ""
    verb_re = _VERBS.get(action, _VERBS["click"])
    confirmed = bool(_CONFIRM_RE.search(text))
    imperatived = bool(re.search(verb_re, text))

    if control and _mentions(text, control):
        return text[:160] if (imperatived or confirmed) else None

    # A different control was named instead: a refusal, never a pass.
    for name in known_names or ():
        if name and name != window and _mentions(text, name):
            return None

    if confirmed:
        # Sir said yes but did not name the control. One action, one turn.
        return text[:160]
    return None


def _sign(payload_bytes):
    return hmac.new(_key(), payload_bytes, hashlib.sha256).digest()


def mint(action, window, control, *, known_names=()):
    """Mint a single-use token for exactly one action, or return (None, reason).

    Refuses when no recorded user turn authorises this exact action. There is no
    `confirmed=` bypass: if Sir has not actually said so, this returns None.
    """
    now = time.time()
    with _LOCK:
        state = _prune(_load(), now)
        spent = set(state.get("spent") or [])
        for turn in reversed(state.get("turns") or []):
            # A turn authorises ONE action in total, not one per call: without
            # this a single "yes" could be spent five times in one turn.
            if turn.get("seq") in spent:
                continue
            phrase = _authorizing_phrase(turn, action, window, control,
                                         known_names)
            if not phrase:
                continue
            payload = {
                "a": str(action), "w": str(window), "c": str(control),
                "p": phrase, "s": turn.get("seq"), "t": round(now, 3),
            }
            raw = json.dumps(payload, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
            signature = _sign(raw)
            token = "%s.%s" % (
                base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
                base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
            )
            return token, {
                "granted_by": phrase,
                "turn_seq": turn.get("seq"),
                "source": turn.get("source"),
                # Bound means his words named THIS control, not merely the app.
                "bound_to_target": bool(control and _mentions(phrase, control)),
            }
    return None, {"reason": "no recorded user turn authorises %r on %r"
                           % (action, control or window)}


def consume(token, action, window, control):
    """Verify and burn a token. Returns (ok, reason, info).

    Checks the signature, the expiry, that it has not been used before, and that
    it was minted for *these* arguments. A token for "Seven" cannot be spent on
    "Delete".
    """
    if not token:
        return False, "no confirmation token was presented", None
    now = time.time()
    with _LOCK:
        state = _prune(_load(), now)
        try:
            body, signature = str(token).split(".", 1)
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            given = base64.urlsafe_b64decode(
                signature + "=" * (-len(signature) % 4))
        except Exception:
            return False, "confirmation token is malformed", None
        if not hmac.compare_digest(_sign(raw), given):
            return False, "confirmation token failed its signature check", None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return False, "confirmation token payload is unreadable", None
        token_at = float(payload.get("t") or 0)
        if (now - token_at) > TOKEN_TTL_SECONDS:
            return False, ("confirmation expired %.0fs ago - ask Sir again"
                           % (now - token_at)), None
        if payload.get("a") != str(action) \
                or payload.get("w") != str(window) \
                or payload.get("c") != str(control):
            return False, ("confirmation was given for a different action "
                           "(%r/%r, not %r/%r)"
                           % (payload.get("a"), payload.get("c"),
                              action, control)), None
        fingerprint = hashlib.sha256(raw).hexdigest()[:32]
        if fingerprint in (state.get("consumed") or {}):
            return False, ("confirmation has already been used - each "
                           "confirmation authorises one action"), None
        state.setdefault("consumed", {})[fingerprint] = now
        # Spend the turn too, so one "yes" cannot be re-minted.
        try:
            state.setdefault("spent", []).append(int(payload.get("s")))
        except (TypeError, ValueError):
            pass
        if not _save(state):
            return False, "could not record the confirmation; refusing", None
    return True, None, {
        "granted_by": payload.get("p"),
        "bound_to_target": payload.get("c") == str(control),
    }


def status():
    """Honest, bounded self-report for the prompt / self-awareness."""
    with _LOCK:
        state = _prune(_load())
        turns = state.get("turns") or []
        return {
            "turns_recorded": len(turns),
            "last_turn_at": (turns[-1].get("at") if turns else None),
            "tokens_used": len(state.get("consumed") or {}),
            "ttl_seconds": TOKEN_TTL_SECONDS,
            "mintable_actions": sorted(_VERBS),
            "state_file": state_paths()[2],
            "shared_across_processes": True,
        }


def reset():
    """Forget everything. Used by tests and at a fresh session boundary."""
    with _LOCK:
        _save(_empty_state())