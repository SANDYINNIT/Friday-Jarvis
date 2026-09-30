"""Exhaustion-ledger persistence must survive app restarts (6-hour windows)."""

import json

from source.server import api_pools


def _fake_pools(monkeypatch):
    real_pool = api_pools.Pool("brain_groq", ["g1"], cooldown=10, min_interval=0.0)
    pools = {
        name: (real_pool if name == "brain_groq" else api_pools.Pool(name, [], min_interval=0.0))
        for name in api_pools.POOL_SCHEMA
    }
    monkeypatch.setattr(api_pools, "_pools", pools)
    return pools, real_pool


def test_pool_fail_persists_exhaustion(monkeypatch, tmp_path):
    ledger = tmp_path / "exhaustion_ledger.json"
    monkeypatch.setattr(api_pools, "_LEDGER_PATH", str(ledger))
    _, real_pool = _fake_pools(monkeypatch)

    real_pool.fail("g1", cooldown=21600)
    assert ledger.exists()
    data = json.loads(ledger.read_text(encoding="utf-8"))
    # SECURITY (2026-09-29): the ledger must NOT contain the raw key. It stores a
    # digest so a stolen ledger is useless without the live credentials.
    dumped = json.dumps(data)
    assert "g1" not in dumped, dumped
    assert api_pools._key_fingerprint("g1") in dumped, dumped
    assert abs(list(data["keys"].values())[0] - 360.0) < 1.0


def test_restore_ledger_accepts_digest_identifiers(monkeypatch, tmp_path):
    # The digest form written by _persist_ledger must restore correctly, so a
    # 6-hour ban still survives a restart after the security change.
    ledger = tmp_path / "exhaustion_ledger.json"
    digest = api_pools._key_fingerprint("g1")
    ledger.write_text(
        json.dumps({"keys": {"brain_groq:" + digest: 300.0}, "models": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(api_pools, "_LEDGER_PATH", str(ledger))
    pools, _ = _fake_pools(monkeypatch)

    assert api_pools._restore_ledger() == 1
    assert pools["brain_groq"].banned() == 1


def test_retry_after_hint_is_parsed_and_clamped():
    # Groq / OpenAI phrasing
    assert api_pools._retry_after_seconds("Rate limit reached. Please try again in 6.6s") == 6.6
    # Google phrasing (observed on the free Gemini TTS tier, limit 10)
    assert abs(api_pools._retry_after_seconds("Please retry in 29.683900996s.") - 29.68) < 0.01
    # clamped into a sane window
    assert api_pools._retry_after_seconds("try again in 99999s") == api_pools._RETRY_AFTER_MAX
    assert api_pools._retry_after_seconds("retry in 0.2s") == api_pools._RETRY_AFTER_MIN
    # no hint at all
    assert api_pools._retry_after_seconds("HTTP 500 Internal Server Error") is None
    assert api_pools._retry_after_seconds("") is None
    assert api_pools._retry_after_seconds(None) is None


def test_retry_after_handles_google_and_groq_phrasings():
    assert abs(api_pools._retry_after_seconds("Please retry in 29.68s.") - 29.68) < 0.01
    assert api_pools._retry_after_seconds("Please try again in 6.6s") == 6.6
    # a number that is not a retry hint must not be mistaken for one
    assert api_pools._retry_after_seconds("limit: 10, model: gemini") is None


def test_restore_ledger_reapplies_bans(monkeypatch, tmp_path):
    ledger = tmp_path / "exhaustion_ledger.json"
    ledger.write_text(
        '{"keys": {"brain_groq:g1": 359.9}, "models": {"brain_gemini:gemini-3.8-flash": 10.0}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(api_pools, "_LEDGER_PATH", str(ledger))
    pools, _ = _fake_pools(monkeypatch)

    restored = api_pools._restore_ledger()
    assert restored == 2
    assert pools["brain_groq"].banned() == 1


def test_exhaustion_window_is_six_hours():
    assert abs(api_pools._EXHAUSTION_COOLDOWN - 6 * 3600) < 1
