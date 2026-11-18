"""Structural, content-free description of provider stream chunks.

Why this exists (2026-10-03)
---------------------------
The first version of the empty-turn diagnostic recorded only three hard-coded
field names read via `.get()`. A live structural probe of the primary provider
showed why that was not enough:

* the chunk is a LiteLLM **`ModelResponseStream` OBJECT**, not a dict, so
  duck-typed `.get()` can fail silently and record zeros for a perfectly healthy
  call;
* `usage` is **never present** in this provider's stream - null even on
  successful calls - so its absence carries no information at all;
* the answer arrives in `delta.content` while thinking arrives separately in
  `delta.reasoning_content`, so the two must be counted independently.

These tests pin the three modes that must be told apart, and guarantee that no
content, credential or payload is ever retained.
"""

import os
import sys
import unittest

sys.path.insert(0, os.getcwd())

from source.server.server import _append_turn_record, _describe_chunk, \
    _merge_chunk_shape, _turns_journal_path


class _Obj:
    """Mimics LiteLLM's ModelResponseStream: an object exposing model_dump()."""

    def __init__(self, choices, usage=None):
        self._payload = {
            "id": "x", "created": 1, "model": "m",
            "object": "chat.completion.chunk",
            "choices": choices, "usage": usage,
        }

    def model_dump(self):
        return self._payload


def _chunk(delta, finish=None, usage=None):
    return _Obj([{"index": 0, "finish_reason": finish, "delta": delta}], usage)


class DescribeChunkTests(unittest.TestCase):
    def test_object_chunks_are_parsed_via_model_dump(self):
        info = _describe_chunk(_chunk({"content": "seven"}))
        self.assertEqual(info["parse"], "model_dump")
        self.assertEqual(info["chars"], {"content": 5})

    def test_plain_dict_chunks_still_work(self):
        info = _describe_chunk(
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]})
        self.assertEqual(info["parse"], "dict")
        self.assertEqual(info["chars"], {"content": 2})
        self.assertEqual(info["finish_reason"], "stop")

    def test_content_and_reasoning_are_counted_separately(self):
        reasoning = "11 chars"
        info = _describe_chunk(
            _chunk({"content": "seven", "reasoning_content": reasoning}))
        self.assertEqual(info["chars"]["content"], 5)
        self.assertEqual(info["chars"]["reasoning_content"], len(reasoning))

    def test_none_fields_are_recorded_as_present_but_empty(self):
        """The whole point: we can tell 'field absent' from 'field empty'."""
        info = _describe_chunk(
            _chunk({"content": None, "reasoning_content": None}))
        self.assertIn("content", info["delta"])
        self.assertIn("reasoning_content", info["delta"])
        self.assertEqual(info["chars"], {})

    def test_usage_absent_is_recorded_as_none_not_as_a_failure(self):
        self.assertIsNone(_describe_chunk(_chunk({"content": "x"}))["usage"])

    def test_usage_field_names_are_captured_when_present(self):
        info = _describe_chunk(_chunk({"content": "x"},
                                      usage={"total_tokens": 42}))
        self.assertIn("total_tokens", info["usage"])

    def test_tool_calls_are_counted_never_dumped(self):
        info = _describe_chunk(_chunk(
            {"tool_calls": [{"function": {"arguments": '{"path": "secret"}'}}]}))
        self.assertEqual(info["chars"].get("tool_calls"), 1)
        self.assertNotIn("secret", repr(info))

    def test_hostile_chunks_never_raise(self):
        for bad in (None, "a string", 42, {"choices": "not a list"},
                    {"choices": [None]}, {"choices": [{"delta": "nope"}]}):
            self.assertIsInstance(_describe_chunk(bad), dict)


class AccumulateTests(unittest.TestCase):
    def _drain(self, chunks):
        acc = {}
        for chunk in chunks:
            _merge_chunk_shape(acc, chunk)
        return acc

    def test_healthy_stream(self):
        acc = self._drain([
            _chunk({"role": "assistant", "content": None,
                    "reasoning_content": "thinking..."}),
            _chunk({"content": "seven"}),
            _chunk({"content": None}, finish="stop"),
        ])
        self.assertEqual(acc["chunks"], 3)
        self.assertEqual(acc["content_len"], 5)
        self.assertEqual(acc["reasoning_len"], 11)
        self.assertEqual(acc["finish_reason"], "stop")

    def test_the_anomaly_is_distinguishable_from_a_healthy_call(self):
        """Normal stop, fields present, zero text - now provable, not inferred."""
        acc = self._drain([
            _chunk({"role": "assistant", "content": None,
                    "reasoning_content": None}),
            _chunk({}),
            _chunk({}, finish="stop"),
        ])
        self.assertEqual(acc["content_len"], 0)
        self.assertEqual(acc["reasoning_len"], 0)
        self.assertEqual(acc["finish_reason"], "stop")
        self.assertIn("content", acc["delta"],
                      "proves the field existed and was empty")

    def test_budget_exhaustion_is_distinguishable_from_the_anomaly(self):
        acc = self._drain([
            _chunk({"reasoning_content": "lots of thinking"}),
            _chunk({}, finish="length"),
        ])
        self.assertEqual(acc["finish_reason"], "length")
        self.assertGreater(acc["reasoning_len"], 0)
        self.assertEqual(acc["content_len"], 0)

    def test_chunk_type_and_field_names_are_retained(self):
        acc = self._drain([_chunk({"content": "x"})])
        self.assertEqual(acc["chunk_type"], "_Obj")
        self.assertIn("object", acc["top"])
        self.assertIn("delta", acc["choice"])
        self.assertIn("content", acc["delta"])

    def test_no_content_is_ever_retained(self):
        acc = self._drain([_chunk({"content": "SUPERSECRETANSWER"})])
        self.assertNotIn("SUPERSECRETANSWER", repr(acc))
        self.assertEqual(acc["chars"]["content"], len("SUPERSECRETANSWER"))


class TurnsJournalTests(unittest.TestCase):
    def test_journal_is_append_only_and_honours_the_env_override(self):
        import tempfile

        previous = os.environ.get("FRIDAY_LOG_DIR")
        os.environ["FRIDAY_LOG_DIR"] = tempfile.mkdtemp(prefix="friday-turns-test-")
        try:
            self.assertIn("friday-turns-test-", _turns_journal_path())
            _append_turn_record({"event": "empty_turn", "n": 1})
            _append_turn_record({"event": "empty_turn", "n": 2})
            path = _turns_journal_path()
            with open(path, encoding="utf-8") as handle:
                lines = [x for x in handle.read().splitlines() if x.strip()]
            self.assertEqual(len(lines), 2, "second write must not truncate")
        finally:
            if previous is None:
                os.environ.pop("FRIDAY_LOG_DIR", None)
            else:
                os.environ["FRIDAY_LOG_DIR"] = previous

    def test_a_broken_record_never_raises(self):
        original = os.environ.get("FRIDAY_LOG_DIR")
        os.environ["FRIDAY_LOG_DIR"] = "\\\\?\\invalid\\path\\nope"
        try:
            self.assertFalse(_append_turn_record({"event": "empty_turn"}))
        finally:
            if original is None:
                os.environ.pop("FRIDAY_LOG_DIR", None)
            else:
                os.environ["FRIDAY_LOG_DIR"] = original


if __name__ == "__main__":
    unittest.main()