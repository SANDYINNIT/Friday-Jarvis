"""Ambient-context buffer and LLM intent judge for FRIDAY.

When the user speaks without the wake word, FRIDAY keeps a short rolling
window of that speech instead of discarding it.  When a wake word *does*
arrive, the judge asks a small local model whether the utterance is a
directed request and, if so, lets it fold nearby ambient context into the
query.  The judge only runs when ambient context exists, is bounded by a
timeout, and falls back to the raw query on any failure.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque


AMBIENT_WINDOW_SECONDS = float(os.environ.get("FRIDAY_AMBIENT_SECONDS", "120"))
MAX_AMBIENT_ENTRIES = 12
AMBIENT_JOIN_MAX_CHARS = 2000

_OLLAMA_BASE = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
_JUDGE_TIMEOUT_SECONDS = float(os.environ.get("FRIDAY_JUDGE_TIMEOUT", "6"))


def judge_enabled():
    configured = os.environ.get("FRIDAY_INTENT_JUDGE")
    if configured is not None:
        return configured.strip() == "1"
    return True


class AmbientBuffer:
    """A locking, windowed, bounded store of non-wake speech."""

    def __init__(self, window_seconds=AMBIENT_WINDOW_SECONDS, max_entries=MAX_AMBIENT_ENTRIES, now_fn=None):
        self.window_seconds = float(window_seconds)
        self.max_entries = int(max_entries)
        self._now_fn = now_fn or time.monotonic
        self._entries = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _is_usable(text):
        return bool(re.search(r"[\w]{2,}", str(text or "")))

    def add(self, text, now=None):
        """Record ambient speech; drop anything outside the window."""
        if not AmbientBuffer._is_usable(text):
            return False
        if now is None:
            now = self._now_fn()
        with self._lock:
            self._prune(now)
            self._entries.append((now, str(text)))
            while len(self._entries) > self.max_entries:
                self._entries.popleft()
            return True

    def _prune(self, now):
        while self._entries and now - self._entries[0][0] > self.window_seconds:
            self._entries.popleft()

    def recent(self, seconds=None, max_chars=AMBIENT_JOIN_MAX_CHARS):
        """Return joined ambient text within ``seconds``, bounded in size."""
        if seconds is None:
            seconds = self.window_seconds
        now = self._now_fn()
        with self._lock:
            self._prune(now)
            lines = [line for ts, line in self._entries if now - ts <= seconds]
        if not lines:
            return ""
        joined = " | ".join(lines)
        return joined[:max_chars]

    def count(self):
        with self._lock:
            return len(self._entries)


JUDGE_SYSTEM = (
    "You are the wake-word intent judge for a voice assistant. "
    "A user just used the wake word. You also overheard ambient speech "
    "spoken shortly before it. Decide whether the wake utterance is a "
    "directed request to the assistant. If yes, return it rewritten to "
    "include any relevant ambient context; if it is self-talk, noise, or "
    "addressed to no one, return directed=false. Reply with strict JSON "
    "only: {\"directed\": true|false, \"query\": \"...\", \"reason\": \"...\"}."
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(content):
    if not isinstance(content, str):
        return None
    match = _JSON_OBJECT_RE.search(content)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def fast_judge_model():
    """Pick the cheapest local model capable of a quick JSON judgement."""
    configured = os.environ.get("FRIDAY_FAST_MODEL", "").strip()
    if configured:
        return configured
    try:
        from .model_registry import FAST, select_model

        model = select_model(FAST)
        if model is not None and not model.known_timeout:
            return model.name
    except Exception:
        pass
    return "llama3.2:3b"


class JudgeClient:
    """Minimal offline Ollama client; injectable for deterministic tests."""

    def __init__(self, base_url=_OLLAMA_BASE, model=None, timeout=_JUDGE_TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.model = model or fast_judge_model()
        self.timeout = float(timeout)

    def classify(self, query, ambient, max_chars=AMBIENT_JOIN_MAX_CHARS):
        """Return an intent dict or ``None`` on any failure."""
        ambient = str(ambient or "").strip()[:max_chars]
        query = str(query or "").strip()
        if not query:
            return None
        user_prompt = "Ambient speech: " + (ambient or "(none)") + "\nWake utterance: " + query
        try:
            import httpx

            response = httpx.post(
                self.base_url + "/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                    "format": "json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
            content = body.get("message", {}).get("content", "")
        except Exception:
            return None
        payload = _extract_json(content)
        if not payload:
            return None
        directed = payload.get("directed") is True
        query_out = str(payload.get("query") or "").strip()
        return {
            "directed": directed,
            "query": (query_out or query) if directed else query,
            "reason": str(payload.get("reason") or ""),
            "model": self.model,
        }


def judge(query, ambient, *, client=None, enabled=None):
    """Top-level helper: return judge output or a safe fallback dict."""
    if enabled is None:
        enabled = judge_enabled()
    if not enabled or not str(query or "").strip():
        return {"directed": True, "query": str(query or ""), "reason": "disabled-or-empty"}
    if not str(ambient or "").strip():
        return {"directed": True, "query": str(query or ""), "reason": "no-ambient-context"}
    client = client or JudgeClient()
    result = client.classify(query, ambient)
    if result is None:
        return {"directed": True, "query": str(query or ""), "reason": "judge-unavailable"}
    return result