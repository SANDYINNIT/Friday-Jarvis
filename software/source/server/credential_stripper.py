"""Advanced credential and token stripper ported from OpenJarvis-main security.

Ensures API keys, tokens, auth headers, and secrets never leak into plaintext
file logs, telemetry, or disk storage.
"""

from __future__ import annotations

import re
from typing import List, Tuple

_ADVANCED_CREDENTIAL_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"sk-[a-zA-Z0-9_-]{20,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[a-zA-Z0-9]{36}")),
    ("slack_token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]+")),
    ("bearer_token", re.compile(r"Bearer\s+[a-zA-Z0-9_\-.]{20,}", re.IGNORECASE)),
    ("jwt_token", re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA|DSA|EC|OPENSSH) PRIVATE KEY-----")),
    # 2026-10-01: three real secret shapes were missing here. Found by a test,
    # not by inspection: `AIza...` was written to the live memory DB IN
    # PLAINTEXT (the extractor rejected it, but `MemoryStore.remember()` with an
    # explicit key went straight past the extractor and the stripper had no
    # pattern for it). The Modal combined proxy token `wk-<id>.ws-<secret>` is
    # FRIDAY's own primary credential - it must never reach disk or a log.
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{20,}")),
    ("modal_proxy_token", re.compile(r"wk-[A-Za-z0-9]{4,}\.ws-[A-Za-z0-9]{4,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_\-]{25,}")),
    ("openrouter_key", re.compile(r"sk-or-v1-[a-fA-F0-9]{32,}")),
]


class AdvancedCredentialStripper:
    """Multi-pattern credential stripper for secure logging and telemetry."""

    def __init__(self) -> None:
        self._patterns = _ADVANCED_CREDENTIAL_PATTERNS

    def strip(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return ""
        for label, pattern in self._patterns:
            text = pattern.sub(f"[REDACTED:{label.upper()}]", text)
        return text


def redact_for_storage(text) -> str:
    """Return `text` with every known secret shape removed. THE chokepoint.

    2026-10-02 audit (D2). The SQLite memory store redacted on write, but the
    DURABLE journals did not: `self_improve.append_outcome` wrote the request
    and FRIDAY's own answer verbatim, and all three of its call sites in
    server.py passed unredacted text. So a key spoken aloud, or pasted for her
    to repeat, was written to `outcomes.md` in plaintext - a memory store with
    no secret handling at all, while the database right next to it had it.

    Two layers, and the ORDER matters:
      * `AdvancedCredentialStripper` knows the exact vendor shapes
        (AIza..., wk-<id>.ws-<secret>, AKIA..., sk-..., gh*_..., xox*-...).
      * `redaction.redact` then catches the generic residue (emails, cards,
        IPs, phone numbers, long hex/base64 blobs).
    Stripping first is what makes the Telegram bot token work: `redact()` alone
    rewrites the numeric chat id to `<PHONE>` and leaves the secret tail
    behind, whereas the stripper matches the whole `123456789:AAH...` token.

    Deliberately dependency-light and never raising - this runs inside journal
    writers that must not break a turn.
    """
    if not isinstance(text, str) or not text:
        return ""
    try:
        cleaned = AdvancedCredentialStripper().strip(text)
    except Exception:
        cleaned = text
    try:
        from .redaction import redact

        return redact(cleaned)
    except Exception:
        return cleaned
