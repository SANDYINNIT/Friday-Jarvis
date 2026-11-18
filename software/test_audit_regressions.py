"""Regression tests for audit findings D1 and D2 (2026-10-02).

D1 - a /chat request that arrived while another turn was still running was
     queued, then dispatched by the follow-up path with NO reply token. That
     reset turn_reply_token to None, routed the reply into the legacy shared
     queue, and left the waiting caller to time out. My earlier live test passed
     only because neither request happened to be queued.

D2 - the DURABLE journals (outcomes.md, lessons.md, scratchpad.md) had no
     secret redaction at all, while the SQLite store next to them did. A key
     spoken aloud or pasted for her to repeat was written to disk in plaintext.

All secrets below are SYNTHETIC, publicly documented shapes. No real
credential is used, printed, or stored anywhere in this file.
"""

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from source.server import self_improve
from source.server.credential_stripper import redact_for_storage

# Synthetic, non-functional stand-ins. Deliberately not the documented example
# values in full: these cannot authenticate against anything.
SYNTHETIC_SECRETS = (
    "AKIA" + "QWERASDFZXCVBGHN",          # AWS access key id shape
    "AIza" + "SyB1c2D3e4F5g6H7i8J9k0L1m",  # Google API key shape
    "wk-aa11.ws-bb22",                     # Modal combined proxy token shape
    "987654321:AAHsyn1het1cT0k3nV4lu3",    # Telegram bot token shape
    "sk-or-v1-" + "0123456789abcdef" * 2,   # OpenRouter key shape
    "ghp_" + "0123456789abcdefghijklmnopqrstuvwx",  # GitHub token shape
    "xoxb-000000000000-" + "syntheticSlackTokenValue",
)


class DurableJournalRedactionTests(unittest.TestCase):
    """D2: the journals are memory stores and must redact like one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(self_improve, "outcomes_path",
                              lambda: str(self.root / "outcomes.md")),
            mock.patch.object(self_improve, "lessons_path",
                              lambda: str(self.root / "lessons.md")),
            mock.patch.object(self_improve, "scratchpad_path",
                              lambda: str(self.root / "scratchpad.md")),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def _all_written_text(self):
        blob = []
        for name in ("outcomes.md", "lessons.md", "scratchpad.md"):
            path = self.root / name
            if path.exists():
                blob.append(path.read_text(encoding="utf-8"))
        return "\n".join(blob)

    def test_append_outcome_never_writes_a_secret(self):
        for secret in SYNTHETIC_SECRETS:
            self.assertTrue(
                self_improve.append_outcome(
                    "sir asked me to repeat this %s" % secret, tag="asked"),
                "append_outcome refused to record anything at all",
            )
        written = self._all_written_text()
        for secret in SYNTHETIC_SECRETS:
            self.assertNotIn(secret, written, "secret written to a journal")

    def test_append_lesson_note_never_writes_a_secret(self):
        for secret in SYNTHETIC_SECRETS:
            self_improve.append_lesson_note("learned that %s works" % secret)
        written = self._all_written_text()
        for secret in SYNTHETIC_SECRETS:
            self.assertNotIn(secret, written, "secret written to lessons.md")

    def test_scratchpad_note_never_writes_a_secret(self):
        for secret in SYNTHETIC_SECRETS:
            self_improve.scratchpad_note("tried using %s" % secret)
        written = self._all_written_text()
        for secret in SYNTHETIC_SECRETS:
            self.assertNotIn(secret, written, "secret written to scratchpad.md")

    def test_journals_still_record_ordinary_text(self):
        """Redaction must not be a blanket silencer."""
        self.assertTrue(self_improve.append_outcome(
            "checked the screen and found Roblox in focus", tag="did"))
        self.assertTrue(self_improve.scratchpad_note("resolved the window handle"))
        written = self._all_written_text()
        self.assertIn("Roblox", written)
        self.assertIn("resolved the window handle", written)

    def test_redact_for_storage_catches_every_shape(self):
        for secret in SYNTHETIC_SECRETS:
            self.assertNotIn(secret, redact_for_storage("value %s end" % secret))

    def test_redact_for_storage_is_idempotent(self):
        for secret in SYNTHETIC_SECRETS:
            once = redact_for_storage("value %s end" % secret)
            self.assertEqual(once, redact_for_storage(once))

    def test_redact_for_storage_never_raises_on_odd_input(self):
        for value in (None, 123, b"bytes", object(), ""):
            self.assertIsInstance(redact_for_storage(value), str)


class QueuedTurnReplyTokenTests(unittest.TestCase):
    """D1: a queued /chat request must keep its own reply box.

    These assert the real state transitions in server.py's source. The full
    server cannot be imported in a test (it constructs the assistant), so the
    queueing and consuming lines are located and inspected directly - which
    still catches the exact regression, because the bug was a missing argument
    on one of these calls, not runtime behaviour.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent
                   / "source" / "server" / "server.py").read_text(encoding="utf-8")

    # PHASE 3 (2026-10-03): the token now travels inside ONE pending record
    # instead of a set of independent scalars. These assertions encode the new
    # invariant - a queued request carries its OWN token to the drain, and no
    # path may destroy another request's token.

    def test_state_declares_a_pending_queue_not_scalar_slots(self):
        self.assertIn('"pending_turns": collections.deque()', self.src)
        for gone in ('"pending_reply_token": None', '"pending_input": None',
                     '"pending_user_text": None', '"pending_router_response": None',
                     '"pending_memory_response": None', '"pending_screen_response": None'):
            self.assertNotIn(gone, self.src,
                             "scalar slot %s came back; it is last-writer-wins" % gone)

    def test_queue_branch_stores_the_token_with_the_request(self):
        # dispatch_text's `if response_is_active():` queue branch must push ONE
        # record carrying the request, its content AND its reply token.
        self.assertIn("_pending_push(server_state, user_text=request, content=content,",
                      self.src)
        self.assertIn("reply_token=reply_token, source=\"text\"", self.src)

    def test_follow_up_path_restores_the_token(self):
        # The consumer that actually dispatches the queued turn must take the
        # token off the record and forward it.
        self.assertIn('record = _pending_pop(server_state)', self.src)
        self.assertIn('pending_token = record.get("reply_token")', self.src)
        self.assertIn("reply_token=pending_token,", self.src)

    def test_every_mark_turn_dispatch_call_forwards_a_token(self):
        """No call site may silently drop the token again."""
        import re
        calls = re.findall(r"_mark_turn_dispatch\((?:[^()]|\([^()]*\))*\)",
                           self.src, flags=re.S)
        self.assertGreaterEqual(len(calls), 3, "expected the dispatch call sites")
        for call in calls:
            if "def _mark_turn_dispatch" in call:
                continue
            self.assertIn("reply_token=", call,
                          "a dispatch call site no longer forwards a token")

    def test_no_live_code_writes_a_pending_token_scalar(self):
        # The voice follow-up used to write pending_reply_token = None to
        # "clear" a stale token. That nulled the token of a request parked
        # BEHIND it, so the parked request kept running but could never be
        # handed its answer: a guaranteed 504 plus the answer spoken on the
        # desk. A voice follow-up now declares reply_token=None on its OWN
        # record, which costs the parked request nothing.
        offenders = [ln.strip() for ln in self.src.splitlines()
                     if "pending_reply_token" in ln and not ln.strip().startswith("#")]
        self.assertEqual(offenders, [],
                         "live code still touches the pending token scalar: %s" % offenders)

    def test_voice_follow_up_queues_its_own_record(self):
        self.assertIn('_pending_push(server_state, user_text=clean_content,',
                      self.src)
        self.assertIn("content=content, reply_token=None, source=\"voice\"", self.src)

    def test_token_is_stamped_unconditionally(self):
        # The stale-token bug: stamping only when non-None left the previous
        # request's token in place for the next turn. The stamp now lives in
        # _install_text_reply_route and is called without a None guard.
        self.assertIn('server_state["turn_reply_token"] = reply_token', self.src)
        self.assertNotIn('if reply_token:\n        server_state["turn_reply_token"]',
                         self.src)

    def test_route_install_is_only_called_from_proven_owners(self):
        """_install_text_reply_route mutates the shared reply slots, so it must
        never be reachable from the `if response_is_active():` QUEUE branch -
        only from the `else` branch, which runs when no turn is in flight.

        Checked on the AST rather than by substring: a sliding text window
        cannot tell a queue branch from the else-branch that follows it."""
        import ast
        tree = ast.parse(self.src)
        queue_bodies = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if "response_is_active" not in ast.unparse(node.test):
                continue
            queue_bodies.append(node)
        self.assertGreaterEqual(len(queue_bodies), 4,
                                "expected the queue branches in dispatch_text/new_input")
        for node in queue_bodies:
            for stmt in node.body:
                called = [
                    sub.func.attr for sub in ast.walk(stmt)
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                ]
                self.assertNotIn("_install_text_reply_route", called,
                                 "the reply route is installed inside a queue branch: %s"
                                 % ast.unparse(stmt)[:140])
                # A queue branch must hand the request to the queue and stop.
                self.assertNotIn("return", [type(s).__name__.lower() for s in node.body[:-1]],
                                 "a queue branch continues past its return")


class ModuleLevelScopeTests(unittest.TestCase):
    """A latent 500 that only real end-to-end traffic could reach.

    `_flog` and `trace` are defined INSIDE start_server(), so they are closure
    names. The module-level `_mark_turn_dispatch` logged through them, which
    raised NameError the first time the history trim actually fired - the first
    turn after 200 messages - converting a working trim into an HTTP 500.

    This sat latent from the moment the trim was added. The unit tests all
    passed, and the full suite passed, because no test ever drove more than 200
    real messages through a live turn. Only a real run did.
    """

    @classmethod
    def setUpClass(cls):
        cls.path = (Path(__file__).resolve().parent
                    / "source" / "server" / "server.py")
        cls.src = cls.path.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def _module_level_functions(self):
        return [n for n in self.tree.body if isinstance(n, ast.FunctionDef)]

    def _closure_only_names(self):
        """Names assigned inside start_server() - invisible to module level."""
        inner = next(n for n in self.tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "start_server")
        assigned = set()
        for node in ast.walk(inner):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assigned.add(node.name)
        return assigned

    def test_module_level_functions_never_use_start_server_closure_names(self):
        closures = self._closure_only_names()
        # Names that also exist at module level are legitimately usable.
        module_names = set()
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        module_names.add(target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    module_names.add((alias.asname or alias.name).split(".")[0])
        forbidden = closures - module_names - {"start_server"}
        self.assertTrue(forbidden, "expected start_server to define closure names")

        offenders = {}
        for func in self._module_level_functions():
            used = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
            # Names the function defines itself - every kind of binding counts,
            # or ordinary parameters read as false positives.
            local = set()
            for arg in (func.args.posonlyargs + func.args.args
                        + func.args.kwonlyargs):
                local.add(arg.arg)
            if func.args.vararg:
                local.add(func.args.vararg.arg)
            if func.args.kwarg:
                local.add(func.args.kwarg.arg)
            for node in ast.walk(func):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            local.add(target.id)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    local.add(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        local.add((alias.asname or alias.name).split(".")[0])
                elif isinstance(node, ast.comprehension):
                    for sub in ast.walk(node.target):
                        if isinstance(sub, ast.Name):
                            local.add(sub.id)
                elif isinstance(node, (ast.For, ast.AsyncFor)):
                    # A loop variable is a real local binding. Missing this made
                    # `for key in (...)` look like a reference to some outer
                    # `key`, which produced false positives.
                    for sub in ast.walk(node.target):
                        if isinstance(sub, ast.Name):
                            local.add(sub.id)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    local.add(node.target.id)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    local.add(node.name)
            clash = (used & forbidden) - local
            if clash:
                offenders[func.name] = sorted(clash)
        self.assertEqual(offenders, {},
                         "module-level functions reference start_server() closure "
                         "names, which raise NameError at runtime: %s" % offenders)

    def test_the_specific_bug_is_guarded(self):
        """Name the real offenders explicitly, so narrowing the scan above
        cannot quietly stop testing the thing that actually broke."""
        self.assertNotIn('"_flog"', self.src.split("def _mark_turn_dispatch", 1)[1]
                         .split("def _max_turn_retries", 1)[0].replace(
                             "# _flog/trace are closures", "")
                         .replace("_flog`/`trace`", ""))

    def test_mark_turn_dispatch_trims_without_raising_at_the_cap(self):
        """Drive it past the cap - the exact case that used to 500."""
        from source.server.server import _mark_turn_dispatch

        class _Interp:
            pass

        interp = _Interp()
        interp.messages = [{"role": "user" if i % 4 == 0 else "assistant",
                            "content": "m%d" % i} for i in range(500)]
        state = {"turn_user_text": "", "turn_content": ""}
        _mark_turn_dispatch(state, interp, user="hi", content="hi", source="text")
        self.assertEqual(len(interp.messages), 200)
        self.assertEqual(interp.messages[0]["role"], "user")
        self.assertEqual(interp.messages[-1]["content"], "m499")
        # A second call while already under the cap must be a no-op, not a raise.
        _mark_turn_dispatch(state, interp, user="hi2", content="hi2", source="text")
        self.assertEqual(len(interp.messages), 200)


if __name__ == "__main__":
    unittest.main()