"""WP6: intent judge client, JSON extraction, and model selection."""
import os

from source.server.intent_judge import JudgeClient, _extract_json, fast_judge_model


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakePost:
    def __init__(self, result):
        self._result = result
        self.calls = []
        self.saw_timeout = False

    def __call__(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        return FakeResponse({"message": {"content": self._result}})


def test_extract_json_with_fences():
    content = '```json\n{"directed": true, "query": "hi"}\n```'
    assert _extract_json(content) == {"directed": True, "query": "hi"}
    assert _extract_json("not json") is None


def test_judge_client_classifies(monkeypatch):
    post = FakePost('{"directed": true, "query": "what is the plan for the picnic", "reason": "ok"}')
    monkeypatch.setattr("httpx.post", post)
    client = JudgeClient(base_url="http://test:11434", model="fake:1", timeout=1.0)
    result = client.classify("what do you think", "we were planning the picnic")
    assert result["directed"] is True
    assert "picnic" in result["query"]
    url, payload, timeout = post.calls[0]
    assert url == "http://test:11434/api/chat"
    assert payload["model"] == "fake:1"


def test_judge_client_returns_none_on_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("httpx.post", boom)
    client = JudgeClient(base_url="http://test:11434", model="fake:1", timeout=0.1)
    assert client.classify("hi", "ambient") is None


def test_fast_judge_model_prefers_env(monkeypatch):
    monkeypatch.setenv("FRIDAY_FAST_MODEL", "ollama_chat/fastest:1")
    assert fast_judge_model() == "ollama_chat/fastest:1"