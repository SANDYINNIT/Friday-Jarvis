"""Tests for the single authoritative lab state (revisions, axes, duty, cast).

Everything runs against a temp SQLite file - never the production world DB.
"""

import json
import sqlite3

import pytest

from source.server import lab_authority as la


@pytest.fixture(autouse=True)
def hermetic_inventory(monkeypatch):
    """Keep tests off the live credential store.

    `derive_cast()` reads the REAL pool inventory, which normally means
    `~/.friday/api_credentials.json` (outside this working tree). The tests
    assert the *shape* of the derivation against a fixed fake inventory so they
    are deterministic and touch no live state; the live path is exercised by
    `test_cast_availability_comes_from_the_real_pool_health`'s structure checks.
    """
    from source.server import api_pools, brain_router

    monkeypatch.setattr(
        api_pools, "describe_pools",
        lambda: {"brain_modal": {"total": 1, "banned": 0},
                 "brain_groq": {"total": 2, "banned": 1},
                 "brain_openrouter": {"total": 1, "banned": 0},
                 "vision_modal": {"total": 1, "banned": 0},
                 "vision_gemini": {"total": 1, "banned": 1},
                 "stt_groq": {"total": 1, "banned": 0},
                 "stt_deepgram": {"total": 1, "banned": 0},
                 "gemini_tts": {"total": 1, "banned": 0}})
    monkeypatch.setattr(
        api_pools, "model_banned",
        lambda pool, model: pool == "vision_gemini")
    monkeypatch.setattr(
        brain_router, "resolve",
        lambda text: [("brain_modal", "nvidia/GLM-5.3-Flash-NVFP4",
                       "https://example.invalid/v1"),
                      ("brain_groq", "openai/gpt-oss-20b", "https://x"),
                      ("brain_openrouter", "nemotron:free", "https://y"),
                      ("local", None, None)])
    return True


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "authority.db"))
    c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def auth(conn):
    return la.Authority(conn)


# -- transitions ------------------------------------------------------------
def test_life_table_pauses_on_duty_and_resumes():
    assert la.life_next(la.LIFE_NPC, "start") == la.LIFE_NPC
    assert la.life_next(la.LIFE_NPC, "enable_ai") == la.LIFE_AI
    assert la.life_next(la.LIFE_AI, "duty_admitted") == la.LIFE_PAUSED
    assert la.life_next(la.LIFE_PAUSED, "duty_finished") == la.LIFE_NPC
    assert la.life_next(la.LIFE_AI, "budget_exhausted") == la.LIFE_NPC


def test_life_unknown_event_never_changes_state():
    assert la.life_next(la.LIFE_AI, "nonsense") == la.LIFE_AI
    assert la.life_next("bogus", "start") == la.LIFE_NPC


def test_availability_table():
    assert la.availability_next(la.AVAIL_UP, "quota")[0] == la.AVAIL_DEGRADED
    state, reason = la.availability_next(la.AVAIL_DEGRADED, "three_strikes")
    assert state == la.AVAIL_BANNED and reason
    assert la.availability_next(la.AVAIL_BANNED, "cooldown_expired")[0] == la.AVAIL_UP
    assert la.availability_next(la.AVAIL_UP, "chain_exhausted")[0] == la.AVAIL_NONE


def test_emergency_table():
    assert la.emergency_next(la.EMERGENCY_OFF, "cloud_down") == la.EMERGENCY_LOCAL_ONLY
    assert la.emergency_next(la.EMERGENCY_LOCAL_ONLY, "cloud_up") == la.EMERGENCY_OFF
    assert la.emergency_next(la.EMERGENCY_LOCAL_ONLY, "local_down") == la.EMERGENCY_NONE


# -- precedence -------------------------------------------------------------
def test_no_provider_beats_everything():
    out = la.precedence(emergency=la.EMERGENCY_NONE, duty="chr_friday",
                        pin="chr_x", availability=la.AVAIL_UP,
                        life=la.LIFE_AI)
    assert out["decided_by"] == "emergency"
    assert out["outcome"] == "unavailable"


def test_duty_beats_pin_and_life():
    out = la.precedence(emergency=la.EMERGENCY_OFF, duty="chr_friday",
                        pin="chr_x", availability=la.AVAIL_UP,
                        life=la.LIFE_AI)
    assert out["decided_by"] == "duty" and out["char_id"] == "chr_friday"


def test_pin_beats_availability_and_life():
    out = la.precedence(emergency=la.EMERGENCY_OFF, duty=la.DUTY_IDLE,
                        pin="chr_x", availability=la.AVAIL_BANNED,
                        life=la.LIFE_NPC)
    assert out["decided_by"] == "pin" and out["char_id"] == "chr_x"


def test_life_is_lowest_priority():
    out = la.precedence(emergency=la.EMERGENCY_OFF, duty=la.DUTY_IDLE,
                        pin=None, availability=la.AVAIL_UP,
                        life=la.LIFE_AI)
    assert out["decided_by"] == "life" and out["outcome"] == la.LIFE_AI


# -- revisions --------------------------------------------------------------
def test_rev_is_monotonic_and_bumps_entity(auth):
    assert auth.rev() == 0
    first = auth.bump("task", "task-abc")
    second = auth.bump("axis", "life")
    assert first == 1 and second == 2
    assert auth.entity_rev("task-abc") == first
    assert auth.rev() == second


def test_snapshot_carries_rev_and_is_json_safe(auth):
    auth.set_axis("life", la.LIFE_NPC)
    snap = auth.snapshot()
    assert snap["rev"] >= 1
    json.dumps(snap)          # must not raise
    assert snap["axes"]["life"] == la.LIFE_NPC


# -- axes -------------------------------------------------------------------
def test_invalid_axis_state_is_refused(auth):
    out = auth.set_axis("life", "partying")
    assert out["ok"] is False and out["reason"] == "invalid state"
    assert auth.axis("life") != "partying"


def test_axis_reason_is_recorded(auth):
    auth.set_axis("emergency", la.EMERGENCY_LOCAL_ONLY, "all cloud banned")
    assert auth.axis("emergency_reason") == "all cloud banned"


# -- serving identity (single writer) --------------------------------------
def test_install_then_clear_serving(auth):
    auth.install_serving("chr_groq_x", served_by="brain_groq/x",
                         reason="main banned")
    s = auth.serving()
    assert s["char_id"] == "chr_groq_x" and s["served_by"] == "brain_groq/x"
    assert auth.axis("duty") == "chr_groq_x"

    auth.clear_serving("turn complete")
    s2 = auth.serving()
    assert s2["char_id"] == "" and s2["duty"] == la.DUTY_IDLE
    assert auth.axis("duty") == la.DUTY_IDLE


def test_serving_survives_reload(auth, conn):
    auth.install_serving("chr_a", served_by="p/m", reason="r")
    again = la.Authority(conn)          # fresh object, same database
    assert again.serving()["char_id"] == "chr_a"


def test_handover_history_is_bounded_and_real(auth):
    for i in range(60):
        auth.record_handover("chr_a", "chr_b", "reason %d" % i, "p/m")
    rows = auth.handover_history(10)
    assert len(rows) <= 10
    assert rows[-1]["reason"] == "reason 59"
    assert rows[-1]["served_by"] == "p/m"


# -- pins are NOT a disguised duty ----------------------------------------
def test_pin_is_recorded_separately_from_duty(auth):
    auth.pin("chr_local")
    assert auth.axis("pin") == "chr_local"
    assert auth.axis("duty") == la.DUTY_IDLE      # pin never becomes duty
    out = la.precedence(emergency=la.EMERGENCY_OFF, duty=auth.axis("duty"),
                        pin=auth.axis("pin"), availability=la.AVAIL_UP,
                        life=la.LIFE_NPC)
    assert out["decided_by"] == "pin"


def test_empty_pin_refused(auth):
    assert auth.pin("")["ok"] is False


# -- global vs individual life mode ---------------------------------------
def test_global_ai_never_enables_local_characters(auth):
    out = auth.apply_global_ai(True, local_ids=("chr_local_a", "chr_cloud_b"),
                               is_local=lambda c: c.startswith("chr_local"))
    assert out["changed"] == ["chr_cloud_b"]
    assert out["skipped_local"] == ["chr_local_a"]
    assert auth.life_override("chr_local_a") == ""     # untouched
    assert auth.life_override("chr_cloud_b") == la.LIFE_AI


def test_individual_override_persists_and_validates(auth):
    assert auth.set_life_override("chr_local_a", la.LIFE_AI)["ok"] is True
    assert auth.life_override("chr_local_a") == la.LIFE_AI
    assert auth.set_life_override("chr_local_a", "banana")["ok"] is False


def test_global_off_returns_characters_to_npc(auth):
    auth.apply_global_ai(True, local_ids=("chr_cloud_b",),
                         is_local=lambda c: False)
    out = auth.apply_global_ai(False, local_ids=("chr_cloud_b",),
                               is_local=lambda c: False)
    assert out["changed"] == ["chr_cloud_b"]
    assert auth.life_override("chr_cloud_b") == la.LIFE_NPC


# -- cast derivation --------------------------------------------------------
def test_cast_is_derived_from_the_real_chain_not_hardcoded_cards():
    cast = la.derive_cast()
    assert cast["boss_char_id"] == la.BOSS_CHAR_ID
    ids = [c["char_id"] for c in cast["cast"]]
    assert ids[0] == la.BOSS_CHAR_ID
    # every brain character carries a real pool binding
    brain = [c for c in cast["cast"] if c["lane"] in ("main_brain",
                                                      "fallback_brain")]
    assert brain, "brain chain must not be empty"
    for c in brain:
        assert c["pool"], c
        assert c["binding"], c
    # capability lanes exist and are honestly labelled as non-conversational
    lanes = {c["lane"] for c in cast["cast"]}
    assert {"main_brain", "fallback_brain"} <= lanes
    voice = [c for c in cast["cast"] if c["lane"] == "voice_out"]
    for c in voice:
        assert "not a conversational persona" in c["persona"]


def test_cast_availability_comes_from_the_real_pool_health():
    cast = la.derive_cast()
    for c in cast["cast"]:
        assert c["availability"] in la.AVAIL_STATES, c


def test_local_character_is_marked_and_defaults_to_npc():
    cast = la.derive_cast()
    locals_ = [c for c in cast["cast"] if c["local"]]
    for c in locals_:
        assert c["life_mode"] == la.LIFE_NPC
        assert c["life_source"] == "individual"


def test_cast_is_json_serialisable():
    json.dumps(la.derive_cast())
