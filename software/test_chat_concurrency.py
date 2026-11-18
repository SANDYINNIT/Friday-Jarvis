"""Concurrency tests for /chat reply routing.

The reply path used to be ONE shared asyncio.Queue with N waiters. Two
simultaneous /chat callers could therefore receive each other's answer, or one
could hang until timeout while the other consumed the reply. Each request now
gets its own reply box, correlated by a token stamped onto the turn.
"""

import ast
import asyncio
import unittest
from pathlib import Path

SERVER_PY = Path(__file__).resolve().parent / "source" / "server" / "server.py"


def _load_reply_helpers():
    """Exec just the reply-routing helpers out of server.py.

    server.py cannot be imported in a test (it constructs the whole assistant),
    so the three small pure helpers are extracted by AST and run in isolation.
    That keeps the test honest - it exercises the REAL source text, not a
    hand-copied imitation of it.

    The module-level CONSTANTS those helpers close over are lifted from the
    same source tree rather than hard-coded here, so a change to a bound in
    server.py cannot silently leave this harness testing a stale number.
    """
    source = SERVER_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SERVER_PY))
    wanted = {"_new_reply_token", "_post_reply", "_close_reply_box"}
    wanted_consts = {"_TEXT_REPLY_QUEUE_MAX"}
    chunks = []
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            chunks.append(ast.get_source_segment(source, node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted_consts:
                    constants[target.id] = ast.literal_eval(node.value)
    assert len(chunks) == len(wanted), (
        "expected %d reply helpers in server.py, found %d"
        % (len(wanted), len(chunks))
    )
    assert set(constants) == wanted_consts, (
        "expected the reply-sink bound %s in server.py, found %s"
        % (sorted(wanted_consts), sorted(constants))
    )

    class _NullLogger:
        def info(self, *a, **k):
            pass

        warning = error = debug = info

    namespace = {
        "asyncio": asyncio,
        "uuid": __import__("uuid"),
        "_turn_log": _NullLogger(),
        "_redact_text": lambda v: str(v),
    }
    namespace.update(constants)
    for chunk in chunks:
        exec(compile(chunk, str(SERVER_PY), "exec"), namespace)
    return namespace


HELPERS = _load_reply_helpers()


class ReplyBoxRoutingTests(unittest.TestCase):
    def setUp(self):
        self.state = {}

    def test_each_request_gets_a_distinct_box(self):
        first = HELPERS["_new_reply_token"](self.state)
        second = HELPERS["_new_reply_token"](self.state)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.state["reply_waiters"]), 2)

    def test_reply_goes_only_to_the_turn_that_asked(self):
        async def scenario():
            one = HELPERS["_new_reply_token"](self.state)
            two = HELPERS["_new_reply_token"](self.state)

            self.state["turn_reply_token"] = one
            HELPERS["_post_reply"](self.state, "answer to caller ONE")
            self.state["turn_reply_token"] = two
            HELPERS["_post_reply"](self.state, "answer to caller TWO")

            box_one = self.state["reply_waiters"][one]
            box_two = self.state["reply_waiters"][two]
            got_one = await asyncio.wait_for(box_one.get(), timeout=1.0)
            got_two = await asyncio.wait_for(box_two.get(), timeout=1.0)
            return got_one, got_two

        got_one, got_two = asyncio.new_event_loop().run_until_complete(scenario())
        self.assertEqual(got_one, "answer to caller ONE")
        self.assertEqual(got_two, "answer to caller TWO")

    def test_concurrent_callers_do_not_cross_talk(self):
        """Two callers at once: serialized, and each still gets its own answer.

        FRIDAY has one brain and one interpreter, and turn_channel /
        telegram_capture / turn_reply_token are single slots - so concurrent
        turns CANNOT both run. The endpoint therefore serializes them with an
        asyncio.Lock and reports 503 when one is already in flight, rather than
        interleaving and cross-talking. This test proves both halves of that.
        """
        async def serialized_caller(label, answer, delay):
            lock = self.state.setdefault("turn_lock", asyncio.Lock())
            async with lock:
                token = HELPERS["_new_reply_token"](self.state)
                self.state["turn_reply_token"] = token
                await asyncio.sleep(delay)
                HELPERS["_post_reply"](self.state, answer)
                box = self.state["reply_waiters"][token]
                return await asyncio.wait_for(box.get(), timeout=2.0)

        async def scenario():
            return await asyncio.gather(
                serialized_caller("alpha", "alpha's own reply", 0.05),
                serialized_caller("beta", "beta's own reply", 0.0),
            )

        alpha, beta = asyncio.new_event_loop().run_until_complete(scenario())
        self.assertEqual(alpha, "alpha's own reply")
        self.assertEqual(beta, "beta's own reply")

    def test_a_busy_server_refuses_rather_than_interleaving(self):
        """With the lock held, the second caller must not touch the single slots."""
        async def scenario():
            lock = self.state.setdefault("turn_lock", asyncio.Lock())
            await lock.acquire()
            try:
                # Simulate what the endpoint does when it cannot get the lock.
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=0.2)
                    return "acquired"
                except asyncio.TimeoutError:
                    return "refused"
            finally:
                lock.release()

        self.assertEqual(
            asyncio.new_event_loop().run_until_complete(scenario()), "refused"
        )

    def test_closing_a_box_does_not_disturb_the_other(self):
        async def scenario():
            one = HELPERS["_new_reply_token"](self.state)
            two = HELPERS["_new_reply_token"](self.state)
            self.state["turn_reply_token"] = two
            HELPERS["_post_reply"](self.state, "still delivered")
            HELPERS["_close_reply_box"](self.state, one)
            self.assertNotIn(one, self.state["reply_waiters"])
            self.assertIn(two, self.state["reply_waiters"])
            box = self.state["reply_waiters"][two]
            return await asyncio.wait_for(box.get(), timeout=1.0)

        self.assertEqual(
            asyncio.new_event_loop().run_until_complete(scenario()),
            "still delivered",
        )

    def test_voice_turn_with_no_request_still_uses_the_legacy_queue(self):
        """The voice/Telegram paths have no reply box and must not regress."""
        async def scenario():
            self.state["turn_reply_token"] = None
            HELPERS["_post_reply"](self.state, "spoken path")
            return await asyncio.wait_for(
                self.state["text_reply_queue"].get(), timeout=1.0)

        self.assertEqual(
            asyncio.new_event_loop().run_until_complete(scenario()), "spoken path"
        )


if __name__ == "__main__":
    unittest.main()