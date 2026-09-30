"""Tests for the TTS chain: CLOUD FIRST, local voice as the fallback.

Updated 2026-09-30. The chain used to be local-first, which meant
_local_pcm() called ensure_edge_tts_server() and blocked up to 45s waiting for
a server that was usually not running, while cloud TTS answered in 0.27s.
That produced a measured 62-second silence. The order is now cloud-first
(Sir's decision), with the local voice used only when the cloud cannot speak,
and never blocking on a speculative server start.
"""

import base64

import pytest

from source.server import api_pools
from source.server import gemini_tts


@pytest.fixture(autouse=True)
def _reset_bans_and_pools(monkeypatch):
    api_pools._model_bans.clear()
    yield
    api_pools._model_bans.clear()
    gemini_tts._local_server_started = False


def _tts_pools(monkeypatch, keys):
    pools = {"gemini_tts": api_pools.Pool("gemini_tts", keys, min_interval=0.0)}
    monkeypatch.setattr(api_pools, "_pools", pools)
    return pools


def _gemini_response(status=200, data=""):
    class R:
        status_code = status
        content = b""
        text = ""

        def json(self):
            if status != 200:
                return {}
            return {"candidates": [{"content": {"parts": [{"inlineData": {
                "data": data, "mimeType": "audio/L16;rate=24000"}}]}}]}

    return R


def _install_fake_pydub(monkeypatch, pcm=b"LOCALPCM" * 6):
    class Seg:
        def __init__(self, buf, format="mp3"):
            pass

        def set_frame_rate(self, rate):
            return self

        def set_channels(self, channels):
            return self

        def set_sample_width(self, width):
            return self

        @property
        def raw_data(self):
            return pcm

    class FakeAudioSegment:
        @staticmethod
        def from_file(buf, format=("mp3",)):
            return Seg(buf)

    monkeypatch.setattr(gemini_tts, "_audio_segment_cls", FakeAudioSegment)


def test_tts_speak_uses_cloud_voice_first(monkeypatch):
    """Cloud must be reached before the local voice is even probed."""
    pools = _tts_pools(monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(gemini_tts, "_local_server_started", True)
    cloud_calls = []
    local_hits = []

    def fake_post(url, **kwargs):
        if "localhost" in url:
            local_hits.append(url)

            class Local:
                status_code = 200
                content = b"ID3FAKE_MP3_BYTES"
                text = ""

                def json(self):
                    return {}

            return Local
        cloud_calls.append(url)
        return _gemini_response(200, base64.b64encode(b"\x00\x01" * 10).decode())()

    monkeypatch.setattr(gemini_tts.requests, "post", fake_post)

    voice = gemini_tts.CloudFirstTTSVoice()
    pcm = voice.speak("Hello, Sir.")
    assert pcm
    assert cloud_calls, "cloud TTS must be tried first"
    assert not local_hits, "local voice must not be touched while cloud works"
    assert voice.LAST_STAGE == "gemini"
    assert pools["gemini_tts"].banned() == 0


def test_tts_speak_falls_back_to_local_when_cloud_dead(monkeypatch):
    """When every cloud account fails, the local voice is the last resort."""
    _tts_pools(monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(gemini_tts, "_local_server_started", True)
    _install_fake_pydub(monkeypatch)
    attempts = {"n": 0}
    local_hits = []

    def fake_post(url, **kwargs):
        if "localhost" in url:
            local_hits.append(url)

            class Local:
                status_code = 200
                content = b"ID3FAKE_MP3_BYTES"
                text = ""

                def json(self):
                    return {}

            return Local
        attempts["n"] += 1
        return _gemini_response(429)()

    monkeypatch.setattr(gemini_tts.requests, "post", fake_post)
    voice = gemini_tts.CloudFirstTTSVoice()
    pcm = voice.speak("Hi again.")
    assert pcm == b"LOCALPCM" * 6
    assert attempts["n"] == 2, "both cloud accounts must be tried before local"
    assert local_hits, "local voice is the fallback when cloud is exhausted"
    assert voice.LAST_STAGE == "local"


def test_tts_speak_returns_none_when_local_and_cloud_dead(monkeypatch):
    _tts_pools(monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(gemini_tts, "_local_server_started", True)
    cloud_calls = []

    def fake_post(url, **kwargs):
        if "localhost" in url:
            class Local:
                status_code = 500
                content = b""
                text = ""

                def json(self):
                    return {}

            return Local
        cloud_calls.append(url)
        return _gemini_response(429)()

    monkeypatch.setattr(gemini_tts.requests, "post", fake_post)
    voice = gemini_tts.CloudFirstTTSVoice()
    assert voice.speak("Say something.") is None
    assert len(cloud_calls) == 2
    assert voice.LAST_STAGE == "none"


def test_cloud_429_does_not_ban_the_voice_for_six_hours(monkeypatch):
    """Regression: fail(key) with no cooldown meant a single 429 muted BOTH
    Gemini TTS accounts for the full 6-hour exhaustion window (observed:
    360 minutes). It must now be a short, provider-hint-aware cooldown."""
    import time

    pools = _tts_pools(monkeypatch, ["acc1", "acc2"])
    monkeypatch.setattr(gemini_tts, "_local_server_started", True)

    def fake_post(url, **kwargs):
        if "localhost" in url:
            class Local:
                status_code = 500
                content = b""
                text = ""

                def json(self):
                    return {}

            return Local
        return _gemini_response(429)()

    monkeypatch.setattr(gemini_tts.requests, "post", fake_post)
    gemini_tts.CloudFirstTTSVoice().speak("Testing the ban window.")

    pool = pools["gemini_tts"]
    now = time.monotonic()
    for index, until in pool._banned_until.items():
        remaining = until - now
        assert 0 < remaining < api_pools._COOLDOWN_SECONDS * 0.5, (
            f"key {index} cooled for {remaining}s - a TTS 429 must not ban for hours"
        )


def test_provider_retry_hint_is_preserved(monkeypatch):
    """Google answers a TTS quota hit with 'Please retry in 29.68s' and names
    the violated metric. That guidance must reach the caller, not be dropped."""
    import time

    pools = _tts_pools(monkeypatch, ["acc1"])
    monkeypatch.setattr(gemini_tts, "_local_server_started", True)

    class Quota429:
        status_code = 429
        content = b""
        text = ("Quota exceeded for metric: generate_content_free_tier_requests, "
                "limit: 10, model: gemini-3.1-flash-tts. Please retry in 29.5s.")

        def json(self):
            return {}

    def fake_post(url, **kwargs):
        if "localhost" in url:
            class Local:
                status_code = 500
                content = b""
                text = ""

                def json(self):
                    return {}

            return Local
        return Quota429()

    monkeypatch.setattr(gemini_tts.requests, "post", fake_post)
    gemini_tts.CloudFirstTTSVoice().speak("Testing the hint.")

    pool = pools["gemini_tts"]
    now = time.monotonic()
    remaining = pool._banned_until.get(0, 0) - now
    # close to the provider's own 29.5s ask, not the 60s default and not 6 hours
    assert 20 < remaining < 45, f"expected the provider hint (~30s), got {remaining}s"


def test_engine_synthesize_queues_pcm(monkeypatch):
    _tts_pools(monkeypatch, ["acc1"])
    monkeypatch.setattr(gemini_tts, "_local_server_started", True)

    def fake_post(url, **kwargs):
        return _gemini_response(200, base64.b64encode(b"CLOUDPCM" * 4).decode())()

    monkeypatch.setattr(gemini_tts.requests, "post", fake_post)
    engine = gemini_tts.CloudFirstTTSEngine()
    assert engine.synthesize("Speak up") is True
    chunks = []
    while not engine.queue.empty():
        chunks.append(engine.queue.get())
    assert b"".join(chunks)
    assert engine.voice_bridge.LAST_STAGE == "gemini"
    assert engine.get_stream_info()[2] == 24000
