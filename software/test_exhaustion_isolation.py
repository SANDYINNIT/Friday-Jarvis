"""The exhaustion ledger must be unreachable from the test suite.

WHY THIS MATTERS MORE THAN "TESTS MAKE NOISE"
----------------------------------------------
`api_pools._persist_ledger()` is a **full-snapshot rewrite**: on every mutator
(`fail_model`, `Pool.fail`, AND `Pool.succeed`) it serialises every currently
banned key and truncates the file. So a test reaching a real mutator with fake
keys does not merely add a junk line - it **deletes every genuine provider
cooldown** and replaces it with fabricated entries, which `_restore_ledger()`
then applies on the next FRIDAY boot as if they were real.

Measured exposure before this fix: `test_gemini_tts.py` (6 tests, fake keys
`acc1`/`acc2`), `test_api_pools.py` (3 tests constructing real `Pool` objects),
`test_brain_router_turn_lifecycle.py` (1), `test_screen_understanding.py` (1).
`_LEDGER_PATH` had no env override, so unlike the audit log, the memory DB and
the confirmation ledger there was nowhere to redirect it.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.getcwd())

from source.server import api_pools


def _sha(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class LedgerPathOverrideTests(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get("FRIDAY_EXHAUSTION_LEDGER")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.previous is None:
            os.environ.pop("FRIDAY_EXHAUSTION_LEDGER", None)
        else:
            os.environ["FRIDAY_EXHAUSTION_LEDGER"] = self.previous

    def test_defaults_to_the_live_location_when_unset(self):
        os.environ.pop("FRIDAY_EXHAUSTION_LEDGER", None)
        self.assertEqual(api_pools.ledger_path(), api_pools._LEDGER_PATH)
        self.assertIn(".friday", api_pools.ledger_path())

    def test_the_override_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "custom.json")
            os.environ["FRIDAY_EXHAUSTION_LEDGER"] = target
            self.assertEqual(api_pools.ledger_path(), target)

    def test_the_path_is_resolved_per_call_not_cached_at_import(self):
        """A module-level constant would freeze the first value seen."""
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "one.json")
            second = os.path.join(tmp, "two.json")
            os.environ["FRIDAY_EXHAUSTION_LEDGER"] = first
            self.assertEqual(api_pools.ledger_path(), first)
            os.environ["FRIDAY_EXHAUSTION_LEDGER"] = second
            self.assertEqual(api_pools.ledger_path(), second)

    def test_conftest_redirects_the_ledger_for_the_session(self):
        self.assertIn("FRIDAY_EXHAUSTION_LEDGER", os.environ,
                      "conftest.py must set it at module scope, before "
                      "collection imports anything")
        self.assertIn("friday-pytest-state-", os.environ["FRIDAY_EXHAUSTION_LEDGER"])


class MutatorsWriteOnlyToTheOverrideTests(unittest.TestCase):
    """Every mutator, driven for real, must land in the override."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="friday-ledger-test-")
        self.target = os.path.join(self.tmp, "ledger.json")
        self.live = api_pools._LEDGER_PATH
        self.previous = os.environ.get("FRIDAY_EXHAUSTION_LEDGER")
        os.environ["FRIDAY_EXHAUSTION_LEDGER"] = self.target
        self.addCleanup(self._restore_env)

        # Real Pool objects, so _persist_ledger succeeds rather than dying on a
        # duck-typed fake and silently "passing" this test for the wrong reason.
        pool = api_pools.Pool("brain_groq", ["synthetic-key-a"], min_interval=0.0)
        patcher = mock.patch.object(api_pools, "_pools", {"brain_groq": pool})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = pool

    def _restore_env(self):
        if self.previous is None:
            os.environ.pop("FRIDAY_EXHAUSTION_LEDGER", None)
        else:
            os.environ["FRIDAY_EXHAUSTION_LEDGER"] = self.previous

    def _entries(self):
        if not os.path.exists(self.target):
            return {}
        with open(self.target, encoding="utf-8") as handle:
            return json.load(handle)

    def test_pool_fail_writes_only_to_the_override(self):
        before = _sha(self.live) if os.path.exists(self.live) else None
        self.pool.fail("synthetic-key-a", cooldown=600.0)
        self.assertTrue(os.path.exists(self.target))
        self.assertTrue(any(k.startswith("brain_groq:")
                            for k in self._entries().get("keys", {})),
                        self._entries())
        if before is not None:
            self.assertEqual(_sha(self.live), before,
                             "the LIVE ledger was modified by a test")

    def test_pool_succeed_also_writes_only_to_the_override(self):
        """succeed() rewrites too - the snapshot is replaced on the happy path."""
        before = _sha(self.live) if os.path.exists(self.live) else None
        self.pool.succeed("synthetic-key-a")
        self.assertTrue(os.path.exists(self.target))
        if before is not None:
            self.assertEqual(_sha(self.live), before)

    def test_fail_model_writes_only_to_the_override(self):
        before = _sha(self.live) if os.path.exists(self.live) else None
        api_pools.fail_model("brain_gemini", "gemini-3.1-flash-lite", cooldown=600.0)
        self.assertTrue(os.path.exists(self.target))
        self.assertIn("brain_gemini:gemini-3.1-flash-lite",
                      self._entries().get("models", {}))
        if before is not None:
            self.assertEqual(_sha(self.live), before)

    def test_no_plaintext_key_is_written(self):
        """The 2026-09-29 guarantee: digests only, never key material."""
        self.pool.fail("synthetic-key-a", cooldown=600.0)
        with open(self.target, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("synthetic-key-a", raw)
        self.assertIn(api_pools._key_fingerprint("synthetic-key-a"), raw)

    def test_restore_reads_the_override_not_the_live_file(self):
        self.pool.fail("synthetic-key-a", cooldown=600.0)
        restored = api_pools._restore_ledger()
        self.assertGreaterEqual(restored, 1)
        self.assertTrue(self.pool.banned,
                        "the override ledger should have re-banned the key")

    def test_explicit_path_argument_still_wins(self):
        """The pre-existing `path=` seam must keep working."""
        other = os.path.join(self.tmp, "explicit.json")
        api_pools._persist_ledger(path=other)
        self.assertTrue(os.path.exists(other))
        self.assertFalse(os.path.exists(self.target))


class LiveLedgerUntouchedTests(unittest.TestCase):
    """The real file must not gain or lose an entry during a session."""

    def test_live_ledger_is_not_written_by_the_own_mutators(self):
        previous = os.environ.get("FRIDAY_EXHAUSTION_LEDGER")
        os.environ["FRIDAY_EXHAUSTION_LEDGER"] = os.path.join(
            tempfile.mkdtemp(prefix="friday-ledger-live-"), "x.json")
        self.addCleanup(lambda: os.environ.__setitem__(
            "FRIDAY_EXHAUSTION_LEDGER", previous) if previous is not None
            else os.environ.pop("FRIDAY_EXHAUSTION_LEDGER", None))

        live = api_pools._LEDGER_PATH
        before = _sha(live) if os.path.exists(live) else None
        pool = api_pools.Pool("brain_groq", ["another-synthetic"],
                              min_interval=0.0)
        with mock.patch.object(api_pools, "_pools", {"brain_groq": pool}):
            pool.fail("another-synthetic", cooldown=600.0)
            api_pools.fail_model("brain_openrouter", "some/model")
            pool.succeed("another-synthetic")
        after = _sha(live) if os.path.exists(live) else None
        self.assertEqual(before, after,
                         "the live exhaustion ledger changed during tests")


if __name__ == "__main__":
    unittest.main()