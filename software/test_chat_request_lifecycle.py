"""ROUND ZB — the `/chat` request lifecycle contract.

Written BEFORE the implementation, as checklist item I requires, so the tests
define the states rather than describing whatever the code happens to do.

WHAT WAS BROKEN
---------------
`/chat` had four implicit states spread across an asyncio.Lock, a reply box and
a timeout, with nothing connecting them:

* a **504 closed the reply box and released the lock, but cancelled nothing** -
  the turn kept running and any queued request still executed, for a caller who
  was already gone;
* a **queued request could run after its caller expired**, doing real work that
  nobody would ever read;
* **late answers fell into an unowned bounded sink** - work survived with no
  owner to receive it;
* the collector's 0.4 s "did the turn finish?" grace wait **discarded the value
  it had just fetched**, so the final chunk of a multi-sentence answer could be
  silently dropped;
* and the fix everyone reaches for first - raising `FRIDAY_TEXT_TIMEOUT` - cannot
  work, because lock-waiting and reply-waiting have separate timeouts and
  neither one cancels anything.

WHAT THIS CONTRACT SAYS
-----------------------
Five explicit states: queued -> active -> {completed, expired, cancelled}.
Terminal states are final. A request that is expired or cancelled must never
run, must never accept another result, and must never leak into another
request's box.
"""

import time

from source.server.chat_lifecycle import (  # noqa: E402
    ACTIVE,
    CANCELLED,
    COMPLETED,
    EXPIRED,
    QUEUED,
    TERMINAL,
    ChatRequest,
    LifecycleRegistry,
)


class TestStatesAreExplicit:
    def test_the_five_states_exist(self):
        assert {QUEUED, ACTIVE, COMPLETED, EXPIRED, CANCELLED} == {
            "queued", "active", "completed", "expired", "cancelled"}

    def test_terminal_states_are_final(self):
        assert TERMINAL == frozenset({COMPLETED, EXPIRED, CANCELLED})
        assert QUEUED not in TERMINAL
        assert ACTIVE not in TERMINAL

    def test_a_new_request_starts_queued(self):
        req = ChatRequest("tok", "hello", timeout=10.0, clock=lambda: 100.0)
        assert req.state == QUEUED


class TestLegalTransitions:
    def test_queued_can_become_active(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        assert req.activate() is True
        assert req.state == ACTIVE

    def test_active_can_complete(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        req.complete()
        assert req.state == COMPLETED

    def test_active_can_expire(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        req.expire()
        assert req.state == EXPIRED

    def test_active_can_be_cancelled(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        assert req.cancel() is True
        assert req.state == CANCELLED

    def test_queued_can_be_cancelled(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        assert req.cancel() is True
        assert req.state == CANCELLED


class TestIllegalTransitions:
    """A terminal request must be final - the whole point of the contract."""

    def test_a_completed_request_cannot_be_reactivated(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        req.complete()
        assert req.activate() is False
        assert req.state == COMPLETED

    def test_an_expired_request_cannot_be_activated(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.expire()
        assert req.activate() is False
        assert req.state == EXPIRED

    def test_a_cancelled_request_cannot_be_completed(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.cancel()
        req.complete()
        assert req.state == CANCELLED

    def test_a_terminal_request_cannot_expire_again(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        req.complete()
        req.expire()
        assert req.state == COMPLETED

    def test_only_cancel_works_before_activation(self):
        """`complete()` means 'the caller got their answer' - impossible while
        the request has never run."""
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        assert req.complete() is False
        assert req.state == QUEUED


class TestExpiry:
    """A caller who is gone must never cause work to be done."""

    def test_a_request_expires_when_the_deadline_passes(self):
        clock = [0.0]
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: clock[0])
        clock[0] = 11.0
        assert req.expired() is True

    def test_a_queued_request_past_its_deadline_never_runs(self):
        clock = [0.0]
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: clock[0])
        clock[0] = 99.0
        assert req.activate() is False
        assert req.state in (EXPIRED, QUEUED)

    def test_activation_of_a_past_deadline_marks_it_expired(self):
        clock = [0.0]
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: clock[0])
        clock[0] = 99.0
        req.activate()
        assert req.state == EXPIRED
        assert req.may_run is False

    def test_a_queued_request_marks_itself_expired_on_inspection(self):
        clock = [0.0]
        req = ChatRequest("tok", "hi", 10.0, clock=lambda: clock[0])
        clock[0] = 99.0
        req.sweep()
        assert req.state == EXPIRED

    def test_expiry_sets_the_cancellation_signal(self):
        """Cooperative cancellation: the running work must be told to stop."""
        clock = [0.0]
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: clock[0])
        req.activate()
        clock[0] = 99.0
        req.expire()
        assert req.cancel_requested is True
        assert req.cancel_event.is_set() is True

    def test_remaining_time_never_goes_negative(self):
        clock = [0.0]
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: clock[0])
        clock[0] = 500.0
        assert req.remaining() == 0.0


class TestResults:
    """Who may read a result, and who must never see one."""

    def test_results_are_accepted_only_while_active(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        assert req.accept_result("early") is False   # still queued
        req.activate()
        assert req.accept_result("chunk") is True

    def test_a_late_result_after_expiry_is_discarded(self):
        clock = [0.0]
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: clock[0])
        req.activate()
        clock[0] = 99.0
        req.expire()
        assert req.accept_result("too late") is False
        assert req.chunks == []

    def test_a_late_result_after_cancel_is_discarded(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        req.cancel()
        assert req.accept_result("too late") is False
        assert req.chunks == []

    def test_no_result_after_completion(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        req.accept_result("a")
        req.complete()
        assert req.accept_result("b") is False
        assert req.chunks == ["a"]

    def test_multi_chunk_answers_are_kept_in_order(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        for word in ("first", "second", "third"):
            req.accept_result(word)
        assert req.chunks == ["first", "second", "third"]
        # `text` is what the CALLER posted; the answer is `answer`.
        assert req.answer == "first second third"

    def test_an_incomplete_answer_is_not_presented_as_complete(self):
        req = ChatRequest("tok", "hi", timeout=10.0, clock=lambda: 0.0)
        req.activate()
        req.accept_result("partial")
        req.expire()
        assert req.state == EXPIRED
        assert req.delivered_text() == ""


class TestOwnership:
    """One request's state must never bleed into another's."""

    def test_requests_do_not_share_chunks(self):
        a = ChatRequest("a", "one", 10.0, lambda: 0.0)
        b = ChatRequest("b", "two", 10.0, lambda: 0.0)
        a.activate()
        a.accept_result("only for a")
        assert b.chunks == []
        assert b.answer == ""          # `text` is b's own request, not an answer

    def test_requests_do_not_share_cancellation(self):
        a = ChatRequest("a", "one", 10.0, lambda: 0.0)
        b = ChatRequest("b", "two", 10.0, lambda: 0.0)
        a.activate()
        a.cancel()
        assert b.cancel_requested is False
        assert b.cancel_event.is_set() is False

    def test_a_follow_up_request_does_not_inherit_state(self):
        first = ChatRequest("t1", "first", 10.0, lambda: 0.0)
        first.activate()
        first.complete()
        second = ChatRequest("t2", "follow up", 10.0, lambda: 0.0)
        assert second.state == QUEUED
        assert second.activate() is True


class TestRegistry:
    """Bookkeeping must be bounded and must not leak across requests."""

    def test_a_registered_request_is_found_by_its_token(self):
        reg = LifecycleRegistry()
        req = reg.register(ChatRequest("tok", "hi", 10.0, lambda: 0.0))
        assert reg.get("tok") is req

    def test_an_unknown_token_returns_none(self):
        assert LifecycleRegistry().get("nope") is None

    def test_registry_size_is_bounded(self):
        reg = LifecycleRegistry(max_held=5)
        for i in range(20):
            reg.register(ChatRequest("t%d" % i, "x", 10.0, lambda: 0.0))
        assert len(reg) <= 5

    def test_terminal_requests_are_pruned(self):
        reg = LifecycleRegistry()
        req = reg.register(ChatRequest("tok", "hi", 10.0, lambda: 0.0))
        req.activate()
        req.complete()
        assert reg.sweep() == 1
        assert reg.get("tok") is None

    def test_sweep_expires_overdue_queued_requests(self):
        clock = [0.0]
        reg = LifecycleRegistry()
        reg.register(ChatRequest("tok", "hi", 10.0, lambda: clock[0]))
        clock[0] = 99.0
        reg.sweep()
        assert reg.get("tok").state == EXPIRED

    def test_sweep_does_not_touch_a_living_active_request(self):
        clock = [0.0]
        reg = LifecycleRegistry()
        req = reg.register(ChatRequest("tok", "hi", 10.0, lambda: clock[0]))
        req.activate()
        clock[0] = 5.0            # still inside its window
        reg.sweep()
        assert req.state == ACTIVE
        assert reg.get("tok") is req

    def test_registration_is_keyed_by_an_opaque_token_not_the_text(self):
        """Security: a caller's words must not become an identifier another
        caller could address."""
        reg = LifecycleRegistry()
        a = reg.register(ChatRequest("opaque-a", "delete my history", 10.0, lambda: 0.0))
        assert reg.get("delete my history") is None
        assert reg.get("opaque-a") is a


class TestIdempotence:
    def test_expiry_is_idempotent(self):
        clock = [0.0]
        req = ChatRequest("tok", "hi", 10.0, lambda: clock[0])
        req.activate()
        req.expire()
        req.expire()
        assert req.state == EXPIRED

    def test_cancellation_is_idempotent(self):
        req = ChatRequest("tok", "hi", 10.0, lambda: 0.0)
        assert req.cancel() is True
        assert req.cancel() is False
        assert req.state == CANCELLED
