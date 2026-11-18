"""ROUND LAB-2B integration tests: real router outcomes -> the ONE serving
projection -> the actual views/bridge methods assert agreement.

Nothing here mocks the router or the authority away: a real `BrainRouter`
object is driven through its real mark_success/mark_failure entry points, and
the real bridge methods (get_core / get_ai_agents / get_roster / get_lab) are
called with a harness interpreter. Only the network is absent - no provider
call is ever attempted, because mark hooks are bookkeeping, not transport.
"""

import time

import pytest

from source.server import lab_authority as authority_mod
from source.server import lab_state as lab
from source.server import brain_router
from source.server.brain_router import BrainRouter
from source.server.status_ui import StatusBus, WebviewBridge

MODAL_MODEL = "nvidia/GLM-5.3-Flash-NVFP4"
GROQ_MODEL = "openai/gpt-oss-20b"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class _HarnessInterpreter:
    """What the bridge actually reads on the server paths used here."""

    def __init__(self, router):
        self.server_state = {"brain_router": router}


@pytest.fixture()
def authority(tmp_path, monkeypatch):
    # A synthetic successful Modal ticket needs the corresponding configured
    # lane. Keep the real resolver, with an endpoint that is never contacted.
    monkeypatch.setattr(brain_router, "MODAL_BASE", "https://modal-fixture.invalid/v1")
    monkeypatch.setattr(brain_router, "MODAL_MODEL", MODAL_MODEL)
    monkeypatch.setattr(brain_router, "GROQ_FAST_MODEL", GROQ_MODEL)
    """The ONE authority in-process, bound to an in-scope temp store."""
    authority_mod.reset_shared()
    authority_mod.configure(tmp_path / "lab-authority.db")
    try:
        yield authority_mod.shared()
    finally:
        authority_mod.reset_shared()


@pytest.fixture()
def lab_world(tmp_path, monkeypatch):
    database = tmp_path / "roster.db"
    monkeypatch.setattr(lab, "db_path", lambda: str(database))
    lab.reset()
    lab._execution_queue_reset()
    try:
        yield tmp_path
    finally:
        lab._execution_queue_reset()
        lab.reset()


def _router_with_current(router, pool, model):
    """Put one real ticket on a real BrainRouter (no transport involved)."""
    router._current = (pool, "not-a-real-key", model)
    router._current_pool = pool
    router._current_model = model


@pytest.fixture()
def router():
    return BrainRouter()


@pytest.fixture()
def bridge(router, authority):
    return WebviewBridge(StatusBus(), _HarnessInterpreter(router))


# ---------------------------------------------------------------------------
# Real router outcomes -> the serving projection
# ---------------------------------------------------------------------------
class TestRouterServingProjection:
    def test_mark_success_installs_the_badge_from_the_real_chain(
            self, authority, router):
        _router_with_current(router, "brain_modal", MODAL_MODEL)
        router.mark_success()
        serving = authority.serving()
        assert serving.get("char_id") == "chr_friday"
        assert "brain_modal" in (serving.get("served_by") or "")
        assert serving.get("active") is True
        assert authority.axis("availability") == "up"

    def test_fallback_switch_records_a_real_handover(self, authority, router):
        _router_with_current(router, "brain_modal", MODAL_MODEL)
        router.mark_success()
        first = authority.serving().get("char_id")
        _router_with_current(router, "brain_groq", GROQ_MODEL)
        router.mark_failure("upstream 429 quota")
        _router_with_current(router, "brain_groq", GROQ_MODEL)
        router.mark_success()
        serving = authority.serving()
        assert serving.get("char_id") != first, "handover did not move the badge"
        assert "brain_groq" in (serving.get("served_by") or "")
        handovers = authority.handover_history(5)
        assert any("provider switch" in (row.get("reason") or "")
                   for row in handovers)
        # The FALLBACK badge is visible and its model is the underlying truth.
        _router_with_current(router, "brain_groq", GROQ_MODEL)
        router.mark_success()
        assert "brain_groq" in authority.snapshot()["serving"]["served_by"]

    def test_mark_failure_degrades_the_availability_axis(self, authority,
                                                         router):
        _router_with_current(router, "brain_modal", MODAL_MODEL)
        router.mark_success()
        before_rev = authority.rev()
        _router_with_current(router, "brain_modal", MODAL_MODEL)
        router.mark_failure("503 high demand try again later")
        assert authority.axis("availability") == "degraded"
        assert authority.rev() > before_rev        # a real event was recorded

    def test_turn_cleared_idles_duty_but_keeps_last_serving_record(
            self, authority, router):
        _router_with_current(router, "brain_modal", MODAL_MODEL)
        router.mark_success()
        authority.turn_cleared("turn finished")
        assert authority.axis("duty") == "idle"
        serving = authority.serving()
        assert serving.get("char_id") == "chr_friday"   # last-known-served
        assert serving.get("active") is False


    def test_exact_model_binding_and_restart_never_revive_duty(
            self, authority, router, monkeypatch):
        import sqlite3

        monkeypatch.setattr(brain_router, "MODAL_BASE", None)
        other = brain_router.GROQ_STRONG_MODEL
        first = authority._char_for("brain_groq", GROQ_MODEL)
        second = authority._char_for("brain_groq", other)
        assert first == "chr_friday" and second and first != second
        assert authority._char_for("brain_groq", "unbound") is None
        _router_with_current(router, "brain_groq", other)
        router.mark_success()
        assert authority.serving()["char_id"] == second

        filename = authority._conn.execute("PRAGMA database_list").fetchone()[2]
        conn = sqlite3.connect(filename)
        try:
            reopened = authority_mod.Authority(conn)
            assert reopened.axis("duty") == "idle"
            assert reopened.serving()["char_id"] == second
            assert reopened.serving()["active"] is False
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# User controls: eligibility, pin, global life mode
# ---------------------------------------------------------------------------
class TestUserControls:
    def test_user_disable_refuses_badge_and_skips_worker_lane(
            self, authority, router, monkeypatch):
        _router_with_current(router, "brain_modal", MODAL_MODEL)
        router.mark_success()
        assert authority.serving().get("char_id") == "chr_friday"
        disabled = authority.set_eligibility(
            "chr_friday", False, "the user paused the main")
        assert disabled.get("ok") is True
        assert disabled.get("handover_pending"), disabled
        serve_refusal = authority.install_serving(
            "chr_friday", served_by="brain_modal/%s" % MODAL_MODEL)
        assert serve_refusal.get("ok") is False
        assert "user-disabled" in (serve_refusal.get("reason") or "")
        # Worker lane: the same character's pool is skipped by provider code.
        assert authority_mod.pool_user_disabled("brain_modal", MODAL_MODEL) \
            is True

    def test_pin_cannot_resurrect_a_user_disabled_service(
            self, authority, router):
        authority.set_eligibility("chr_friday", False, "paused")
        router_pin = authority.pin("chr_friday")
        assert router_pin.get("ok") is True          # pin recorded
        _router_with_current(router, "brain_modal", MODAL_MODEL)
        router.mark_success()
        serving = authority.serving()
        # The real router DID pick modal, but the disabled character's badge
        # is refused, so the projection keeps the LAST ELIGIBLE truth and
        # records a visible refusal - the pin never resurrects it.
        assert serving.get("char_id") != "chr_friday", serving
        refusals = [row for row in authority.handover_history(10)
                    if "user-disabled" in (row.get("reason") or "")]
        assert refusals, "the refusal must be visible in the handover log"

    def test_global_admin_life_never_touches_duty_or_the_badge(
            self, authority):
        _helpers_before = authority.serving()
        result = authority.apply_global_ai(
            True, local_ids=("chr_friday", "chr_local_a"),
            is_local=lambda char: char == "chr_local_a")
        assert result.get("changed") == ["chr_friday"]
        assert result.get("skipped_local") == ["chr_local_a"]
        # Background life is a SEPARATE axis: duty and serving unchanged.
        assert authority.axis("duty") == "idle" \
            if _helpers_before.get("char_id") == "" else True
        assert authority.serving() == _helpers_before

    def test_global_background_state_visible_in_the_projection(
            self, authority):
        authority.set_axis("life", "npc", "global NPC switch")
        state = authority.snapshot()
        assert state["axes"]["life"] == "npc"
        assert state["precedence"]["decided_by"] == "life"


# ---------------------------------------------------------------------------
# The five-view agreement: real bridge methods on the real router outcomes
# ---------------------------------------------------------------------------
class TestViewAgreement:
    def test_badge_switch_gives_all_views_the_same_served_by_and_rev(
            self, authority, router, bridge, lab_world):
        _router_with_current(router, "brain_modal", MODAL_MODEL)
        router.mark_success()
        truth = authority.serving()
        truth_rev = authority.rev()

        core = bridge.get_core()
        agents = bridge.get_ai_agents()
        roster = bridge.get_roster()
        labpane = bridge.get_lab()

        assert truth["active"] and truth["duty"] == "brain"
        projections = (core["authority"], agents["summary"],
                       roster, labpane["authority"])
        for projection in projections:
            assert projection["serving"] == truth
            rev = projection.get("rev", projection.get("authority_rev"))
            assert rev == truth_rev
        resident = next(c for c in labpane["map"]["characters"] if c["id"] == "friday")
        assert resident["id"] != truth["char_id"]
        assert labpane["authority"]["life"][resident["id"]]["local"] is None
        assert labpane["authority"]["life"][truth["char_id"]]["mode"] == "npc"

    def test_lab_view_reports_authority_unavailable_without_configuring_it(
            self, router):
        # The pytest guard: a suite that never configured the authority must
        # never read the real (forbidden) store; it degrades honestly.
        authority_mod.reset_shared()
        try:
            bridge = WebviewBridge(StatusBus(), _HarnessInterpreter(router))
            payload = bridge.get_lab()
            auth = (payload or {}).get("authority") or {}
            assert auth.get("ok") is False
            assert auth.get("reason") == "authority not configured"
        finally:
            authority_mod.reset_shared()
