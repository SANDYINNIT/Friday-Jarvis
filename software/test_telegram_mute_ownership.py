"""Ownership record and mute gate unit checks. See test_request_reply_ownership for admission."""

import dataclasses
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())

from source.server.server import (  # noqa: E402
    LOCAL_OWNERSHIP,
    TurnOwnership,
    _clear_ownership,
    _external_reply_pending,
    _install_ownership,
    _ownership,
)

_OWNERSHIP_NAMES = (
    "LOCAL_OWNERSHIP", "TurnOwnership", "_clear_ownership",
    "_external_reply_pending", "_install_ownership", "_ownership",
)


def _refresh_ownership_names():
    """Rebind our ownership names to whatever the live module holds NOW.

    Other suites reload `source.server.server` (`test_intent_judge_removed`
    calls `importlib.reload(srv)`). A reload re-executes the module into the
    SAME module dict, swapping the class objects, while the names we imported
    here still point at the old ones. `_install_ownership` then raises
    `TypeError` doing `isinstance(obj, <new class>)` on an instance of the
    <old class> - which is why this file passed standalone but failed 22 tests
    in the full suite. Module-level `__getattr__` cannot help: it is only
    consulted for `module.attr`, never for global lookup inside a function.
    Refreshing in setUp means every test runs against the current objects.
    """
    module = importlib.import_module("source.server.server")
    for name in _OWNERSHIP_NAMES:
        globals()[name] = getattr(module, name)


class OwnershipCase(unittest.TestCase):
    """Base class: every test starts from freshly-bound ownership helpers."""

    def setUp(self):
        _refresh_ownership_names()

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")
with open(SERVER_PY, encoding="utf-8") as _fh:
    SERVER_SRC = _fh.read()


def telegram_state(speaks_locally=False, chat_id="42"):
    """server_state with an in-flight Telegram turn's ownership installed."""
    state = {}
    _install_ownership(state, TurnOwnership(
        channel="telegram", chat_id=chat_id, speaks_locally=speaks_locally))
    return state


def idle_state():
    """server_state for a purely local turn: nothing is owed elsewhere."""
    state = {}
    _clear_ownership(state)
    return state


class ExternalReplyPending(OwnershipCase):
    """Option 2: the guard follows the immutable RECORD, not the mirrored dict."""

    def test_in_flight_telegram_turn_is_pending(self):
        state = {}
        _install_ownership(state, TurnOwnership(
            channel="telegram", chat_id="42", speaks_locally=False))
        self.assertTrue(_external_reply_pending(state))

    def test_chat_turn_is_pending(self):
        state = {}
        _install_ownership(state, TurnOwnership(
            channel="text", chat_id="text", reply_token="tok",
            speaks_locally=False))
        self.assertTrue(_external_reply_pending(state))

    def test_local_turn_is_not_pending(self):
        self.assertFalse(_external_reply_pending({}))
        self.assertFalse(_external_reply_pending({"turn_ownership": None}))

    def test_cleared_turn_is_not_pending(self):
        state = {}
        _install_ownership(state, TurnOwnership(channel="telegram", chat_id="42"))
        _clear_ownership(state)
        self.assertFalse(_external_reply_pending(state))

    def test_a_legacy_dict_alone_does_not_claim_ownership(self):
        # Stale mirrored state must not be able to re-assert a claim: only the
        # record is authoritative, so a leftover dict reads as local.
        self.assertFalse(_external_reply_pending(
            {"telegram_capture": {"chat_id": "42"},
             "telegram_turn_mute": True}))


class TTSGateIsIntact(OwnershipCase):
    """The gate that decides playback must read the OWNING RECORD."""

    def test_both_gate_sites_still_read_the_ownership(self):
        gates = [
            i for i, line in enumerate(SERVER_SRC.splitlines(), 1)
            if "if _ownership(server_state).muted:" in line
        ]
        self.assertEqual(len(gates), 2, "expected a segment gate and an end gate")

    def test_the_gate_still_blocks_playback_when_muted(self):
        # Exactly the condition the response handler evaluates before feeding TTS.
        state = {}
        _install_ownership(state, TurnOwnership(
            channel="telegram", chat_id="42", speaks_locally=False))
        self.assertTrue(bool(_ownership(state).muted))

    def test_a_phone_turn_asked_to_speak_is_not_muted(self):
        # "Say it out loud" over Telegram must still reach the PC speakers.
        state = {}
        _install_ownership(state, TurnOwnership(
            channel="telegram", chat_id="42", speaks_locally=True))
        self.assertFalse(bool(_ownership(state).muted))

    def test_an_unmuted_local_turn_still_reaches_playback(self):
        state = {}
        _install_ownership(state, TurnOwnership(channel="telegram", speaks_locally=False))
        self.assertTrue(bool(_ownership(state).muted))
        # The phone turn finishes and releases ownership, THEN a local turn
        # arrives: the PC must be audible again.
        _clear_ownership(state)
        self.assertFalse(bool(_ownership(state).muted))


class TurnOwnershipRecord(OwnershipCase):
    """Option 2: one immutable record owns the turn, written in one place."""

    def test_the_record_is_frozen(self):
        own = TurnOwnership(channel="telegram", chat_id="42")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            own.chat_id = "99"

    def test_a_local_turn_is_never_muted(self):
        self.assertFalse(LOCAL_OWNERSHIP.muted)
        self.assertFalse(LOCAL_OWNERSHIP.is_external)
        self.assertEqual(LOCAL_OWNERSHIP.channel, "")

    def test_a_phone_turn_is_muted_unless_sir_asked_it_to_speak(self):
        self.assertTrue(TurnOwnership(channel="telegram",
                                      speaks_locally=False).muted)
        self.assertFalse(TurnOwnership(channel="telegram",
                                       speaks_locally=True).muted)

    def test_a_chat_turn_is_always_external(self):
        own = TurnOwnership(channel="text", chat_id="text", reply_token="tok",
                            speaks_locally=False)
        self.assertTrue(own.is_external)
        self.assertTrue(own.muted)

    def test_install_stores_the_record_and_creates_no_legacy_state(self):
        # Option 2 is complete: there is no `telegram_capture` dict and no
        # `telegram_turn_mute` bool any more. Install must write ONLY the record.
        state = {}
        _install_ownership(state, TurnOwnership(
            channel="telegram", chat_id="42",
            speaks_locally=False, post_action_screenshot=True,
            screen_request="show me this"))
        own = _ownership(state)
        self.assertEqual(own.chat_id, "42")
        self.assertTrue(own.post_action_screenshot)
        self.assertEqual(own.screen_request, "show me this")
        self.assertTrue(own.muted)
        self.assertNotIn("telegram_capture", state)
        self.assertNotIn("telegram_turn_mute", state)

    def test_chat_install_carries_the_reply_token(self):
        state = {}
        _install_ownership(state, TurnOwnership(
            channel="text", chat_id="text", reply_token="tok-1",
            speaks_locally=False))
        self.assertEqual(_ownership(state).reply_token, "tok-1")
        self.assertTrue(_ownership(state).muted)

    def test_clear_returns_the_pc_to_a_local_turn(self):
        state = {}
        _install_ownership(state, TurnOwnership(
            channel="telegram", chat_id="42", speaks_locally=False))
        _clear_ownership(state)
        self.assertIs(_ownership(state), LOCAL_OWNERSHIP)
        self.assertNotIn("telegram_capture", state)
        self.assertNotIn("telegram_turn_mute", state)

    def test_install_rejects_a_bare_dict(self):
        # The whole point is that ownership cannot be smuggled in as a mutable
        # dict again - that is the bug being retired.
        with self.assertRaises(TypeError):
            _install_ownership({}, {"chat_id": "42"})

    def test_a_malformed_slot_falls_back_to_local_not_to_none(self):
        self.assertIs(_ownership({"turn_ownership": None}), LOCAL_OWNERSHIP)
        self.assertIs(_ownership({"turn_ownership": {"chat_id": "42"}}),
                      LOCAL_OWNERSHIP)
        self.assertIs(_ownership({}), LOCAL_OWNERSHIP)

    def test_the_guard_reads_the_record_not_the_mirrored_dict(self):
        # The legacy dict could read as "pending" while the record said
        # otherwise (e.g. an empty chat_id still mirrors to a truthy dict).
        # One source of truth: the guard must follow the record.
        state = {}
        _install_ownership(state, TurnOwnership(
            channel="telegram", chat_id="", speaks_locally=False))
        self.assertTrue(_external_reply_pending(state))
        _clear_ownership(state)
        self.assertFalse(_external_reply_pending(state))


if __name__ == "__main__":
    unittest.main()
