"""Focused startup warmup tests."""

from types import SimpleNamespace

import main


class FakeResponse:
    def raise_for_status(self):
        return None


calls = []
main.requests.post = lambda url, **kwargs: (calls.append(("post", url, kwargs)) or FakeResponse())
main.requests.get = lambda url, **kwargs: (calls.append(("get", url, kwargs)) or FakeResponse())

interpreter = SimpleNamespace(
    llm=SimpleNamespace(model="ollama_chat/qwen3:8b", api_base="http://ollama"),
    tts="openai",
)
main.warmup_services(interpreter)

# Nothing is warmed at startup: the local TTS server starts lazily (only when
# both cloud TTS accounts die), so boot stays fast and offline-tolerant.
assert not any(url == "http://ollama/api/chat" for _, url, _ in calls)
assert not any("localhost:5050" in url for _, url, _ in calls)
print("PASS: no startup warmup at all (local voice boots lazily on fallback)")
