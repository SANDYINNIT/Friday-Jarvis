"""Tests for the failover/hard-stop fixes made after the empty-reply diagnosis.

These lock in the corrections found by reviewing the first pass:
  * landing on LOCAL must still dispatch the turn (it is a real candidate, and
    the end of the chain, not a failure)
  * a direct reply must be recorded exactly once, via the enqueue path
  * a hop-exhausted turn must not double-send the apology to Telegram
  * a live-turn wrap must not emit `complete` (that finalises twice)
  * a candidate that was never asked must not be cooled
"""

import os
import sys
import unittest

sys.path.insert(0, os.getcwd())

SERVER_PY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "source", "server", "server.py"
)
SOURCE = open(SERVER_PY, encoding="utf-8").read()
ROUTER_PY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "source", "server", "brain_router.py"
)
ROUTER_SOURCE = open(ROUTER_PY, encoding="utf-8").read()


def _function_body(text, marker="def "):
    """Return the source of one nested function, up to the next sibling def.

    These helpers live inside start_server() and are indented by 4 spaces, so
    the terminator is a def at that same indentation. Both `def` and
    `async def` can appear, so the cut is the EARLIER of the two.
    """
    if marker not in text:
        raise AssertionError(f"{marker} not found")
    after = text.split(marker, 1)[1]
    positions = [after.find(t) for t in ("\n    def ", "\n    async def ")]
    positions = [p for p in positions if p >= 0]
    if positions:
        after = after[: min(positions)]
    return after


class FailoverDispatchTests(unittest.TestCase):
    def test_local_landing_does_not_short_circuit_to_apology(self):
        """Regression: the hop branch tested `if not hopped`, and
        failover_to_next() returns False once it lands on local - so FRIDAY
        apologised "every thinking model is unavailable" WITHOUT ever running
        the local brain. The decision must be based on a real ticket."""
        self.assertIn("landed = getattr(server_state[\"brain_router\"], \"current\", None)", SOURCE)
        self.assertIn("if not landed or hop_count > hop_max:", SOURCE)

    def test_no_longer_discards_the_local_landing(self):
        # The exhausted branch must not be gated on `not hopped` alone.
        self.assertNotIn("if not hopped or hop_count > hop_max:", SOURCE)

    def test_untried_candidate_is_not_cooled(self):
        """Regression: the exhausted branch cooled whatever ticket the router
        had just applied, penalising a provider that was never asked."""
        self.assertIn('if not landed or landed[0] == "local":', SOURCE)

    def test_direct_response_is_not_double_recorded(self):
        """Regression: queue_direct_response recorded into the turn log AND
        enqueued a message that new_output records again, doubling the
        snapshot and duplicating the chat row.

        Checks for an actual CALL, not a mention - the explanatory comment in
        the body deliberately names the function it no longer calls.
        """
        import re

        body = _function_body(SOURCE, "def queue_direct_response")
        code = "\n".join(line for line in body.splitlines()
                         if not line.strip().startswith("#"))
        self.assertNotRegex(code, r"_record_turn_output\s*\(")

    def test_direct_response_still_drops_empty_text(self):
        body = _function_body(SOURCE, "def queue_direct_response")
        self.assertIn("if not text.strip():", body)

    def test_speech_only_helper_exists_and_does_not_finalize(self):
        """The hard-stop wrap runs while the turn is still live, so it must
        speak without emitting `status: complete` (that finalised twice and
        cleared the ban on the candidate we deliberately killed)."""
        self.assertIn("def queue_speech_only(", SOURCE)
        body = _function_body(SOURCE, "def queue_speech_only")
        self.assertNotIn('"complete"', body)

    def test_hard_stop_uses_speech_only(self):
        self.assertIn("queue_speech_only(wrap)", SOURCE)

    def test_hop_exhaustion_clears_direct_text_to_avoid_double_send(self):
        """queue_direct_response sets last_direct_text, and the deterministic
        block re-sends it, so the phone got the apology twice and /chat kept a
        stale answer."""
        self.assertIn('server_state["last_direct_text"] = None\n                                continue',
                      SOURCE)

    def test_retry_hint_parser_is_shared_with_api_pools(self):
        """Two divergent parsers meant Google's 'retry in Ns' was ignored."""
        body = _function_body(ROUTER_SOURCE, "def _retry_after_seconds")
        self.assertIn("api_pools._retry_after_seconds", body)


if __name__ == "__main__":
    unittest.main()
    print("PASS: failover corrections")
