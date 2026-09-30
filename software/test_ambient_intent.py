"""WP6: ambient buffer windows, pruning, and safe fallback."""
import os

from source.server.intent_judge import AmbientBuffer, judge, judge_enabled


def test_add_records_ambient_speech():
    buffer = AmbientBuffer()
    assert buffer.add("the garden looks nice") is True
    assert buffer.count() == 1


def test_prune_drops_entries_outside_window():
    buffer = AmbientBuffer(window_seconds=10.0, now_fn=lambda: 121.0)
    buffer.add("old talk", now=110.0)
    buffer.add("new talk", now=115.0)
    assert buffer.count() == 2
    buffer._prune(now=121.0)
    assert buffer.count() == 1
    assert buffer.recent() == "new talk"


def test_recent_respects_seconds_window():
    buffer = AmbientBuffer(window_seconds=10.0, now_fn=lambda: 118.0)
    buffer.add("old talk", now=100.0)
    buffer.add("new talk", now=118.0)
    assert buffer.recent(seconds=2.0) == "new talk"
    assert buffer.recent(seconds=5.0) == "new talk"


def test_short_noise_is_not_ambient():
    buffer = AmbientBuffer()
    assert buffer.add("") is False
    assert buffer.add("a") is False
    assert buffer.count() == 0


def test_judge_falls_back_when_disabled():
    os.environ["FRIDAY_INTENT_JUDGE"] = "0"
    try:
        assert judge_enabled() is False
        result = judge("what time is it", "the garden needs water")
        assert result["directed"] is True
        assert result["query"] == "what time is it"
        assert result["reason"] == "disabled-or-empty"
    finally:
        del os.environ["FRIDAY_INTENT_JUDGE"]


def test_judge_falls_back_without_ambient():
    result = judge("what time is it", "", enabled=None)
    assert result["directed"] is True
    assert result["reason"] == "no-ambient-context"


def test_judge_returns_client_result():
    class FakeClient:
        def classify(self, query, ambient):
            return {"directed": True, "query": "rewritten query", "reason": "ok", "model": "fake"}

    result = judge("why", "some ambient", client=FakeClient())
    assert result["query"] == "rewritten query"


class FakeEmptyClient:
    def classify(self, query, ambient):
        return None


def test_judge_falls_back_when_client_fails():
    result = judge("why", "some ambient", client=FakeEmptyClient())
    assert result["directed"] is True
    assert result["query"] == "why"
    assert result["reason"] == "judge-unavailable"