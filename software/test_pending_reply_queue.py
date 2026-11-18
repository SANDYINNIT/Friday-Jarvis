"""Queue and reply-box unit checks. Production drain is covered in test_request_reply_ownership."""

import collections
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())

from source.server.server import (  # noqa: E402  (real production functions)
    _PENDING_QUEUE_MAX,
    _TEXT_REPLY_QUEUE_MAX,
    _new_reply_token,
    _ownership,
    _pending_clear,
    _pending_count,
    _pending_has,
    _pending_peek,
    _pending_pop,
    _pending_push,
    _pending_queue,
    _post_reply,
    _close_reply_box,
)

def _new_state():
    """A realistic server_state with a live reply_waiters registry."""
    return {
        "turn_channel": "",
        "telegram_capture": None,
        "turn_reply_token": None,
        "telegram_turn_mute": False,
        "pending_turns": collections.deque(),
        "reply_waiters": {},
        "is_paused": False,
    }


class PendingQueueIntegrityTests(unittest.TestCase):
    def test_three_queued_requests_are_all_retained_in_order(self):
        state = _new_state()
        for n in (1, 2, 3):
            _pending_push(state, user_text="question %d" % n,
                          content={"role": "user", "type": "message",
                                   "content": "question %d" % n},
                          reply_token="token-%d" % n, source="text")
        self.assertEqual(_pending_count(state), 3)
        for n in (1, 2, 3):
            record = _pending_pop(state)
            self.assertEqual(record["user_text"], "question %d" % n)
            self.assertEqual(record["reply_token"], "token-%d" % n)
            self.assertEqual(record["content"]["content"], "question %d" % n)
        self.assertEqual(_pending_count(state), 0)

    def test_a_queued_record_carries_its_own_token_and_content(self):
        """The old code wrote three scalars separately, so they could disagree."""
        state = _new_state()
        _pending_push(state, user_text="raw text",
                      content={"role": "user", "type": "message", "content": "prefixed"},
                      reply_token="tok", source="text")
        record = _pending_peek(state)
        self.assertEqual(record["user_text"], "raw text")
        self.assertEqual(record["content"]["content"], "prefixed")
        self.assertEqual(record["reply_token"], "tok")

    def test_a_voice_follow_up_does_not_destroy_a_parked_chat_token(self):
        """REGRESSION LOCK.

        The voice queue branch used to write `pending_reply_token = None` to
        "clear a stale token". That nulled the token of the request parked
        BEHIND it, so the parked request ran but could never receive its
        answer - a guaranteed 504 plus the answer spoken on the desk.
        """
        state = _new_state()
        _pending_push(state, user_text="chat question", content={"content": "chat question"},
                      reply_token="parked-token", source="text")
        _pending_push(state, user_text="voice follow up", content={"content": "voice follow up"},
                      reply_token=None, source="voice")
        parked = _pending_pop(state)
        self.assertEqual(parked["reply_token"], "parked-token",
                         "the parked /chat request lost its token to a voice follow-up")

    def test_a_voice_memory_command_does_not_drop_a_queued_request(self):
        """REGRESSION LOCK.

        The voice memory branch used to run `pending_input = None` before
        setting the memory answer, which DESTROYED an already-queued request.
        """
        state = _new_state()
        _pending_push(state, user_text="parked", content={"content": "parked"},
                      reply_token="parked-token", source="text")
        _pending_push(state, user_text="remember this",
                      source="voice", direct_response="I'll remember that.")
        self.assertEqual(_pending_count(state), 2,
                         "a memory command dropped the queued request")
        first = _pending_pop(state)
        second = _pending_pop(state)
        self.assertEqual(first["reply_token"], "parked-token")
        self.assertEqual(second["direct_response"], "I'll remember that.")

    def test_the_queue_is_bounded_and_says_so(self):
        """Bounded, but the eviction is reported rather than silent."""
        state = _new_state()
        for n in range(_PENDING_QUEUE_MAX):
            _pending_push(state, user_text="q%d" % n, reply_token="t%d" % n)
        self.assertEqual(_pending_count(state), _PENDING_QUEUE_MAX)
        evicted = _pending_push(state, user_text="overflow", reply_token="tX")
        self.assertIsNotNone(evicted, "overflow must report the evicted request")
        self.assertEqual(evicted["user_text"], "q0")
        self.assertEqual(_pending_count(state), _PENDING_QUEUE_MAX)

    def test_pending_clear_empties_everything(self):
        state = _new_state()
        _pending_push(state, user_text="a", reply_token="ta")
        _pending_push(state, user_text="b", reply_token="tb")
        _pending_clear(state)
        self.assertFalse(_pending_has(state))
        self.assertIsNone(_pending_peek(state))

    def test_the_queue_is_created_on_demand(self):
        """Several callers build a partial state dict; the queue must cope."""
        state = {"is_paused": False}
        _pending_push(state, user_text="a", reply_token="ta")
        self.assertEqual(_pending_count(state), 1)
        self.assertIsInstance(_pending_queue(state), collections.deque)


# ---------------------------------------------------------------------------
# Required case 4: Telegram mute state is not stolen by another queued request.
# ---------------------------------------------------------------------------


class TelegramRouteIsolationTests(unittest.TestCase):
    def _telegram_turn_running(self):
        state = _new_state()
        state["turn_channel"] = "998877"
        # Option 2 (2026-10-18): a running phone turn is expressed by the
        # immutable ownership record; the legacy dict is only its mirror, and
        # production never writes it directly any more (only _install_ownership
        # does). Setting the fields by hand left the record absent, so routing
        # reads returned "" and a voice follow-up appeared to destroy the route.
        from source.server.server import TurnOwnership, _install_ownership
        _install_ownership(state, TurnOwnership(
            channel="telegram", chat_id="998877", speaks_locally=False))
        return state

    def test_a_queued_chat_request_leaves_the_telegram_mute_alone(self):
        state = self._telegram_turn_running()
        _pending_push(state, user_text="hi", content={"content": "hi"},
                      reply_token="chat-token", source="text")
        self.assertTrue(_ownership(state).muted,
                        "a queued /chat request un-muted the running Telegram "
                        "turn; its answer would be read aloud on the headset")
        # Check the INVARIANT (the route still points at the phone), not the
        # incidental dict shape: since Option 2 the capture is the mirror of the
        # ownership record and also carries post_action_screenshot/screen_request.
        self.assertEqual(_ownership(state).chat_id, "998877")
        self.assertEqual(state["turn_channel"], "998877")

    def test_the_queued_chat_request_does_not_steal_the_phone_destination(self):
        """Required case 5, partial: no reply crosses into another request."""
        state = self._telegram_turn_running()
        state["turn_reply_token"] = None
        _pending_push(state, user_text="hi", content={"content": "hi"},
                      reply_token="chat-token", source="text")
        # The running Telegram turn posts its answer now.
        _post_reply(state, "the phone's answer")
        self.assertEqual(_pending_count(state), 1)
        self.assertNotIn("chat-token", state["reply_waiters"],
                         "a queued request must not own a reply box yet")

class TextReplySinkTests(unittest.TestCase):
    def test_the_sink_is_bounded(self):
        state = _new_state()
        state["turn_reply_token"] = None
        for n in range(_TEXT_REPLY_QUEUE_MAX):
            self.assertTrue(_post_reply(state, "chunk %d" % n))
        self.assertFalse(_post_reply(state, "one too many"),
                         "the sink accepted more than its bound")
        self.assertEqual(state["text_reply_queue"].qsize(), _TEXT_REPLY_QUEUE_MAX)

    def test_an_unowned_reply_does_not_leave_the_sink_empty(self):
        state = _new_state()
        state["turn_reply_token"] = None
        _post_reply(state, "orphan")
        self.assertEqual(state["text_reply_queue"].qsize(), 1)

    def test_a_closed_box_falls_through_to_the_bounded_sink(self):
        """The /chat handler closes its box on EVERY exit, including the 504
        path, so any later post for that token used to vanish silently."""
        state = _new_state()
        token = _new_reply_token(state)
        state["turn_reply_token"] = token
        _close_reply_box(state, token)
        _post_reply(state, "late arrival")
        self.assertEqual(state["text_reply_queue"].qsize(), 1,
                         "a late reply was silently discarded instead of "
                         "landing in the bounded sink")

    def test_a_full_reply_box_is_reported_not_silently_truncated(self):
        state = _new_state()
        token = _new_reply_token(state)
        state["turn_reply_token"] = token
        for n in range(8):
            self.assertTrue(_post_reply(state, "chunk %d" % n))
        self.assertFalse(_post_reply(state, "overflow"),
                         "a full reply box must report the drop")
        self.assertEqual(state["reply_waiters"][token].qsize(), 8)


if __name__ == "__main__":
    unittest.main()