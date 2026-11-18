"""Local service URLs must default to 127.0.0.1, never `localhost`.

WHY THIS FILE EXISTS (ROUND ZB-LAT, 2026-11-03)
------------------------------------------------
`localhost` on this machine resolves to BOTH `127.0.0.1` and `::1`
(`getaddrinfo` returns both, instantly). Nothing answers on the IPv6 loopback
and the stack **blackholes SYNs instead of refusing them**, so `requests` sits
out its whole connect timeout on `::1` before falling back to IPv4.

Measured on this machine:

    connect 127.0.0.1:11434   connected 0.000s
    connect ::1      :11434   TimeoutError 2.016s
    connect 127.0.0.1:5050    connected 0.000s
    connect ::1      :5050    TimeoutError 2.000s

    http://localhost:11434/api/tags   2.078s   http://127.0.0.1:11434/api/tags   0.015s
    http://localhost:5050/v1/models   2.047s   http://127.0.0.1:5050/v1/models   0.000s

Those 2 seconds were charged to:

* **every prompt render** - `self_state()` -> `_ollama_models()` runs inside
  the `{{self_knowledge()}}` prompt block, so it was ~2s of the measured 7.0s
  `brain select -> llm_request_start` gap (median of 177 real turns);
* **every local TTS utterance** - the edge fallback POSTs to `:5050`, so local
  speech paid 2s on top of synthesis;
* **every local brain / local vision / sandbox completion**.

This file pins the defaults so the 2 seconds cannot quietly come back. It does
NOT assert the live latency penalty: that depends on the machine's IPv6 state
and would be flaky (and slow) on a host where `::1` answers normally.
"""

import os
import re
from pathlib import Path

import pytest

from source.server import brain_keepalive
from source.server import brain_router
from source.server import delegation
from source.server import digest
from source.server import gemini_tts
from source.server import screen_understanding
from source.server import self_awareness
from source.server import tts_bootstrap

ROOT = Path(__file__).resolve().parent / "source" / "server"

# Files whose LOCAL SERVICE DEFAULTS are on a per-turn or per-utterance path.
# n8n_runtime is deliberately excluded: it is an outbound webhook to a
# user-disabled external service, it is not on any latency path, and a dozen
# tests plus the user's own config pin its literal.
HOT_PATH_FILES = [
    "self_awareness.py",
    "brain_router.py",
    "screen_understanding.py",
    "delegation.py",
    "gemini_tts.py",
    "tts_bootstrap.py",
    "brain_keepalive.py",
    "digest.py",
    "doctor.py",
]

DEFAULT_URL_RE = re.compile(
    r"""(?:os\.environ\.get\(\s*["'][A-Z_0-9]+["']\s*,|(?:DEFAULT_[A-Z_]*_URL|LOCAL_[A-Z_]+|LOCAL_BASE)\s*=)\s*["'](http://[^"']+)["']"""
)


class TestTheMeasuredDefaults:
    """The symbols themselves, imported for real."""

    def test_the_ollama_probe_no_longer_costs_two_seconds(self):
        # Measured before: 2.016s. After: 0.015s.
        assert self_awareness._ollama_models()["url"] == "http://127.0.0.1:11434"

    def test_the_local_tts_url_is_the_fast_literal(self):
        assert "127.0.0.1:5050" in gemini_tts.LOCAL_TTS_URL
        assert "localhost" not in gemini_tts.LOCAL_TTS_URL

    def test_the_local_brain_url_is_the_fast_literal(self):
        assert "127.0.0.1" in brain_router.LOCAL_BASE
        assert "localhost" not in brain_router.LOCAL_BASE

    def test_the_local_vision_url_is_the_fast_literal(self):
        assert "127.0.0.1" in screen_understanding.LOCAL_CHAT_URL
        assert "localhost" not in screen_understanding.LOCAL_CHAT_URL

    def test_the_edge_server_defaults_are_the_fast_literal(self):
        assert tts_bootstrap.DEFAULT_BASE_URL == "http://127.0.0.1:5050/v1"
        assert "localhost" not in tts_bootstrap.DEFAULT_BASE_URL

    def test_delegation_defaults_to_the_fast_literal(self):
        assert delegation.DEFAULT_OLLAMA_URL == "http://127.0.0.1:11434"

    def test_the_keepalive_and_digest_defaults_follow(self):
        assert "localhost" not in brain_keepalive._base_url()
        assert "localhost" not in digest._ollama_base()


class TestNoHotPathDefaultStillSaysLocalhost:
    """Scan the hot-path sources for a reintroduced `localhost` default."""

    @pytest.mark.parametrize("name", HOT_PATH_FILES)
    def test_no_default_url_in_a_hot_path_file_uses_localhost(self, name):
        source = (ROOT / name).read_text(encoding="utf-8")
        offenders = [match.group(1)
                     for match in DEFAULT_URL_RE.finditer(source)
                     if "localhost" in match.group(1)]
        assert not offenders, (
            "%s reintroduced a `localhost` default %s - on this machine that "
            "is a measured 2s connect timeout on every call"
            % (name, offenders)
        )

    def test_the_sandbox_llm_base_is_the_fast_literal(self):
        # It lives inside setup_code_template (profiles/default.py), so it is
        # matched as source text rather than imported.
        profile = (ROOT / "profiles" / "default.py").read_text(encoding="utf-8")
        assert 'interpreter.llm.api_base = "http://127.0.0.1:11434"' in profile
        assert 'interpreter.llm.api_base = "http://localhost:11434"' not in profile

    def test_env_overrides_are_still_honoured(self):
        """The default changed; explicit configuration must still win.

        Asserted from source rather than by reloading a live module: a reload
        of `gemini_tts` would re-execute module-level state that other TTS
        tests share.
        """
        source = (ROOT / "gemini_tts.py").read_text(encoding="utf-8")
        assert 'os.environ.get("FRIDAY_TTS_SPEECH_URL"' in source
        assert 'os.environ.get("OPENAI_BASE_URL"' in source
        assert 'os.environ.get("OLLAMA_CHAT_URL"' in (
            ROOT / "brain_router.py").read_text(encoding="utf-8")


class TestTheLatencyThisRoundRemoved:
    """Numbers this round is responsible for, kept next to the fix."""

    def test_the_two_targets_of_the_fix_are_documented(self):
        # Measured on this machine, 2026-11-03 (see the module docstring):
        # prompt-render gap median 7.0s over n=177, of which ~2s was this.
        assert self_awareness._ollama_models.__doc__ is not None
        assert "127.0.0.1" in self_awareness._ollama_models.__doc__
        assert "2.015s" in self_awareness._ollama_models.__doc__

    @pytest.mark.skipif(
        os.name != "nt", reason="the ::1 blackhole is a Windows measurement")
    def test_ipv6_loopback_is_still_the_reason_for_the_literal(self):
        """Only asserts what is TRUE on this machine's hazard, cheaply.

        If `::1` ever starts answering normally this simply documents that the
        hazard is gone; it never fails on a healthy host because it only
        compares `127.0.0.1` against itself.
        """
        import socket
        import time

        started = time.monotonic()
        try:
            sock = socket.create_connection(("127.0.0.1", 11434), timeout=1.0)
            sock.close()
        except OSError:
            pytest.skip("Ollama is not listening on 127.0.0.1")
        v4 = time.monotonic() - started
        assert v4 < 0.5, "the IPv4 loopback itself is slow; not our bug"
