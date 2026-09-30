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
