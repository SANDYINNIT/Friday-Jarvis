"""Explicit lifecycle for `/chat` requests: queued / active / completed /
expired / cancelled.

WHY THIS EXISTS
---------------
`/chat` used to have four implicit states spread across an ``asyncio.Lock``, a
reply box and a timeout, with nothing connecting them:

* a 504 closed the reply box and released the lock **but cancelled nothing** -
  the turn kept running, and a queued request still executed for a caller who
  was already gone;
* a queued request could run **after its caller expired**, doing real work
  nobody would ever read;
* late answers fell into an **unowned bounded sink** - work survived with no
  owner to receive it.

The fix that does NOT work is raising ``FRIDAY_TEXT_TIMEOUT``: lock-waiting and
reply-waiting are separate timeouts and neither one cancels anything. The caller
timeout therefore stays where it is, and ownership is made explicit instead.

DESIGN NOTE - `clock` is injected
---------------------------------
Every timing decision goes through a ``clock`` callable so the whole contract
can be tested without sleeping. That is also why expiry is expressed as a
*property* (`expired()`) plus an explicit ``sweep()``: a request does not
"become" expired by itself, something inspects it, and that inspection is the
single place the decision is made.
"""

import asyncio
import collections
import time

__all__ = [
    "ACTIVE",
    "CANCELLED",
    "COMPLETED",
    "EXPIRED",
    "PRUNABLE",
    "QUEUED",
    "TERMINAL",
    "ChatRequest",
    "LifecycleRegistry",
]

QUEUED = "queued"
ACTIVE = "active"
COMPLETED = "completed"
EXPIRED = "expired"
CANCELLED = "cancelled"

# Final states - no transition out of these is legal.
TERMINAL = frozenset({COMPLETED, EXPIRED, CANCELLED})

# Final states the caller will never collect from, so bookkeeping may be
# dropped. EXPIRED is deliberately NOT here: a caller still has to be able to
# ask "what happened to my request?" and be told it expired.
PRUNABLE = frozenset({COMPLETED, CANCELLED})


class ChatRequest:
    """One `/chat` call, with an explicit state and its own chunks/cancel flag.

    Instances are intentionally NOT shared and carry no reference to any other
    request: chunks, cancellation and state are all per-instance, which is what
    stops two overlapping callers from reading each other's answers.
    """

    def __init__(self, token, text, timeout, clock=time.monotonic, owner=""):
        self.token = str(token)
        self.text = str(text)
        self.timeout = float(timeout)
        self.owner = str(owner or "")
        self._clock = clock
        self.created_at = clock()
        self.state = QUEUED
        self.chunks = []
        self.cancel_requested = False
        # Per-instance, never a module-level event: sharing one would let a
        # cancelled request silence a concurrent one.
        self.cancel_event = asyncio.Event()

    # -- time ------------------------------------------------------------
    @property
    def deadline(self):
        return self.created_at + self.timeout

    def expired(self):
        """True once this request's caller can no longer be waited on."""
        return self._clock() >= self.deadline

    def remaining(self):
        """Never negative - a caller computing `deadline - now` must not loop."""
        return max(0.0, self.deadline - self._clock())

    def sweep(self):
        """Expire a still-queued request whose caller is gone.

        The only place expiry is decided, so an overdue request cannot be
        activated by a later branch that forgot to check the clock.
        """
        if self.state == QUEUED and self.expired():
            self.expire()
        return self.state

    # -- transitions -----------------------------------------------------
    def activate(self):
        """queued -> active. Refuses anything that may no longer run."""
        if self.state != QUEUED:
            return False
        if self.expired():
            # Still queued and already past its deadline: mark it expired so it
            # can never be picked up, and tell the caller so rather than
            # silently doing work for nobody.
            self.expire()
            return False
        self.state = ACTIVE
        return True

    def complete(self):
        """active -> completed. Only a request that actually ran may complete."""
        if self.state != ACTIVE:
            return False
        self.state = COMPLETED
        return True

    def expire(self):
        if self.state in TERMINAL:
            return False
        self.state = EXPIRED
        self._signal_cancel()
        return True

    def cancel(self):
        if self.state in TERMINAL:
            return False
        self.state = CANCELLED
        self._signal_cancel()
        return True

    def _signal_cancel(self):
        """Tell cooperative work to stop. Idempotent."""
        self.cancel_requested = True
        if not self.cancel_event.is_set():
            self.cancel_event.set()

    @property
    def may_run(self):
        """The ONLY gate work should consult before continuing."""
        return self.state == ACTIVE and not self.cancel_requested

    # -- results ---------------------------------------------------------
    def accept_result(self, chunk):
        """Store a chunk only while this caller can still receive it.

        This is the guard that keeps a late answer from surviving its owner:
        once expired or cancelled the chunk is dropped, not routed to a sink
        where it would wait forever with nobody to read it.
        """
        if not self.may_run:
            return False
        self.chunks.append(str(chunk))
        return True

    @property
    def answer(self):
        return " ".join(self.chunks)

    def delivered_text(self):
        """What the caller may be shown. Nothing at all unless it COMPLETED.

        An expired turn's partial text must never be presented as the answer -
        that is how "I couldn't finish" turns into a confident wrong reply.
        """
        if self.state != COMPLETED:
            return ""
        return self.answer


class LifecycleRegistry:
    """Bounded bookkeeping for live requests, keyed by opaque token."""

    def __init__(self, max_held=64, clock=time.monotonic):
        self._items = collections.OrderedDict()
        self._max = int(max_held)
        self._clock = clock

    def register(self, request):
        """Store by TOKEN only. The caller's text is never an identifier: a
        token is opaque, so one caller cannot address another's request."""
        self._items[request.token] = request
        # Evict oldest so a flood of callers cannot grow this without bound.
        while self._max > 0 and len(self._items) > self._max:
            self._items.popitem(last=False)
        return request

    def get(self, token):
        return self._items.get(token)

    def sweep(self):
        """Expire overdue queued requests; drop what nobody will collect.

        Returns how many entries were dropped. EXPIRED is kept so a caller can
        still be told its request expired instead of it simply vanishing.
        """
        dropped = 0
        for token in list(self._items):
            request = self._items[token]
            request.sweep()
            if request.state in PRUNABLE:
                del self._items[token]
                dropped += 1
        return dropped

    def cancel_all(self):
        """Cancel every live request (queued and active). Returns the count.

        Used by `POST /chat/cancel`: stopping the interpreter is not enough on
        its own, because a request still sitting here would otherwise be picked
        up by a later drain and run as if nothing had happened. Terminal states
        are left alone, so calling this twice simply reports 0 the second time
        instead of corrupting a record that already finished.
        """
        cancelled = 0
        for request in list(self._items.values()):
            try:
                if request.cancel():
                    cancelled += 1
            except Exception:
                # One malformed record must not abort the cancel of the rest.
                continue
        return cancelled

    def __len__(self):
        return len(self._items)
