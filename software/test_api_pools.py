"""Tests for quota-pool failover, cloud STT wiring, and brain routing."""

import json
import os

import pytest

from source.server import api_pools
from source.server import cloud_stt
from source.server import brain_router


@pytest.fixture(autouse=True)
def _reset_model_bans():
    """Model bans are process-global; keep tests deterministic."""
    api_pools._model_bans.clear()
    yield
    api_pools._model_bans.clear()


class FakePool:
    def __init__(self, keys, cooldown=10):
        self.keys = list(keys)
        self.cooldown = cooldown
        self._failures = set()
        self.cooldowns = []  # cooldowns actually requested, for assertions

    @property
    def empty(self):
        return not self.keys

    def next_key(self):
        for key in self.keys:
            if key not in self._failures:
                return key
        return None

    def total(self):
        return len(self.keys)

    def banned(self):
        return len(self._failures)

    def succeed(self, key):
        self._failures.discard(key)

    def fail(self, key, cooldown=None):
        self._failures.add(key)
        self.cooldowns.append(cooldown)


def _install_pools(monkeypatch, mapping):
    pools = {name: FakePool(keys) for name, keys in mapping.items()}
    monkeypatch.setattr(api_pools, "_pools", pools)
    return pools


def test_pool_rotation_skips_banned_and_recovers(monkeypatch):
    pool = api_pools.Pool("brain_groq", ["k1", "k2", "k3"], cooldown=0.05, min_interval=0.0)
    assert pool.next_key() in ("k1", "k2", "k3")
    pool.fail("k2")
    remaining = {pool.next_key() for _ in range(3)}
    assert "k2" not in remaining
    assert pool.banned() == 1
    pool.succeed("k2")
    assert pool.banned() == 0


def test_pool_returns_none_when_all_banned(monkeypatch):
    pool = api_pools.Pool("stt_groq", ["k1", "k2"], cooldown=60, min_interval=0.0)
    pool.fail("k1")
    pool.fail("k2")
    assert pool.next_key() is None


def test_run_chain_success_stops_at_first_pool(monkeypatch):
    _install_pools(monkeypatch, {"stt_groq": ["g1"], "stt_deepgram": ["d1"]})
    calls = []

    def call(key, pool_name):
        calls.append((pool_name, key))
        return True, "hello", "", False

    ok, payload, provider, _ = api_pools.run_chain(("stt_groq", "stt_deepgram"), call)
    assert ok and payload == "hello" and provider == "stt_groq"
    assert calls == [("stt_groq", "g1")]


def test_run_chain_bans_quota_keys_and_rotates(monkeypatch):
    pools = _install_pools(monkeypatch, {"stt_groq": ["g1", "g2"], "stt_deepgram": ["d1"]})
    seen = []

    def call(key, pool_name):
        seen.append((pool_name, key))
        if pool_name == "stt_groq" and key == "g1":
            return False, None, "quota exceeded", True
        if pool_name == "stt_groq" and key == "g2":
            return False, None, "quota exceeded", True
        return True, "deep", "", False

    ok, payload, provider, _ = api_pools.run_chain(("stt_groq", "stt_deepgram"), call)
    assert ok and payload == "deep" and provider == "stt_deepgram"
    assert ("stt_groq", "g1") in seen and ("stt_groq", "g2") in seen
    assert pools["stt_groq"].banned() == 2
    assert pools["stt_deepgram"].banned() == 0


def test_run_chain_transient_errors_get_a_short_cooldown(monkeypatch):
    pools = _install_pools(monkeypatch, {"stt_groq": ["g1", "g2"], "stt_deepgram": ["d1"]})

    def call(key, pool_name):
        if pool_name == "stt_groq":
            return False, None, "timeout", False
        return True, "deep", "", False

    ok, payload, provider, _ = api_pools.run_chain(("stt_groq", "stt_deepgram"), call)
    assert ok and provider == "stt_deepgram"
    # Transient failures now get a SHORT cooldown instead of nothing, so a
    # permanently-broken provider eventually leaves the rotation rather than
    # costing a failing request on every single turn forever - and it must not
    # be a 6-hour ban on the first slip.
    assert pools["stt_groq"].banned() > 0
    assert all(c == api_pools.TRANSIENT_COOLDOWN_SECONDS
               for c in pools["stt_groq"].cooldowns), pools["stt_groq"].cooldowns


def test_run_chain_returns_failure_when_everything_exhausted(monkeypatch):
    pools = _install_pools(monkeypatch, {"stt_groq": ["g1"], "stt_deepgram": ["d1"]})
    pools["stt_groq"].fail("g1")
    pools["stt_deepgram"].fail("d1")

    ok, payload, provider, error = api_pools.run_chain(
        ("stt_groq", "stt_deepgram"), lambda key, pool_name: (False, None, "nope", True)
    )
    assert not ok and payload is None
    assert provider == ""
    assert "exhausted" in error


def test_sanitize_redacts_all_key_shapes():
    # NOTE: every fake key here is ASSEMBLED AT RUNTIME, never written as a
    # literal. Our own sanitizer matches the real provider shapes (e.g.
    # `sk-or-v1-[0-9a-f]{60,80}`), which is exactly what GitHub push protection
    # flags, so a committed literal blocks the push even when the value is
    # obviously fake. Assembling them here keeps this test fully meaningful
    # (the sanitiser still sees byte-identical input) AND lets the repo ship.
    fake_groq = "gsk_" + ("ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "abcdefghijkl")
    fake_gemini = "AIza" + "Sy" + ("1234567890abcdefghijklmnopqrstuvwxyz")
    fake_openrouter = "sk-or-v1-" + ("0123456789abcdef" * 4)
    raw = f"token {fake_groq} secret1 fake {fake_gemini} {fake_openrouter}"
    cleaned = api_pools.sanitize(raw)
    assert "gsk_" not in cleaned and "AIza" not in cleaned and "sk-or-v1-" not in cleaned


def test_credential_store_loads_file_and_env(monkeypatch, tmp_path):
    payload = json.dumps({"groq_stt": ["g1", "g2"], "gemini_brain": ["mb"]})
    cred_file = tmp_path / "credentials.json"
    cred_file.write_text(payload, encoding="utf-8")
    for name in (
        "GROQ_STT_API_KEY", "GROQ_API_KEY", "GROQ_BRAIN_API_KEY",
        "DEEPGRAM_API_KEY", "GEMINI_BRAIN_API_KEY", "GEMINI_API_KEY",
        "GEMINI_VISION_API_KEY", "OPENROUTER_BRAIN_API_KEY", "OPENROUTER_VISION_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FRIDAY_CREDENTIALS_FILE", str(cred_file))
    store = api_pools.CredentialStore()
    pools = store.pools()
    assert pools["stt_groq"] == ["g1", "g2"]
    assert pools["brain_gemini"] == ["mb"]
    assert "stt_deepgram" not in pools or pools["stt_deepgram"] == []


def test_pcm_to_wav_builds_valid_16k_mono_wav():
    import wave as wave_mod
    from io import BytesIO

    chunks = bytes([128]) * 32000
    wav = cloud_stt.pcm_to_wav(chunks)
    assert wav.startswith(b"RIFF") and b"WAVE" in wav
    with wave_mod.open(BytesIO(wav), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        assert handle.getsampwidth() == 2


def test_transcribe_wav_uses_groq_then_deepgram_chain(monkeypatch):
    requests_calls = []
    chain_names = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"text": "cloud hello"}

    def fake_post(*args, **kwargs):
        requests_calls.append((args[0], kwargs))
        return FakeResponse()

    def fake_run_chain(pool_names, call, on_result=None):
        chain_names["names"] = pool_names
        ok, payload, error, quota = call("g1", pool_names[0])
        return ok, payload, pool_names[0], error

    monkeypatch.setattr(cloud_stt.requests, "post", fake_post)
    monkeypatch.setattr(cloud_stt.api_pools, "run_chain", fake_run_chain)
    text, provider, error = cloud_stt.transcribe_wav(b"RIFFFAKE", enabled=True)
    assert text == "cloud hello"
    assert provider == "stt_groq"
    assert chain_names["names"] == ("stt_groq", "stt_deepgram", "gemini_stt")
    assert requests_calls[0][0].endswith("/audio/transcriptions")


def test_groq_transcribe_maps_quota_status(monkeypatch):
    class FakeResponse:
        def __init__(self, status, json_payload):
            self.status_code = status
            self._json = json_payload

        def json(self):
            return self._json

    monkeypatch.setattr(
        cloud_stt.requests,
        "post",
        lambda *a, **k: FakeResponse(429, {}),
    )
    ok, payload, error, quota = cloud_stt._groq_transcribe("g1", b"RIFF")
    assert not ok and quota is True

    monkeypatch.setattr(
        cloud_stt.requests,
        "post",
        lambda *a, **k: FakeResponse(200, {"text": "hi"}),
    )
    ok, payload, error, quota = cloud_stt._groq_transcribe("g1", b"RIFF")
    assert ok and payload == "hi" and quota is False


def test_tier_classifier_is_gone():
    """The deep/hard/normal tiers were removed on 2026-09-30 (Sir).

    With Modal as the primary brain for every turn, a tier could not change the
    outcome, so classify() was dead code and "use your strongest model" silently
    did nothing. A phrase that claims to escalate but does not is worse than no
    phrase: it teaches the user a lie about the system.
    """
    assert not hasattr(brain_router, "classify")
    assert not hasattr(brain_router, "DEEP_TRIGGERS")
    assert not hasattr(brain_router, "HARD_TRIGGERS")
    assert not hasattr(brain_router, "TOOL_TASK_TRIGGERS")


def test_one_chain_for_every_kind_of_request():
    """Chat, tool work, "debug this", and the old "strongest" phrase must all
    produce the IDENTICAL order - no tier is left to branch on."""
    baseline = [item[0] for item in brain_router.resolve("hello there")]
    for phrase in (
        "use your strongest model for this",
        "use gemini, this is really important",
        "debug this weird crash in production",
        "research the best approach for this",
        "take a screenshot",
    ):
        assert [item[0] for item in brain_router.resolve(phrase)] == baseline, phrase
def test_tier_classifier_is_gone():
    """The deep/hard/normal tiers were removed on 2026-09-30 (Sir).

    With Modal as the primary brain for every turn, a tier could not change the
    outcome, so classify() was dead code and "use your strongest model" silently
    did nothing. A phrase that claims to escalate but does not is worse than no
    phrase: it teaches the user a lie about the system.
    """
    assert not hasattr(brain_router, "classify")
    assert not hasattr(brain_router, "DEEP_TRIGGERS")
    assert not hasattr(brain_router, "HARD_TRIGGERS")
    assert not hasattr(brain_router, "TOOL_TASK_TRIGGERS")


def test_one_chain_for_every_kind_of_request():
    """Chat, tool work, "debug this", and the old "strongest" phrase must all
    produce the IDENTICAL order - no tier is left to branch on."""
    baseline = [item[0] for item in brain_router.resolve("hello there")]
    for phrase in (
        "use your strongest model for this",
        "use gemini, this is really important",
        "debug this weird crash in production",
        "research the best approach for this",
        "take a screenshot",
    ):
        assert [item[0] for item in brain_router.resolve(phrase)] == baseline, phrase
def test_brain_resolve_keeps_ordered_fallback_chain():
    """Order is Modal -> Groq -> OpenRouter -> Gemini -> local (Sir 2026-09-30)."""
    resolved = brain_router.resolve("hello there")
    pools = [item[0] for item in resolved]
    if brain_router.MODAL_BASE:
        # Modal is the PRIMARY brain whenever an endpoint is configured.
        assert pools[0] == "brain_modal"
        assert pools.index("brain_groq") > pools.index("brain_modal")
    else:
        assert pools[0] == "brain_groq"
    assert pools.index("brain_openrouter") < pools.index("brain_gemini")
    assert pools[-1] == "local"


def test_brain_resolve_deep_keeps_modal_primary_and_gemini_order():
    """"Use your strongest" must NOT jump ahead of the Modal primary."""
    resolved = brain_router.resolve("use your strongest model please")
    if brain_router.MODAL_BASE:
        assert resolved[0][0] == "brain_modal"
    # Per-account-per-model chain: strongest new models first
    gemini_models = [item[1] for item in resolved if item[0] == "brain_gemini"]
    assert gemini_models == [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]


def test_brain_resolve_openrouter_model_chain():
    resolved = brain_router.resolve("hello there")
    openrouter_models = [item[1] for item in resolved if item[0] == "brain_openrouter"]
    # Live-verified Sep 2026: deepseek-chat-v3-0324:free is dead (404
    # unavailable-for-free on OpenRouter); today's healthy free chain:
    assert openrouter_models == [
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "google/gemma-4-31b-it:free",
    ]


def test_brain_model_ban_skips_to_next_gemini_model(monkeypatch):
    pools = _install_pools(monkeypatch, {"brain_gemini": ["m1"]})
    api_pools.fail_model("brain_gemini", "gemini-3.8-flash")
    resolved = brain_router.resolve("use your strongest model")
    # The banned model entry is still in resolve(); apply_for skips it.
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    router.apply_for("hello", interpreter)
    assert llm.model == "openai/gemini-3.7-flash"
    api_pools.fail_model("brain_gemini", "gemini-3.7-flash")
    api_pools.fail_model("brain_gemini", "gemini-3.5-flash")
    api_pools.fail_model("brain_gemini", "gemini-3.5-flash-lite")
    api_pools.fail_model("brain_gemini", "gemini-3.1-flash-lite")
    # All chain models hot -> straight to local, account NOT banned
    router2 = brain_router.BrainRouter(enabled=True)
    ticket = router2.apply_for("hello", interpreter)
    # Local is a real ticket so a local failure is recordable (was: None, which
    # made a dead chain spin with nothing logged anywhere).
    assert ticket == ("local", None, brain_router.LOCAL_MODEL)
    assert llm.model == brain_router.LOCAL_MODEL
    # The ACCOUNT key stays unbanned — only the model bucket was hot.
    assert pools["brain_gemini"].banned() == 0


def test_gemini_stt_pool_aggregates_both_accounts(monkeypatch, tmp_path):
    creds = {"gemini_brain": ["BRAINKEY"], "gemini_vision": ["VISKEY"]}
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(creds), encoding="utf-8")
    store = api_pools.CredentialStore()
    store._file_candidates = [str(path), ""]
    data = store._load()
    # Both accounts available for the Gemini transcribe stage, no duplicates.
    assert data["gemini_stt"] == ["BRAINKEY", "VISKEY"]
    assert data["gemini_brain"] == ["BRAINKEY"]
    assert data["gemini_vision"] == ["VISKEY"]


def test_gemini_transcribe_posts_generate_content(monkeypatch):
    calls = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "hello underground"}]}}]}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setattr(cloud_stt.requests, "post", fake_post)
    ok, text, error, quota = cloud_stt._gemini_transcribe("AIzaFAKE", b"RIFFWAV")
    assert ok and text == "hello underground" and quota is False
    assert calls["url"].endswith("generateContent")


class _FakeLlm:
    def __init__(self):
        self.model = None
        self.api_base = None
        self.api_key = None


def test_brain_apply_for_sets_llm_and_ticket(monkeypatch):
    pools = _install_pools(
        monkeypatch,
        {"brain_groq": ["g1"], "brain_openrouter": ["o1"], "brain_gemini": ["m1"]},
    )
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    ticket = router.apply_for("good morning", interpreter)
    assert ticket == ("brain_groq", "g1", "openai/gpt-oss-20b")
    assert llm.api_key == "g1"
    assert llm.api_base == brain_router.GROQ_BASE
    assert "openai/" in llm.model


def test_brain_failover_skips_banned_pool(monkeypatch):
    pools = _install_pools(
        monkeypatch,
        {"brain_groq": ["g1"], "brain_openrouter": ["o1"], "brain_gemini": ["m1"]},
    )
    pools["brain_groq"].fail("g1")
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    ticket = router.apply_for("hello", interpreter)
    # Chain order is Modal -> Groq -> OpenRouter -> Gemini -> local, so with
    assert ticket == ("brain_openrouter", "o1", "nvidia/nemotron-3.5-lightning:free")


def test_brain_apply_for_uses_local_when_everything_banned(monkeypatch):
    pools = _install_pools(
        monkeypatch,
        {"brain_groq": ["g1"], "brain_openrouter": ["o1"], "brain_gemini": ["m1"]},
    )
    pools["brain_groq"].fail("g1")
    pools["brain_openrouter"].fail("o1")
    pools["brain_gemini"].fail("m1")
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    ticket = router.apply_for("hello", interpreter)
    assert ticket == ("local", None, brain_router.LOCAL_MODEL)
    assert llm.model == brain_router.LOCAL_MODEL
    assert llm.api_key is None


def test_brain_mark_failure_consumes_ticket(monkeypatch):
    pools = _install_pools(monkeypatch, {"brain_groq": ["g1", "g2"]})
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    router.apply_for("hello", interpreter)
    router.mark_failure("some exception")
    assert pools["brain_groq"].banned() == 1
    router.mark_success()
    assert router.current is None


def test_brain_thought_signature_error_short_cooldown(monkeypatch):
    """Gemini thought_signature 400s (foreign Groq tool history) must cool
    the model for ~2 minutes, not the 6-hour exhaustion ban."""
    pools = _install_pools(monkeypatch, {"brain_groq": ["g1"], "brain_gemini": ["m1"]})
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    router.apply_for("hello", interpreter)
    router.failover_to_next(interpreter, "hello", reason="whatever")  # -> gemini
    record = {}

    def fake_fail_model(pool_name, model, cooldown=None):
        record["cooldown"] = cooldown

    monkeypatch.setattr(brain_router.api_pools, "fail_model", fake_fail_model)
    router.mark_failure(
        "400 Function call is missing a thought_signature in functionCall parts"
    )
    assert record["cooldown"] == 120.0


def test_brain_disabled_uses_local(monkeypatch):
    monkeypatch.setattr(brain_router.api_pools, "pool", lambda name: None)
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=False)
    ticket = router.apply_for("hello", interpreter)
    # Local is now a real ticket, not None: a local-brain failure has to be
    # recordable, otherwise a fully dead chain spins with nothing logged.
    assert ticket == ("local", None, brain_router.LOCAL_MODEL)
    assert llm.model == brain_router.LOCAL_MODEL


def test_brain_failover_to_next_bans_key_and_advances(monkeypatch):
    pools = _install_pools(
        monkeypatch,
        {"brain_groq": ["g1"], "brain_openrouter": ["o1"], "brain_gemini": ["m1"]},
    )
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    assert router.apply_for("hello", interpreter) == ("brain_groq", "g1", "openai/gpt-oss-20b")
    ok = router.failover_to_next(interpreter, "hello", reason="tool choice is none")
    assert ok is True
    assert router.current == ("brain_openrouter", "o1", "nvidia/nemotron-3.5-lightning:free")
    assert pools["brain_groq"].banned() == 1
    assert llm.api_key == "o1"


def test_brain_failover_to_next_permanent_after_full_exhaustion(monkeypatch):
    pools = _install_pools(
        monkeypatch,
        {"brain_groq": ["g1"], "brain_openrouter": ["o1"], "brain_gemini": ["m1"]},
    )
    pools["brain_groq"].fail("g1")
    pools["brain_openrouter"].fail("o1")
    pools["brain_gemini"].fail("m1")
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    # Falls straight through the cloud chain to local, which is a real ticket now.
    assert router.apply_for("hello", interpreter) == ("local", None, brain_router.LOCAL_MODEL)
    assert llm.model == brain_router.LOCAL_MODEL
    # A hop to LOCAL is the end of the chain, so it must report exhaustion.
    ok = router.failover_to_next(interpreter, "hello", reason="quota")
    assert ok is False
    assert router.current == ("local", None, brain_router.LOCAL_MODEL)



def test_brain_transient_503_short_cooldown(monkeypatch):
    """Gemini 503 'high demand' capacity blips must cool the model for
    minutes, not the 6-hour exhaustion ban."""
    pools = _install_pools(monkeypatch, {"brain_groq": ["g1"], "brain_gemini": ["m1"]})
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    router.apply_for("hello", interpreter)
    router.failover_to_next(interpreter, "hello", reason="whatever")  # -> gemini
    record = {}

    def fake_fail_model(pool_name, model, cooldown=None):
        record["cooldown"] = cooldown

    monkeypatch.setattr(brain_router.api_pools, "fail_model", fake_fail_model)
    router.mark_failure(
        "503 This model is currently experiencing high demand. Spikes in "
        "demand are usually temporary."
    )
    assert record["cooldown"] == 300.0


def test_pool_fail_three_strikes_promotes_to_six_hours():
    pool = api_pools.Pool("brain_groq", ["g1"], cooldown=0.05 * 0 + api_pools._COOLDOWN_SECONDS / 1000.0, min_interval=0.0) if False else api_pools.Pool(
        "brain_groq", ["g1"], cooldown=api_pools._COOLDOWN_SECONDS, min_interval=0.0
    )
    import time as _time
    # FIX (2026-09-29): succeed() now RESETS the strike counter, so three
    # failures that are NOT in a row must NOT promote to six hours. The old
    # code accumulated strikes across successes, which contradicted this
    # pool's own "3rd failure in a row" contract.
    pool.succeed("g1")
    pool.fail("g1", cooldown=24.0)
    pool.succeed("g1")
    pool.fail("g1", cooldown=13.0)
    pool.succeed("g1")
    pool.fail("g1", cooldown=13.0)
    with pool._lock:
        idx = pool._keys.index("g1")
        remaining = pool._banned_until.get(idx, 0) - _time.monotonic()
    assert 0 < remaining < api_pools._COOLDOWN_SECONDS * 0.9, remaining

    # Three short failures IN A ROW still escalate to the full window.
    pool2 = api_pools.Pool("brain_groq", ["g2"], cooldown=api_pools._COOLDOWN_SECONDS, min_interval=0.0)
    pool2.fail("g2", cooldown=13.0)
    pool2.fail("g2", cooldown=13.0)
    pool2.fail("g2", cooldown=13.0)
    with pool2._lock:
        idx2 = pool2._keys.index("g2")
        remaining2 = pool2._banned_until.get(idx2, 0) - _time.monotonic()
    assert remaining2 >= api_pools._COOLDOWN_SECONDS * 0.9, remaining2


def test_brain_turn_blacklist_forward_only(monkeypatch):
    pools = _install_pools(monkeypatch, {"brain_groq": ["g1", "g2"], "brain_openrouter": ["o1"], "brain_gemini": ["m1", "m2"]})
    llm = _FakeLlm()
    interpreter = type("I", (), {"llm": llm})
    router = brain_router.BrainRouter(enabled=True)
    router.new_turn()
    assert router.apply_for("hello", interpreter) == ("brain_groq", "g1", "openai/gpt-oss-20b")
    router.failover_to_next(interpreter, "hello", reason="TRACEBACK 429 rate limit try again in 5.0s")
    # The blacklist is per (pool, MODEL), and a 429 now takes a SHORT cooldown
    # instead of banning the whole pool for 6h. So the next hop is the sibling
    # model on the SAME healthy account (gpt-oss-120b) - not a jump to another
    # provider. Strict forward-only either way: gpt-oss-20b is never revisited.
    assert router.current == ("brain_groq", "g2", "openai/gpt-oss-120b")
    # Exhausting groq's models in this turn does move on to the next provider
    # (chain order is groq fast -> groq strong -> gemini -> openrouter -> local).
    router._turn_mark("brain_groq", "openai/gpt-oss-120b")
    router.apply_for("hello", interpreter)
    assert router.current[0] == "brain_openrouter"
    # next turn clears the blacklist so groq is eligible again
    router.new_turn()
    ticket = router.apply_for("hello", interpreter)
    assert ticket[0] == "brain_groq"

