"""Per-turn provider skip lifecycle in `BrainRouter`.

THE BUG (found by inspection on 2026-10-03, not by a failing test)
-----------------------------------------------------------------
`_turn_skipped` is the router's strict forward-only blacklist: once a candidate
fails inside a turn it is skipped for the REST OF THAT TURN, so a key that
"healed" via a 20s cooldown cannot be bounced back to. It was cleared **only** by
`new_turn()` - and `new_turn()` had no production caller anywhere in the
repository. Its only two callers were `test_api_pools.py`.

`_turn_mark()` IS called in production, from `failover_to_next()`. So one provider
failure added an entry that nothing ever removed: a single transient 429 could
cost that pool every subsequent turn for the life of the process, silently, with
no log line. This is the same class of defect as the `_tool_this_turn` scoping
bug fixed on 2026-10-03, and it is a second, independent route by which cloud
selection collapses to local.

WHY THE FIX LIVES IN `apply_for()` AND NOT `_begin_single_turn()`
-----------------------------------------------------------------
`_begin_single_turn()` is also called on the MID-TURN retry paths
(`server.py` `new_output` L2450 and L2546). Resetting there would wipe the skip
list part-way through a turn and break the forward-only walk. `apply_for()` is
called exactly once per turn at the three real turn starts (`dispatch_text`,
`new_input`, `new_output`) and once per failover, and the failover path now
passes `_mid_turn=True`.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.getcwd())

from source.server import api_pools
from source.server import brain_router as br


class _FakeLLM:
    def __init__(self):
        self.model = None
        self.api_key = None
        self.api_base = None


class _FakeInterpreter:
    """`apply_for` only reads .messages and writes .llm."""

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.llm = _FakeLLM()
        self.computer = types.SimpleNamespace(language="python")
        self.offline = False
        self.auto_run = True
        self.debug = False


class RetryBudgetTests(unittest.TestCase):
    def test_primary_modal_keys_are_included_in_the_failover_budget(self):
        from source.server import server

        pools = {
            "brain_modal": {"total": 4},
            "brain_groq": {"total": 0},
            "brain_openrouter": {"total": 0},
            "brain_gemini": {"total": 0},
        }
        with mock.patch.object(server.api_pools, "describe_pools", return_value=pools), \
             mock.patch.object(server._brain_router_module, "GEMINI_MODEL_CHAIN", []), \
             mock.patch.object(server._brain_router_module, "OPENROUTER_MODEL_CHAIN", []):
            self.assertEqual(server._max_turn_retries(), 5)


def _user(text):
    return {"role": "user", "type": "message", "content": text}


def _console(text="out"):
    return {"role": "computer", "type": "console", "content": text}


def _assistant(text="ok"):
    return {"role": "assistant", "type": "message", "content": text}


# The primary cloud candidate, so a skip is observable in selection.
MODAL_POOL, MODAL_MODEL = "brain_modal", "nvidia/GLM-5.3-Flash-NVFP4"


class TurnSkipLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.router = br.BrainRouter()
        self.router._enabled = True
        # `failover_to_next()` -> `mark_failure()` -> `api_pools.fail_model()` and
        # `pool().fail()`, and `api_pools._LEDGER_PATH` is a module constant with
        # no env override - so an un-stubbed call writes a real cooldown into the
        # live `~/.friday/exhaustion_ledger.json`. Stub the mutators so this file
        # tests the LIFECYCLE and cannot poison the real ledger.
        _mutators = {
            "fail_model": lambda *a, **k: None,
            "model_succeed": lambda *a, **k: None,
            "model_banned": lambda *a, **k: False,
        }
        for _name, _replacement in _mutators.items():
            _patcher = mock.patch.object(api_pools, _name, _replacement)
            _patcher.start()
            self.addCleanup(_patcher.stop)

    def _start_turn(self, text, history=()):
        """A real new user turn: new_turn() then apply_for(), as production does."""
        self.router.new_turn()
        interp = _FakeInterpreter(history)
        self.router.apply_for(text, interp)
        return interp

    def _mid_turn(self, text, interp):
        """A failover inside a running turn: apply_for() only, no new_turn()."""
        return self.router.apply_for(text, interp)

    # 1. a failure marks the provider skipped
    def test_a_failure_marks_the_provider_skipped(self):
        self._start_turn("turn A")
        self.router._turn_mark(MODAL_POOL, MODAL_MODEL)
        self.assertTrue(self.router._turn_skip(MODAL_POOL, MODAL_MODEL))

    def test_failover_to_next_is_what_marks_it(self):
        """The production path, not a hand-rolled _turn_mark.

        Asserted against the ticket the router ACTUALLY selected. Which pool
        wins depends on live quota/cooldown state, so hard-coding one would make
        this a test of the ledger rather than of the turn lifecycle.
        """
        self._start_turn("turn A")
        # NB: `current` is a (pool, KEY, model) ticket; `_turn_skipped` stores
        # only (pool, model). Compare those two and never render the ticket - it
        # contains a real provider key, and a failing assertion would print it.
        failing = self.router.current
        self.assertTrue(failing, "apply_for must have selected a candidate")
        failing_pair = (failing[0], failing[2])
        before = set(self.router._turn_skipped)
        self.router.failover_to_next(_FakeInterpreter(), "turn A",
                                     reason="429 from provider")
        self.assertIn(failing_pair, self.router._turn_skipped)
        self.assertNotEqual(set(self.router._turn_skipped), before)

    # 2. the same turn keeps it skipped
    def test_the_same_turn_keeps_the_provider_skipped(self):
        self._start_turn("turn A")
        self.router._turn_mark(MODAL_POOL, MODAL_MODEL)
        # A mid-turn re-selection must NOT resurrect it.
        interp = _FakeInterpreter()
        self._mid_turn("turn A", interp)
        self.assertTrue(self.router._turn_skip(MODAL_POOL, MODAL_MODEL))
        self.assertNotEqual(str(interp.llm.model), "openai/" + MODAL_MODEL)

    def test_a_mid_turn_call_never_clears_the_list(self):
        self._start_turn("turn A")
        marked = self.router.current
        self.router._turn_mark(marked[0], marked[2])
        for _ in range(3):
            self.router.apply_for("turn A", _FakeInterpreter())
        self.assertTrue(self.router._turn_skip(marked[0], marked[2]),
                        "apply_for alone must never clear the skip list")

    # 3. a new turn clears the skip
    def test_a_new_turn_clears_the_skip(self):
        self._start_turn("turn A")
        self.router._turn_mark(MODAL_POOL, MODAL_MODEL)
        self.assertTrue(self.router._turn_skip(MODAL_POOL, MODAL_MODEL))
        self._start_turn("turn B")
        self.assertEqual(self.router._turn_skipped, set())

    def test_the_reset_also_survives_many_accumulated_failures(self):
        self._start_turn("turn A")
        for pool, model in (("brain_modal", MODAL_MODEL),
                            ("brain_groq", "openai/gpt-oss-20b"),
                            ("brain_openrouter", "nvidia/nemotron")):
            self.router._turn_mark(pool, model)
        self.assertEqual(len(self.router._turn_skipped), 3)
        self._start_turn("turn B")
        self.assertEqual(self.router._turn_skipped, set())

    # 4. the provider becomes eligible again
    def test_the_provider_is_eligible_again_on_the_next_turn(self):
        """Asserted on the LIFECYCLE state, not on which model happens to win.

        Which candidate is actually available depends on live quota/cooldown
        state, so pinning an exact model here would make the test about the
        quota ledger rather than about turn boundaries.
        """
        self._start_turn("turn A")
        self.router._turn_mark(MODAL_POOL, MODAL_MODEL)
        self.assertTrue(self.router._turn_skip(MODAL_POOL, MODAL_MODEL))
        self._start_turn("turn B")
        self.assertFalse(
            self.router._turn_skip(MODAL_POOL, MODAL_MODEL),
            "a provider skipped in turn A must be eligible again in turn B")

    def test_the_router_does_not_skip_anything_at_the_start_of_a_turn(self):
        interp = self._start_turn("turn B")
        # The selection log records the skip list it consulted; if it had been
        # carried over, the primary would have been passed over.
        self.assertEqual(self.router._turn_skipped, set())
        self.assertTrue(str(interp.llm.model),
                        "apply_for must configure some model")

    def test_repeated_turn_starts_do_not_drift(self):
        chosen = []
        for i in range(6):
            interp = self._start_turn("turn %d" % i)
            chosen.append(str(interp.llm.model))
            self.router._turn_mark(MODAL_POOL, MODAL_MODEL)
        self.assertEqual(len(set(chosen)), 1,
                         "the same provider served every turn: %s" % chosen)

    # 5. current-turn tool activity still behaves correctly
    def test_mid_turn_tool_activity_still_skips_cloud(self):
        """The storm guard must survive the lifecycle change."""
        interp = _FakeInterpreter([
            _user("move the app to the right monitor"),
            _console("screenshot taken"),
            _assistant("working"),
        ])
        self.router.new_turn()
        self._mid_turn("move the app to the right monitor", interp)
        self.assertIn("qwen3", str(interp.llm.model),
                      "cloud must stay skipped mid-turn after a tool ran")

    def test_brand_new_turn_ignores_this_turn_tool_activity(self):
        """No tool has run yet in a fresh turn, so cloud must be selectable."""
        interp = _FakeInterpreter([_user("move the app to the right monitor")])
        self.router.apply_for("move the app to the right monitor", interp)
        self.assertNotIn("qwen3", str(interp.llm.model),
                         "a fresh turn must not be treated as mid-turn")

    # 6. previous-turn tool activity does not affect the new turn
    def test_previous_turn_tool_rows_do_not_affect_the_new_turn(self):
        history = [
            _user("Click the ShowBox button in the Button Bench app"),
            _console(), _assistant("done"), _console(), _assistant("done"),
        ]
        interp = _FakeInterpreter(history)
        self.router.apply_for("In that app, click whichever control", interp)
        self.assertNotIn("qwen3", str(interp.llm.model),
                         "the previous turn's tools must not downgrade this one")

    # regression guard for the actual defect
    def test_new_turn_has_a_production_caller_now(self):
        """`new_turn()` used to be test-only; assert it is reachable in prod."""
        cleared = []
        original = self.router.new_turn

        def spy():
            cleared.append(True)
            return original()

        self.router.new_turn = spy
        self._start_turn("turn A")
        self.assertTrue(cleared, "apply_for() must reset per-turn state")

    def test_queued_requests_do_not_reset_before_the_turn_runs(self):
        """A queued /chat request has not started a turn yet."""
        self.router._turn_mark(MODAL_POOL, MODAL_MODEL)
        # dispatch_text returns early for a queued request without calling
        # apply_for, so nothing should have cleared the mark.
        self.assertTrue(self.router._turn_skip(MODAL_POOL, MODAL_MODEL))


if __name__ == "__main__":
    unittest.main()
