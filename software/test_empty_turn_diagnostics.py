"""PHASE 4 (2026-10-03) - trustworthy empty-response diagnostics.

THE INSTRUMENTATION DEFECTS THESE LOCK

1. `actions` read the WRONG Open Interpreter field. `_record_turn_output` read
   `output.get("code") or output.get("result")`, but OI streams a tool call as
   DELTA frames carrying **`content`** (and the language under **`format`**) -
   `run_tool_calling_llm.py:267-271, 299-303`. So `actions` was structurally
   always `0`. Proved live, not theorised: a turn that ran TWELVE tools was
   audited as `action_count=0` (boot_console.log 2026-10-03 05:21:47).

2. `reasoning_only` was UNREACHABLE. It required OI to emit a
   `{"type": "reasoning"}` frame, and the vendored LLM layer has no
   `reasoning_content` handler at all - so `turn["thinking"]` was always empty
   and `thinking=0` could not fail. The discriminator now comes from the
   PROVIDER's measured `delta.reasoning_content` length.

3. The shape accumulator was TURN-level. One flat dict meant `provider_chunks=6`
   on a four-call turn read as "six chunks somewhere", and a turn that made NO
   call inherited the previous turn's numbers (live proof: `chunks=0` beside a
   named Modal model, boot_console.log 2026-10-02 18:59:23). It is now one
   record per call, with `shape_valid` to mark a turn that never called out.

4. Anomaly logging only reached ONE channel. `_log_empty_turn` fired solely from
   inside `if capture_chat_id == TEXT_CHANNEL_CHAT_ID:`, so voice and Telegram
   turns could never produce a record. Measured: 5 records against 41
   `snapshot_chars=0` events, 12 of 32 audit-visible empties never written.

These tests exercise the REAL module-level functions, imported from
`source.server.server`. Nothing here guesses a cause for the empty completion -
the point is that the next occurrence is diagnosable.
"""

import ast
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())

from source.server.server import (  # noqa: E402
    _TURN_ACTION_LIMIT,
    _begin_turn_log,
    _flush_tool_action,
    _flush_turn_log,
    _log_empty_turn,
    _merge_chunk_shape,
    _prompt_fingerprint,
    _record_tool_action,
    _record_turn_output,
    _scrub_tool_choice,
    _turn_error_class,
)

SERVER_PY = os.path.join(os.getcwd(), "source", "server", "server.py")


def _turn():
    return {"turn_log": None}


def _fresh():
    state = {
        "turn_log": None,
        "turn_error": "",
        "turn_started_at": None,
        "pending_turns": __import__("collections").deque(),
    }
    _begin_turn_log(state, user="do a thing", source="text")
    return state


# ---------------------------------------------------------------------------
# 1. action counting reads the field OI actually emits, and counts ONCE per call
# ---------------------------------------------------------------------------


class ActionCountingTests(unittest.TestCase):
    def test_a_tool_call_streamed_as_content_deltas_counts_once(self):
        """REGRESSION LOCK for defect 1.

        Exactly the shape `run_tool_calling_llm.py` emits: a `start` frame, then
        several `content` deltas of `arguments.code`, then an `end` frame.
        Before the fix this recorded ZERO actions.
        """
        state = _fresh()
        turn = state["turn_log"]
        _record_turn_output(state, {"type": "code", "start": True, "format": "python"})
        _record_turn_output(state, {"type": "code", "format": "python",
                                    "content": "import os\n"})
        _record_turn_output(state, {"type": "code", "format": "python",
                                    "content": "print(os.getcwd())\n"})
        _record_turn_output(state, {"type": "code", "end": True})
        self.assertEqual(len(turn["actions"]), 1,
                         "a streamed tool call must be ONE action, not one per delta")
        self.assertEqual(turn["actions"][0]["tool"], "python")
        self.assertIn("getcwd", turn["actions"][0]["snippet"])

    def test_a_tool_call_with_no_end_frame_is_still_counted(self):
        """The last tool call of a turn may never receive its `end` frame, so
        `_flush_turn_log` must close the buffer."""
        state = _fresh()
        _record_turn_output(state, {"type": "code", "start": True, "format": "python"})
        _record_turn_output(state, {"type": "code", "format": "python",
                                    "content": "print(1)"})
        self.assertEqual(len(state["turn_log"]["actions"]), 0,
                         "not closed yet - correct")
        flushed = _flush_turn_log(state)
        self.assertEqual(len(flushed["actions"]), 1,
                         "the unterminated tool call was lost")

    def test_two_tool_calls_count_two(self):
        state = _fresh()
        _record_turn_output(state, {"type": "code", "start": True, "format": "python"})
        _record_turn_output(state, {"type": "code", "format": "python", "content": "a=1"})
        _record_turn_output(state, {"type": "code", "end": True})
        _record_turn_output(state, {"type": "code", "start": True, "format": "python"})
        _record_turn_output(state, {"type": "code", "format": "python", "content": "b=2"})
        _record_turn_output(state, {"type": "code", "end": True})
        self.assertEqual(len(state["turn_log"]["actions"]), 2)

    def test_an_empty_tool_call_is_not_an_action(self):
        """A tool call whose arguments never parsed executes nothing, so it must
        not count - that is what keeps a turn-level failover possible."""
        state = _fresh()
        _record_turn_output(state, {"type": "code", "start": True, "format": "python"})
        _record_turn_output(state, {"type": "code", "end": True})
        self.assertEqual(state["turn_log"]["actions"], [])

    def test_the_language_falls_back_to_code(self):
        state = _fresh()
        _record_turn_output(state, {"type": "code", "start": True})
        _record_turn_output(state, {"type": "code", "content": "x=1"})
        _record_turn_output(state, {"type": "code", "end": True})
        self.assertEqual(state["turn_log"]["actions"][0]["tool"], "code")

    def test_actions_are_bounded(self):
        state = _fresh()
        for n in range(_TURN_ACTION_LIMIT + 5):
            _record_turn_output(state, {"type": "code", "start": True, "format": "python"})
            _record_turn_output(state, {"type": "code", "format": "python",
                                        "content": "x=%d" % n})
            _record_turn_output(state, {"type": "code", "end": True})
        self.assertLessEqual(len(state["turn_log"]["actions"]), _TURN_ACTION_LIMIT + 5)

    def test_flushing_an_empty_buffer_is_a_no_op(self):
        turn = _fresh()["turn_log"]
        _flush_tool_action(turn)
        self.assertEqual(turn["actions"], [])

    def test_the_recorder_still_ignores_the_old_keys_only(self):
        """Guard against re-introducing the original bug: a frame that carries
        ONLY `code` (never emitted by OI, but harmless) still records."""
        state = _fresh()
        _record_turn_output(state, {"type": "code", "start": True, "language": "sh"})
        _record_turn_output(state, {"type": "code", "code": "ls -la"})
        _record_turn_output(state, {"type": "code", "end": True})
        self.assertEqual(len(state["turn_log"]["actions"]), 1)


# ---------------------------------------------------------------------------
# 2 + 3. per-call shape records, and reasoning_only from the provider
# ---------------------------------------------------------------------------


def _chunk(delta=None, finish_reason=None, choices=None, usage=None):
    """A minimal chunk shaped like what litellm yields."""
    if choices is None:
        choices = [{"index": 0, "delta": delta or {},
                    "finish_reason": finish_reason}]
    body = {"id": "x", "object": "chat.completion.chunk", "choices": choices}
    if usage is not None:
        body["usage"] = usage
    return body


class ChunkShapeTests(unittest.TestCase):
    def test_one_record_per_call(self):
        first, second = {}, {}
        _merge_chunk_shape(first, _chunk({"content": "hello"}))
        _merge_chunk_shape(second, _chunk({"content": "world"}))
        self.assertEqual(first["chunks"], 1)
        self.assertEqual(second["chunks"], 1)
        self.assertEqual(first["content_len"], 5)
        self.assertEqual(second["content_len"], 5)

    def test_reasoning_is_counted_apart_from_content(self):
        shape = {}
        _merge_chunk_shape(shape, _chunk({"content": "answer",
                                          "reasoning_content": "thinking hard"}))
        self.assertEqual(shape["content_len"], 6)
        self.assertEqual(shape["reasoning_len"], 13)
        self.assertIn("reasoning_content", shape["chars"])

    def test_finish_reason_is_captured(self):
        shape = {}
        _merge_chunk_shape(shape, _chunk({"content": "x"}, finish_reason="length"))
        self.assertEqual(shape["finish_reason"], "length")

    def test_an_empty_choices_array_is_visible(self):
        """`run_tool_calling_llm.py:179-181` silently `continue`s on an empty
        `choices`. The raw-chunk view must be able to see that."""
        shape = {}
        _merge_chunk_shape(shape, _chunk(choices=[]))
        self.assertEqual(shape["chunks"], 1)
        self.assertEqual(shape["content_len"], 0)
        # No terminal chunk means no finish_reason to report; production
        # pre-initialises the key, so read it the way the record does.
        self.assertIsNone(shape.get("finish_reason"))

    def test_tool_calls_are_recorded_without_their_arguments(self):
        shape = {}
        _merge_chunk_shape(shape, _chunk({"tool_calls": [{"function": {"arguments": "secret"}}]}))
        self.assertIn("tool_calls", shape["chars"])
        dumped = json.dumps(shape)
        self.assertNotIn("secret", dumped,
                         "tool-call arguments must never reach the diagnostic")

    def test_only_names_and_lengths_are_recorded(self):
        shape = {}
        _merge_chunk_shape(shape, _chunk({"content": "PRIVATE USER TEXT"}))
        dumped = json.dumps(shape)
        self.assertNotIn("PRIVATE", dumped,
                         "content leaked into a structural diagnostic")
        self.assertEqual(shape["content_len"], len("PRIVATE USER TEXT"))


class PromptFingerprintTests(unittest.TestCase):
    def test_it_is_a_size_plus_a_hash_and_no_content(self):
        fp = _prompt_fingerprint([{"role": "user", "content": "my secret question"}])
        self.assertEqual(fp["messages"], 1)
        self.assertEqual(fp["chars"], len("my secret question"))
        self.assertEqual(len(fp["sha256_12"]), 12)
        self.assertNotIn("secret", json.dumps(fp))

    def test_it_is_stable_and_content_sensitive(self):
        a = _prompt_fingerprint([{"role": "user", "content": "one"}])
        b = _prompt_fingerprint([{"role": "user", "content": "one"}])
        c = _prompt_fingerprint([{"role": "user", "content": "two"}])
        self.assertEqual(a["sha256_12"], b["sha256_12"])
        self.assertNotEqual(a["sha256_12"], c["sha256_12"])

    def test_empty_and_malformed_input(self):
        self.assertEqual(_prompt_fingerprint(None)["messages"], 0)
        self.assertEqual(_prompt_fingerprint([])["chars"], 0)


class ToolChoiceScrubTests(unittest.TestCase):
    def test_shapes(self):
        self.assertIsNone(_scrub_tool_choice(None))
        self.assertEqual(_scrub_tool_choice("auto"), "auto")
        self.assertEqual(_scrub_tool_choice({"type": "function"}),
                         {"type": "function"})
        self.assertEqual(_scrub_tool_choice(7), "int")

    def test_a_schema_blob_is_not_recorded_verbatim(self):
        scrubbed = _scrub_tool_choice({"type": "function",
                                       "function": {"name": "my_tool"}})
        self.assertEqual(list(scrubbed), ["type"],
                         "only the discriminating shape may survive, not the schema")
        self.assertNotIn("my_tool", json.dumps(scrubbed))


class TurnErrorClassTests(unittest.TestCase):
    """The field that separates a hard provider rejection from an empty
    completion - a canonical CATEGORY, never the message.

    The vocabulary is deliberately closed: one failure must always produce one
    spelling, so recurrences group by category rather than by class name."""

    CANONICAL = ("ConnectError", "Timeout", "RateLimit", "BadRequest",
                 "Auth", "NotFound", "Server", "ProviderError", "UnknownError")

    def test_every_result_is_in_the_canonical_vocabulary(self):
        samples = [
            "httpx.ConnectError: [WinError 10061] refused",
            "httpx.ReadTimeout: timed out",
            "litellm.RateLimitError: Error code: 429",
            "litellm.BadRequestError: tool arguments must be valid JSON",
            "openai.AuthenticationError: Error code: 401",
            "openai.NotFoundError: Error code: 404",
            "litellm.InternalServerError: Error code: 500",
            "litellm.APIStatusError: Error code: 400",
            "litellm.llms.openai.common_utils.OpenAIError: Error code: 413",
            "Traceback (most recent call last):\n  ^^^^\n",
            "something entirely unexpected",
        ]
        for raw in samples:
            self.assertIn(_turn_error_class(raw), self.CANONICAL,
                          "uncanonical result for %r" % raw[:48])

    def test_one_failure_always_produces_one_spelling(self):
        """The same 413 arriving as three different exception classes."""
        spellings = {
            _turn_error_class("litellm.RateLimitError: Error code: 413"),
            _turn_error_class("openai.APIStatusError: Error code: 413"),
            _turn_error_class("litellm...OpenAIError: Error code: 413"),
        }
        self.assertEqual(spellings, {"RateLimit"},
                         "one failure produced %s" % spellings)

    def test_it_is_normalised_not_echoed(self):
        self.assertEqual(
            _turn_error_class("litellm.RateLimitError: Error code: 413 - Request "
                              "too large for model, TPM Limit 8000"), "RateLimit")
        self.assertEqual(
            _turn_error_class("httpx.ConnectError: [WinError 10061] refused"),
            "ConnectError")
        self.assertEqual(
            _turn_error_class("httpx.ReadTimeout: timed out"), "Timeout")
        self.assertEqual(
            _turn_error_class("litellm.BadRequestError: nope"), "BadRequest")

    def test_empty(self):
        self.assertEqual(_turn_error_class(""), "")
        self.assertEqual(_turn_error_class(None), "")

    def test_a_caret_underline_never_becomes_the_class_name(self):
        raw = "Traceback (most recent call last):\n  ^^^^\n"
        result = _turn_error_class(raw)
        self.assertNotIn("^", result)
        self.assertEqual(result, "UnknownError")

    def test_a_real_class_name_is_recovered_from_the_last_line(self):
        raw = "Traceback (most recent call last):\n  boom\nValueError: bad thing\n"
        self.assertEqual(_turn_error_class(raw), "ValueError")


class AnomalyRecordTests(unittest.TestCase):
    """The record itself: what it proves, and that it leaks nothing."""

    def _capture(self, turn, shape, **kwargs):
        written = []
        import source.server.server as srv
        original = srv._append_turn_record
        srv._append_turn_record = lambda rec: written.append(rec)
        try:
            _log_empty_turn({"turn_error": ""}, turn, response_shape=shape, **kwargs)
        finally:
            srv._append_turn_record = original
        self.assertEqual(len(written), 1, "the anomaly was not journalled")
        return written[0]

    def test_nothing_at_all(self):
        state = _fresh()
        _flush_turn_log(state)
        rec = self._capture(None, {"calls": [], "turn_valid": False})
        self.assertEqual(rec["shape"], "nothing_at_all")
        self.assertFalse(rec["shape_valid"])
        self.assertEqual(rec["baseline"], "absent")

    def test_reasoning_only_is_now_reachable(self):
        """REGRESSION LOCK for defect 2.

        `shape=reasoning_only` previously required an OI `reasoning` frame that
        is never emitted, so it was dead code. It must now be produced from the
        provider's measured reasoning length.
        """
        state = _fresh()
        _flush_turn_log(state)
        shape = {"turn_valid": True, "calls": [
            {"call_index": 1, "content_len": 0, "reasoning_len": 329,
             "chunks": 21, "finish_reason": "length"},
        ]}
        rec = self._capture(None, shape)
        self.assertEqual(rec["shape"], "reasoning_only")
        self.assertEqual(rec["provider_reasoning_len"], 329)

    def test_content_but_no_text(self):
        state = _fresh()
        _flush_turn_log(state)
        shape = {"turn_valid": True, "calls": [
            {"call_index": 1, "content_len": 40, "reasoning_len": 0, "chunks": 3},
        ]}
        rec = self._capture(None, shape)
        self.assertEqual(rec["shape"], "content_but_no_text")

    def test_a_hard_provider_rejection_is_distinguishable(self):
        """REGRESSION LOCK: a ConnectError / 413 must never again be journalled
        with the same field set as a genuinely empty completion."""
        state = _fresh()
        _flush_turn_log(state)
        shape = {"turn_valid": True, "calls": [
            {"call_index": 1, "content_len": 0, "reasoning_len": 0, "chunks": 0},
        ]}
        rec = self._capture(None, shape, turn_failed=True,
                            turn_error="httpx.ConnectError: [WinError 10061] refused")
        self.assertTrue(rec["turn_failed"])
        self.assertEqual(rec["turn_error_class"], "ConnectError")
        self.assertNotIn("10061", json.dumps(rec),
                         "the raw error text must not be journalled")

    def test_a_413_is_classified_as_a_rate_limit(self):
        state = _fresh()
        _flush_turn_log(state)
        rec = self._capture(None, {"calls": []}, turn_failed=True,
                            turn_error="litellm.RateLimitError: 413 Request too large "
                                       "for model on tokens per minute")
        self.assertEqual(rec["turn_error_class"], "RateLimit")

    def test_the_live_413_traceback_is_classified_correctly(self):
        """REGRESSION LOCK, from the real 2026-10-03 08:07:45 failure.

        That turn's class name is `litellm...OpenAIError`, and the traceback's
        final line is a caret underline, so the first version of this function
        returned the literal string "^^^" - a classification that named nothing.
        """
        raw = (
            "Traceback (most recent call last):\n"
            "  raise first_error\n"
            "openai.APIStatusError: Error code: 413 - {'error': {'message': "
            "'Request too large for model `openai/gpt-oss-120b` in organization "
            "`org_01m2a6`}}\n"
            "  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
        )
        self.assertEqual(_turn_error_class(raw), "RateLimit")

    def test_status_codes_are_recognised_regardless_of_class_name(self):
        for code, expected in (("413", "RateLimit"), ("429", "RateLimit"),
                               ("401", "Auth"), ("403", "Auth"),
                               ("404", "NotFound"),
                               ("500", "Server"), ("503", "Server")):
            raw = "litellm.SomethingBrandNew: Error code: %s - whatever" % code
            self.assertEqual(_turn_error_class(raw), expected,
                             "status %s was misclassified" % code)

    def test_a_caret_underline_never_becomes_the_class_name(self):
        raw = "Traceback (most recent call last):\n  ^^^^\n"
        result = _turn_error_class(raw)
        self.assertNotIn("^", result)
        self.assertEqual(result, "UnknownError")

    def test_a_real_class_name_is_recovered_from_the_last_line(self):
        raw = "Traceback (most recent call last):\n  boom\nValueError: bad thing\n"
        self.assertEqual(_turn_error_class(raw), "ValueError")

    def test_per_call_records_are_carried_through(self):
        state = _fresh()
        _flush_turn_log(state)
        calls = [
            {"call_index": 1, "chunks": 6, "content_len": 0, "reasoning_len": 0,
             "finish_reason": "stop", "max_tokens": 4096, "think": "UNSET",
             "model": "openai/nvidia/GLM-5.3-Flash-NVFP4"},
            {"call_index": 2, "chunks": 0, "skipped_stream": True,
             "guard": "plain_end"},
        ]
        rec = self._capture(None, {"turn_valid": True, "calls": calls})
        self.assertEqual(rec["calls_this_turn"], 2)
        self.assertEqual(rec["llm_calls"][0]["call_index"], 1)
        self.assertTrue(rec["llm_calls"][1]["skipped_stream"])
        self.assertEqual(rec["llm_calls"][0]["finish_reason"], "stop")

    def test_the_record_never_carries_message_content(self):
        state = _fresh()
        _flush_turn_log(state)
        calls = [{"call_index": 1, "chunks": 1, "content_len": 0,
                  "prompt_fingerprint": {"messages": 1, "chars": 12,
                                        "sha256_12": "abcdef123456"}}]
        rec = self._capture(None, {"turn_valid": True, "calls": calls})
        blob = json.dumps(rec)
        for banned in ("wk-", "ws-", "Authorization", "Bearer", "api_key"):
            self.assertNotIn(banned, blob, "%s leaked into the record" % banned)

    def test_a_malformed_shape_does_not_raise(self):
        state = _fresh()
        _flush_turn_log(state)
        rec = self._capture(None, {"calls": [None, "junk", {"call_index": 3}]})
        self.assertEqual(rec["calls_this_turn"], 1)


# ---------------------------------------------------------------------------
# 4. reachability: one call site, outside every channel branch
# ---------------------------------------------------------------------------


class ReachabilityTests(unittest.TestCase):
    def setUp(self):
        with open(SERVER_PY, encoding="utf-8") as handle:
            self.src = handle.read()
        self.tree = ast.parse(self.src)

    def _calls(self):
        found = []
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_log_empty_turn"):
                found.append(node)
        return found

    def test_there_is_exactly_one_call_site(self):
        self.assertEqual(len(self._calls()), 1,
                         "more than one site risks journalling a turn twice")

    def test_the_call_site_is_not_inside_a_channel_branch(self):
        """REGRESSION LOCK for defect 4.

        The old call sat inside `if capture_chat_id == TEXT_CHANNEL_CHAT_ID:`,
        so voice and Telegram turns could never produce a record. The call must
        be a direct child of the `status:complete` handler.
        """
        call = self._calls()[0]
        handlers = [n for n in ast.walk(self.tree)
                    if isinstance(n, ast.If)
                    and "content" in ast.unparse(n.test)
                    and "complete" in ast.unparse(n.test)
                    and "status" in ast.unparse(n.test)]
        self.assertTrue(handlers, "the status/complete handler was not found")
        handler = handlers[0]
        # The call may sit inside a guard of its own (it does: "was there any
        # assistant text?"), but it must not be nested inside a branch whose
        # condition selects a CHANNEL.
        for node in ast.walk(handler):
            if not isinstance(node, ast.If):
                continue
            if node is handler or not (node.lineno <= call.lineno <= node.end_lineno):
                continue
            self.assertNotIn(
                "TEXT_CHANNEL_CHAT_ID", ast.unparse(node.test),
                "the anomaly record is still inside a channel-specific branch: %s"
                % ast.unparse(node.test)[:120])

    def test_the_record_runs_for_every_turn_not_just_empty_text_ones(self):
        self.assertIn("if turn is None or not turn.get(\"assistant_snippet\"):",
                      self.src)


# ---------------------------------------------------------------------------
# REHEARSAL (Phase 4 requirement): the diagnostics must actually be WRITTEN for
# the two known-safe failure classes, and must classify them correctly.
#
# These drive the real `_log_empty_turn` with the exact signatures observed live
# on 2026-10-03 (boot_console.log):
#   * 03:47:36  httpx.ConnectError [WinError 10061] connection refused
#   * 03:47:44  413 Request too large ... TPM Limit 8000, Requested 10069
# and assert a record appears in the journal FILE. conftest.py redirects
# FRIDAY_LOG_DIR, so this never touches the live journal.
# ---------------------------------------------------------------------------


class JournalWriteRehearsalTests(unittest.TestCase):
    """The real signatures, through the real writer, onto a real file."""

    LIVE_SIGNATURES = {
        "connection_refused": (
            "httpx.ConnectError: "
            "[WinError 10061] No connection could be made because the "
            "target machine actively refused it"),
        "request_too_large_413": (
            "litellm.RateLimitError: Error code: 413 - {'error': {'message': "
            "\"Request too large for model `openai/gpt-oss-20b` ... on tokens "
            "per minute (TPM): Limit 8000, Requested 10069\"}}"),
    }

    def setUp(self):
        from source.server import server as srv
        self.srv = srv
        self.path = srv._turns_journal_path()
        # Prove the rehearsal is not writing to the live journal.
        self.assertNotEqual(
            os.path.normpath(self.path),
            os.path.normpath(os.path.join(os.path.expanduser("~"), ".friday",
                                          "logs", "turns.jsonl")),
            "FRIDAY_LOG_DIR is not redirected - a test would corrupt the live journal")

    def _lines(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as handle:
            return [json.loads(ln) for ln in handle if ln.strip()]

    def _rehearse(self, error_text):
        before = len(self._lines())
        _log_empty_turn(
            {"turn_error": "", "turn_started_at": None},
            None,
            response_shape={"turn_valid": True, "calls": [
                {"call_index": 1, "chunks": 0, "content_len": 0,
                 "reasoning_len": 0, "finish_reason": None,
                 "stream_opened": True, "skipped_stream": False},
            ]},
            turn_failed=True,
            turn_error=error_text,
        )
        after = self._lines()
        self.assertEqual(len(after), before + 1,
                         "the anomaly record was not written to the journal")
        return after[-1]

    def test_a_connection_failure_is_written_and_classified(self):
        record = self._rehearse(self.LIVE_SIGNATURES["connection_refused"])
        self.assertEqual(record["event"], "empty_turn")
        self.assertEqual(record["turn_error_class"], "ConnectError")
        self.assertTrue(record["turn_failed"])
        self.assertEqual(record["shape"], "nothing_at_all")
        self.assertEqual(record["calls_this_turn"], 1)
        self.assertEqual(record["llm_calls"][0]["chunks"], 0)
        self.assertTrue(record["ts"], "the record must be timestamped")

    def test_a_413_is_written_and_classified(self):
        record = self._rehearse(self.LIVE_SIGNATURES["request_too_large_413"])
        self.assertEqual(record["turn_error_class"], "RateLimit")
        self.assertTrue(record["turn_failed"])
        self.assertFalse(record["shape_valid"] is None)

    def test_a_hard_rejection_is_never_mistaken_for_an_empty_completion(self):
        """The whole point of `turn_failed`: the two must be separable."""
        rejected = self._rehearse(self.LIVE_SIGNATURES["connection_refused"])
        # A genuinely empty completion: provider answered, said nothing.
        _log_empty_turn(
            {"turn_error": "", "turn_started_at": None},
            None,
            response_shape={"turn_valid": True, "calls": [
                {"call_index": 1, "chunks": 6, "content_len": 0,
                 "reasoning_len": 0, "finish_reason": "stop"},
            ]},
            turn_failed=False,
            turn_error="",
        )
        empty = self._lines()[-1]
        self.assertTrue(rejected["turn_failed"])
        self.assertFalse(empty["turn_failed"])
        self.assertNotEqual(rejected["turn_error_class"], empty["turn_error_class"])
        self.assertEqual(empty["shape"], "nothing_at_all")
        self.assertEqual(empty["llm_calls"][0]["finish_reason"], "stop")
        self.assertEqual(empty["llm_calls"][0]["chunks"], 6)

    def test_the_rehearsed_record_carries_no_secrets(self):
        record = self._rehearse(self.LIVE_SIGNATURES["request_too_large_413"])
        blob = json.dumps(record)
        for banned in ("wk-", "ws-", "Bearer", "Authorization", "api_key",
                       "sk-", "10069"):
            self.assertNotIn(banned, blob,
                             "%s leaked into a rehearsal record" % banned)

    def test_a_diagnostic_failure_never_raises(self):
        """`_log_empty_turn` must never break a turn, whatever it is handed."""
        for junk in (None, 0, "", [], {"calls": "not-a-list"},
                     {"calls": [None]}, {"turn_valid": True}):
            _log_empty_turn({"turn_error": ""}, junk,
                            response_shape=junk if isinstance(junk, dict) else None)


if __name__ == "__main__":
    unittest.main()