"""WP8: background small-model digest passes and planner tests."""
import os

from source.server import digest


class FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": self._content}}


class FakePost:
    def __init__(self, content):
        self._content = content
        self.passed = None

    def __call__(self, url, json=None, timeout=None):
        self.passed = (url, json, timeout)
        return FakeResponse(self._content)


def test_summarize_turns_uses_small_model(monkeypatch):
    post = FakePost("The user asked the assistant to plan a picnic.")
    monkeypatch.setattr("httpx.post", post)
    turns = [{"role": "user", "content": "plan the picnic"}, {"role": "assistant", "content": "here is a plan"}]
    summary = digest.summarize_turns(turns, model="qwen2.5:0.5b", base_url="http://local:11434")
    assert summary == "The user asked the assistant to plan a picnic."
    url, payload, _ = post.passed
    assert url == "http://local:11434/api/chat"
    assert payload["model"] == "qwen2.5:0.5b"


def test_summarize_turns_short_context_skips():
    assert digest.summarize_turns([], model="x") is None
    assert digest.summarize_turns([{"role": "user", "content": "hi"}], model="x") is None


def test_summarize_turns_fails_open(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("httpx.post", boom)
    assert digest.summarize_turns([{"role": "user", "content": "a long enough question with detail"}], model="x") is None


def test_plan_turns_returns_steps(monkeypatch):
    post = FakePost('{"steps": ["step one", "step two"]}')
    monkeypatch.setattr("httpx.post", post)
    plan = digest.plan_turns([{"role": "user", "content": "move the files"}])
    assert plan is not None
    assert plan.startswith("Plan:")
    assert "step one" in plan


def test_plan_turns_fails_open(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    assert digest.plan_turns([{"role": "user", "content": "enough detail here to matter"}]) is None


def test_digest_model_env(monkeypatch):
    monkeypatch.setenv("FRIDAY_DIGEST_MODEL", "custom-tiny:1")
    assert digest.digest_model() == "custom-tiny:1"


def test_maybe_run_digest_guards():
    server_state = {"memory_store": None, "is_paused": False}
    assert digest.maybe_run_digest(server_state, status_bus=None) is None


def test_digest_enabled_env(monkeypatch):
    monkeypatch.setenv("FRIDAY_DIGEST", "0")
    assert digest.digest_enabled() is False
    monkeypatch.delenv("FRIDAY_DIGEST")
    assert digest.digest_enabled() is True