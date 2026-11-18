"""Focused startup warmup tests."""

from types import SimpleNamespace

import requests

import main


class FakeResponse:
    def raise_for_status(self):
        return None

    # A real requests.Response always has these. Without them, anything that
    # later inspects a response crashes with AttributeError.
    status_code = 200
    text = ""

    def json(self):
        return {}


calls = []

# FIX (2026-10-01): these used to be bare assignments executed at IMPORT time.
# Because `main.requests` IS the global `requests` module, that permanently
# replaced requests.post/get for the whole pytest session - so every later test
# that made an HTTP call received this FakeResponse. It has no status_code, so
# those tests died with a confusing `AttributeError: 'FakeResponse' object has
# no attribute 'status_code'` that looked like a bug in the code under test.
# It also made the suite order-dependent. Save and restore instead.
# Patched on the `requests` module itself rather than `main.requests`, because
# main.py holds `import requests` at module level and every consumer shares the
# same module object.
_real_post = requests.post
_real_get = requests.get
try:
    requests.post = lambda url, **kwargs: (
        calls.append(("post", url, kwargs)) or FakeResponse()
    )
    requests.get = lambda url, **kwargs: (
        calls.append(("get", url, kwargs)) or FakeResponse()
    )

    interpreter = SimpleNamespace(
        llm=SimpleNamespace(model="ollama_chat/qwen3:8b", api_base="http://ollama"),
        tts="openai",
    )
    main.warmup_services(interpreter)

    # Nothing is warmed at startup: the local TTS server starts lazily (only when
    # both cloud TTS accounts die), so boot stays fast and offline-tolerant.
    assert not any(url == "http://ollama/api/chat" for _, url, _ in calls)
    assert not any("localhost:5050" in url for _, url, _ in calls)
finally:
    requests.post = _real_post
    requests.get = _real_get

print("PASS: no startup warmup at all (local voice boots lazily on fallback)")