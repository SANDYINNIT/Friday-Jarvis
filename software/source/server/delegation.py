"""Explicit, provider-neutral AI delegation boundary.

This module is intentionally independent of the voice server.  Callers must
construct a request and choose a configured provider; no conversation history,
tools, retries, or user-facing output are handled here.
"""

from dataclasses import dataclass
import os
import re
import time

import requests


MAX_CONTEXT_CHARS = 12000
MAX_RESULT_CHARS = 12000
MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_OLLAMA_URL = "http://localhost:11434"


@dataclass(frozen=True)
class DelegationRequest:
    provider: str
    model: str
    task: str
    context: str = ""
    expected_output: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self):
        for field in ("provider", "model", "task", "context", "expected_output"):
            value = getattr(self, field)
            if not isinstance(value, str):
                raise TypeError(f"{field} must be a string")
        if not self.provider.strip() or not self.model.strip() or not self.task.strip():
            raise ValueError("provider, model, and task are required")
        try:
            timeout = float(self.timeout)
        except (TypeError, ValueError) as error:
            raise ValueError("timeout must be a number") from error
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "task", self.task.strip())
        object.__setattr__(self, "context", self.context[:MAX_CONTEXT_CHARS])
        object.__setattr__(self, "expected_output", self.expected_output[:MAX_CONTEXT_CHARS])
        object.__setattr__(self, "timeout", min(timeout, MAX_TIMEOUT_SECONDS))


@dataclass(frozen=True)
class DelegationResult:
    provider: str
    model: str
    success: bool
    text: str = ""
    error: str = ""
    latency: float = 0.0
    source: str = ""


_SECRET_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED TOKEN]"),
)


def redact_secrets(value):
    """Redact common credential forms without pretending to be a DLP system."""
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _result(request, started, success=False, text="", error="", source=""):
    return DelegationResult(
        provider=request.provider,
        model=request.model,
        success=success,
        text=redact_secrets(text)[:MAX_RESULT_CHARS],
        error=redact_secrets(error)[:MAX_RESULT_CHARS],
        latency=max(0.0, time.monotonic() - started),
        source=source or request.provider,
    )


def _messages(request):
    prompt = "Task:\n" + redact_secrets(request.task)
    if request.expected_output:
        prompt += "\n\nExpected output:\n" + redact_secrets(request.expected_output)
    if request.context:
        prompt += "\n\nRelevant context:\n" + redact_secrets(request.context)
    return [{"role": "user", "content": prompt}]


def _ollama(request, started):
    base_url = os.environ.get("FRIDAY_DELEGATION_OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")
    try:
        response = requests.post(
            base_url + "/api/chat",
            json={"model": request.model, "messages": _messages(request), "stream": False},
            timeout=request.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        text = payload.get("message", {}).get("content", "")
        if not isinstance(text, str):
            raise ValueError("Ollama response did not contain message.content")
        return _result(request, started, success=True, text=text, source="ollama")
    except (requests.Timeout, TimeoutError):
        return _result(request, started, error="Ollama delegation timed out", source="ollama")
    except Exception as error:
        return _result(request, started, error=f"Ollama delegation failed: {error}", source="ollama")


def _openai_compatible(request, started):
    base_url = os.environ.get("FRIDAY_DELEGATION_BASE_URL", "").rstrip("/")
    if not base_url:
        return _result(request, started, error="OpenAI-compatible delegation is not configured", source="openai-compatible")
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("FRIDAY_DELEGATION_API_KEY", "")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    try:
        response = requests.post(
            base_url + "/chat/completions",
            headers=headers,
            json={"model": request.model, "messages": _messages(request), "stream": False},
            timeout=request.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        text = payload.get("choices", [])[0].get("message", {}).get("content", "")
        if not isinstance(text, str):
            raise ValueError("OpenAI-compatible response did not contain message.content")
        return _result(request, started, success=True, text=text, source="openai-compatible")
    except (requests.Timeout, TimeoutError):
        return _result(request, started, error="OpenAI-compatible delegation timed out", source="openai-compatible")
    except Exception as error:
        return _result(request, started, error=f"OpenAI-compatible delegation failed: {error}", source="openai-compatible")


def delegate(request):
    """Execute one explicit delegation request, returning a truthful result."""
    if not isinstance(request, DelegationRequest):
        raise TypeError("delegate() requires a DelegationRequest")
    started = time.monotonic()
    if request.provider == "ollama":
        return _ollama(request, started)
    if request.provider in {"openai", "openai-compatible", "openai_compatible"}:
        return _openai_compatible(request, started)
    return _result(request, started, error=f"Unknown or unconfigured provider: {request.provider}")


def second_opinion(task, context="", provider="ollama", model="qwen3:8b", expected_output="", timeout=DEFAULT_TIMEOUT_SECONDS):
    """Convenience wrapper for an explicitly requested independent opinion."""
    return delegate(DelegationRequest(provider, model, task, context, expected_output, timeout))
