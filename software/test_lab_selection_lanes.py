"""ROUND LAB-2D2: per-lane eligibility enforcement through REAL lanes.

The review (LAB-2 D) said brain-only was insufficient: a user-disable on
`chr_stt_groq`/`chr_vision_modal`/TTS must actually change the supported
lane's real provider selection. This file wires the consult into `run_chain`
(single cloud-chain lane entry) + the direct `_cloud_vision` lane and proves
both with deterministic pools/callbacks — no real HTTP anywhere.
"""

import pytest

from source.server import lab_authority as authority_mod
from source.server import api_pools


class _FakePool:
    def __init__(self, name, total=1):
        self.name = name
        self.empty = False
        self._total = total
        self._next_calls = 0

    def total(self):
        return self._total

    def next_key(self, *a, **k):
        self._next_calls += 1
        return "fake-key-%s-%d" % (self.name, self._next_calls)

    def succeed(self, *a, **k):
        return True

    def fail(self, *a, **k):
        return None


@pytest.fixture()
def authority(tmp_path):
    authority_mod.reset_shared()
    authority_mod.configure(tmp_path / "lab-authority.db")
    try:
        yield authority_mod.shared()
    finally:
        authority_mod.reset_shared()


@pytest.fixture()
def wired_pools(authority, monkeypatch):
    """The REAL api_pools module with the consult installed (the same
    binding the running app gets through the authority warmup), and every
    SHIPPED pool name stubbed deterministically."""
    authority.register_router_listener()
    fake_pools = {name: _FakePool(name) for name in (
        "stt_groq", "stt_deepgram", "gemini_stt",
        "vision_modal", "vision_gemini", "vision_openrouter",
        "brain_modal", "brain_groq", "brain_openrouter")}
    monkeypatch.setattr(api_pools, "pool",
                        lambda name: fake_pools.get(name))
    # run_chain reads its registry snapshot directly, rather than pool().
    monkeypatch.setattr(api_pools, "_ensure_pools", lambda: fake_pools)
    monkeypatch.setattr(api_pools, "model_banned",
                        lambda pool, model: False)
    return fake_pools


def _recording_call(records, ok=True, error=""):
    def _call(key, pool_name):
        records.append(pool_name)
        return (ok, {"text": "ok %s" % pool_name}, error,
                "429" in error or "quota" in error)
    return _call


class TestRunChainLanesHonourTheConsult:
    """`api_pools.run_chain` is the pool selection for STT (and any chain
    lane). A user-disable on ONE pool excludes it; the next eligible pool
    serves."""

    def test_call_chain_with_no_disabled_serves_the_first_pool(
            self, authority, wired_pools):
        records = []
        ok, payload, provider, error = api_pools.run_chain(
            ("stt_groq", "stt_deepgram", "gemini_stt"),
            _recording_call(records, ok=True))
        assert provider == "stt_groq", provider
        assert records == ["stt_groq"], records

    def test_disabled_stt_pool_falls_to_the_next_eligible(
            self, authority, wired_pools):
        # the user disables chr_stt_groq: the stt_groq pool must be SKIPPED and
        # stt_deepgram must serve instead.
        # NOTE: the cast's STT characters are `chr_stt_groq` etc; the pool
        # names resolve through the SAME consult.
        authority.set_eligibility("chr_stt_groq", False, "sandy paused stt")
        records = []
        ok, payload, provider, error = api_pools.run_chain(
            ("stt_groq", "stt_deepgram", "gemini_stt"),
            _recording_call(records, ok=True))
        assert provider == "stt_deepgram", provider
        assert "stt_groq" not in records, (
            "user-disabled pool must be SKIPPED, not attempted", records)
        assert "stt_deepgram" in records, records

    def test_re_enabling_stt_restores_the_disabled_pool(
            self, authority, wired_pools):
        authority.set_eligibility("chr_stt_groq", False, "sandy pause")
        records_before = []
        api_pools.run_chain(("stt_groq", "stt_deepgram"),
                            _recording_call(records_before, ok=True))
        assert "stt_groq" not in records_before
        authority.set_eligibility("chr_stt_groq", True, "resume")
        records_after = []
        ok, payload, provider, error = api_pools.run_chain(
            ("stt_groq", "stt_deepgram"),
            _recording_call(records_after, ok=True))
        assert provider == "stt_groq", provider

    def test_disabled_everything_refuses_the_whole_chain_honestly(
            self, authority, wired_pools):
        for name in ("chr_stt_groq", "chr_stt_deepgram"):
            authority.set_eligibility(name, False, "sandy propagate refusal")
        records = []
        ok, payload, provider, error = api_pools.run_chain(
            ("stt_groq", "stt_deepgram"), _recording_call(records, ok=True))
        assert ok is False, ok
        assert provider == "" or provider is None
        assert "user-disabled" in (error or ""), error
