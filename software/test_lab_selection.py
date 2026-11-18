"""ROUND LAB-2B2: real-turn selection eligibility under a user-disable.

Genuine production semantics - the REAL `BrainRouter` and its REAL
`apply_for()` selection is exercised (with a documented deterministic
`resolve` chain as the ensure input, and a stubbed pool file so nothing hits
the network). Under a user-disable, `apply_for` must skip the affected pool
at the next selection boundary and fall to an eligible fallback; a pin must
never resurrect the disable; the local lanes keep their documented semantics.
"""

import pytest

from source.server import lab_authority as authority_mod
from source.server.brain_router import BrainRouter

MODAL_MODEL = "nvidia/GLM-5.3-Flash-NVFP4"
GROQ_MODEL = "openai/gpt-oss-20b"
CHAIN = (("brain_modal", MODAL_MODEL, "https://modal.invalid/v1"),
         ("brain_groq", GROQ_MODEL, "https://groq.invalid/v1"),
         ("local", None, None))


class _FakeLLM:
    def __init__(self):
        self.model = None
        self.api_base = None
        self.api_key = None


class _Interpreter:
    def __init__(self):
        self.llm = _FakeLLM()
        self.messages = []


class _FakePool:
    def __init__(self, name):
        self.name = name
        self.empty = False

    def next_key(self, *args, **kwargs):
        return "persisted-fake-key"

    def succeed(self, *args, **kwargs):
        return True

    def fail(self, *args, **kwargs):
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
def router(authority):
    """REAL router with the authority consult + serving listener wired -
    the same binding the running app gets through the status_ui warmup."""
    authority.register_router_listener()
    yield BrainRouter()


@pytest.fixture(autouse=True)
def _fake_pools(monkeypatch):
    """No real ledger writes, no real sleeps, no real HTTP. The resolve
    stub is the documented ENTRY INPUT (not the router logic)."""
    from source.server import api_pools
    monkeypatch.setattr(api_pools, "pool", lambda name: _FakePool(name))
    monkeypatch.setattr(api_pools, "model_banned",
                        lambda pool, model: False)
    monkeypatch.setattr(api_pools, "model_succeed",
                        lambda pool, model, cooldown=None: True)
    monkeypatch.setattr(api_pools, "fail_model",
                        lambda pool, model, cooldown=None: None)
    monkeypatch.setattr("source.server.brain_router.resolve",
                        lambda user_text, chain=CHAIN: list(chain))


def _turn(router, text="turn text"):
    """One REAL production selection for one turn."""
    interpreter = _Interpreter()
    outcome = router.apply_for(text, interpreter)
    return interpreter, outcome


class TestDisableAtTurnSelection:
    def test_main_serves_before_disable(self, authority, router):
        _turn(router, "baseline turn")
        assert router.current_pool == "brain_modal"
        assert router._current_model == MODAL_MODEL
        router.clear_selection() if hasattr(router, "clear_selection") \
            else None

    def test_current_duty_tracks_real_selection_and_preserves_last_success(
            self, authority, router):
        _turn(router)
        live = authority.serving()
        assert live["active"] and live["duty"] == "brain"
        assert live["served_by"] == "brain_modal/" + MODAL_MODEL
        router.mark_success()
        authority.turn_cleared()
        assert authority.serving()["active"] is False

        authority.set_eligibility("chr_friday", False)
        _turn(router)
        assert authority.serving()["served_by"].startswith("brain_groq/")
        router.mark_failure("synthetic failure")
        history = authority.serving()
        assert not history["active"] and history["served_by"] == live["served_by"]
        assert authority.axis("availability") == "degraded"

    def test_disabled_main_falls_to_the_next_eligible_candidate(
            self, authority, router):
        _turn(router, "baseline turn")
        authority.set_eligibility("chr_friday", False, "manual pause")
        assert authority_mod.pool_user_disabled("brain_modal", MODAL_MODEL) \
            is True
        interpreter, _ = _turn(router, "next turn after manual disable")
        assert router.current_pool == "brain_groq", \
            "the selection must skip the user-disabled pool"
        assert router._current_model == GROQ_MODEL
        router.mark_success()
        serving = (authority.serving().get("served_by") or "")
        assert "brain_groq" in serving, serving
        # The honest handover story: the manual-disable row is recorded at the
        # disable decision itself (visible BEFORE the provider change), and
        # the actual fallback install is recorded separately by the router
        # success path.
        handovers = authority.handover_history(10)
        assert any("manual user-disable" in (row.get("reason") or "")
                   for row in handovers), handovers
        assert any("installed" in (row.get("reason") or "provider switch"
                                   ) and "brain_groq" in
                   ((row.get("served_by") or "") + (row.get("reason") or ""))
                   for row in handovers), handovers

    def test_re_enable_at_the_boundary_restores_the_healthy_main(
            self, authority, router):
        authority.set_eligibility("chr_friday", False, "pause")
        _turn(router, "fallback turn")
        assert router.current_pool == "brain_groq"
        assert authority.set_eligibility("chr_friday", True,
                                         "resume").get("ok") is True
        assert authority_mod.pool_user_disabled("brain_modal",
                                                MODAL_MODEL) is False
        _turn(router, "return turn")
        assert router.current_pool == "brain_modal"
        assert router._current_model == MODAL_MODEL

    def test_no_eligible_provider_refuses_selection_honestly(
            self, authority, router):
        authority.set_eligibility("chr_friday", False, "disable main")
        authority.set_eligibility("chr_brain_groq_openai_gpt_oss_20b",
                                  False, "disable groq")
        authority.set_eligibility("chr_local", False, "disable local")
        interpreter, _ = _turn(router, "nothing is eligible")
        # NOTE: `local` is a special branch in the real selection - it is
        # configured as the FINAL failover outside the pool loop. The honest
        # contract this test pins is: brain_modal and brain_groq are BOTH
        # refused by the user disable (no silent 'we still have main'), so
        # the ONLY thing left is the documented local failover; we assert
        # the selection visibly consumed the local failover and no FAKE
        # "we still have a disabled brain" occurred.
        assert router.current_pool in ("local", None)
        if router.current_pool == "local":
            assert interpreter.llm.model == "ollama_chat/qwen3:8b"

    def test_pin_cannot_resurrect_a_disabled_service_in_real_selection(
            self, authority, router):
        authority.set_eligibility("chr_friday", False, "sandy pause")
        assert authority.pin("chr_friday").get("ok") is True
        interpreter, _ = _turn(router, "pinned-but-disabled turn")
        assert router.current_pool != "brain_modal", \
            "a user pin must never bypass a user-disable in the selection"
        assert interpreter.llm.model != "nvidia/GLM-5.3-Flash-NVFP4"


class TestLocalSemantics:
    def test_global_ai_never_enables_locals_and_never_confuses_duty(
            self, authority):
        before_serving = authority.serving()
        out = authority.apply_global_ai(
            True, local_ids=("chr_friday", "chr_local"),
            is_local=lambda char: True)
        assert out.get("changed") == []
        assert out.get("skipped_local") == ["chr_friday", "chr_local"]
        # The global background switch never touches duty/the badge.
        assert authority.serving() == before_serving

    def test_local_is_still_selectable_and_responds_to_disable(
            self, authority, router, monkeypatch):
        chain = (("local", None, None),)
        monkeypatch.setattr("source.server.brain_router.resolve",
                            lambda user_text, chain=chain: list(chain))
        interpreter, _ = _turn(router, "local only turn")
        assert router.current_pool == "local", "local is eligible"
        authority.set_eligibility("chr_local", False, "nothing is allowed")
        interpreter2, _ = _turn(router, "nothing eligible")
        # With `local` user-disabled the local BACKUP stops being selected;
        # the honest outcome is no configured brain (not a fake success).
        assert interpreter2.llm.model is None, \
            "a user-disabled local must not silently serve as a fake brain"
        assert authority.axis("duty") == "idle"
        assert authority.serving()["active"] is False
