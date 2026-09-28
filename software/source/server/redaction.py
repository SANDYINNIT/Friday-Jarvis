"""Deterministic auto-redaction for anything FRIDAY persists or logs.

Call ``redact()`` before writing to memory, audit logs, or transcripts.
Redaction is deliberately conservative: it masks shapes, never rewrites
content, and never runs on the live user<->assistant conversation.
"""

import os
import re

import unicodedata


REDACTED_PATH = "<PATH>"
REDACTED_EMAIL = "<EMAIL>"
REDACTED_PHONE = "<PHONE>"
REDACTED_IP = "<IP>"
REDACTED_CARD = "<CARD>"
REDACTED_TOKEN = "<TOKEN>"
REDACTED_KEY = "<KEY>"


def redaction_enabled():
    configured = os.environ.get("FRIDAY_REDACTION")
    if configured is not None:
        return configured.strip() == "1"
    return True


_BOUNDARY = r"(?<![A-Za-z0-9])"
_TOKEN_CHARS = r"[A-Fa-f0-9+/=_\-]"

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![A-Za-z0-9])"
)
_PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?\d[\d\s().\-]{6,}\d)(?![A-Za-z0-9])"
)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_CARD_RE = re.compile(
    r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"
)
_TOKEN_RE = re.compile(
    _BOUNDARY + _TOKEN_CHARS + r"{" + str(24) + r",}(?![A-Za-z0-9])"
)
_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:sk|pk|gh[pousr]|xox[baprs]|AKIA)[A-Za-z0-9_\-]{8,}|"
    r"eyJ[A-Za-z0-9_\-]{20,})(?![A-Za-z0-9])"
)
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?:[A-Z]:\\|\\\\)[^\s\\]*(?:\\[^\s\\]*)+"
)
_POSIX_PATH_RE = re.compile(
    r"(?:/(?:home|Users|opt|var|etc|usr|root|boot|dev|mnt|media)/[\w.\-/]+)"
)
_SECRET_LABEL_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|auth)")

_PLACEHOLDERS = frozenset(
    {
        REDACTED_PATH,
        REDACTED_EMAIL,
        REDACTED_PHONE,
        REDACTED_IP,
        REDACTED_CARD,
        REDACTED_TOKEN,
        REDACTED_KEY,
    }
)


def _mask(match, replacement):
    """Apply one masking pass without re-matching existing placeholders."""
    text = match.group(0)
    for placeholder in _PLACEHOLDERS:
        if placeholder in text:
            return text
    return replacement


def _redact_paths(text):
    text = _WINDOWS_PATH_RE.sub(lambda m: _mask(m, REDACTED_PATH), text)
    text = _POSIX_PATH_RE.sub(lambda m: _mask(m, REDACTED_PATH), text)
    return text


def redact(text, mask_paths=True):
    """Return ``text`` with sensitive shapes replaced by stable placeholders.

    ``mask_paths=False`` keeps real filesystem paths intact (used for
    owner-bound phone replies where the owner needs the actual path — the
    full masking would turn ``C:\\work\\project\\...`` into ``<PATH>``, making answers
    like "I'm working in C:\\work\\project" useless). Secrets (tokens, keys,
    emails, cards, phone numbers, IPs) are ALWAYS masked either way.
    """
    if not isinstance(text, str) or not text:
        return text
    if not redaction_enabled():
        return text
    text = unicodedata.normalize("NFKC", text)
    text = _EMAIL_RE.sub(lambda m: _mask(m, REDACTED_EMAIL), text)
    text = _IP_RE.sub(lambda m: _mask(m, REDACTED_IP), text)
    text = _PHONE_RE.sub(lambda m: _mask(m, REDACTED_PHONE), text)
    text = _CARD_RE.sub(lambda m: _mask(m, REDACTED_CARD), text)
    text = _KEY_RE.sub(lambda m: _mask(m, REDACTED_KEY), text)
    text = _TOKEN_RE.sub(lambda m: _mask(m, REDACTED_TOKEN), text)
    if mask_paths:
        text = _redact_paths(text)
    return text


def redact_secret_values(payload):
    """Redact common secret-bearing fields in a JSON-shaped structure."""
    if isinstance(payload, dict):
        return {
            key: redact_secret_values(value) if not _SECRET_LABEL_RE.search(str(key)) else REDACTED_TOKEN
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_secret_values(value) for value in payload]
    if isinstance(payload, (tuple, set)):
        return type(payload)(redact_secret_values(value) for value in payload)
    if isinstance(payload, str):
        return redact(payload)
    if payload is None or isinstance(payload, (int, float, bool, bytes)):
        return payload
    try:
        return redact(str(payload))
    except (TypeError, ValueError):
        return str(payload) if payload is not None else None