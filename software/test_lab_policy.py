"""LAB-3R5 part D: bulk 'AI powered' policy over the ONE authority.

Covered here (no network, no router changes):
  * bulk enable touches eligible NONLOCAL brain-lane residents only,
    with local classification from the REAL pool inventory (derive_cast);
  * individual opt-in/opt-out of a LOCAL resident (the only path);
  * primary (boss) opt-out persists;
  * emergency-runtime axis changes never buy AI life (no conflation);
  * local fallback availability never writes any life override;
  * public_state exposes one authoritative `life` shape (override wins);
  * bridge refusals are honest (unconfigured / invalid mode / empty id);
  * shipped UI wires the bulk + individual controls on that payload.
"""

import pytest

from source.server import lab_authority as la
from source.server.status_ui import WebviewBridge


@pytest.fixture()
def auth(tmp_path):
    la.reset_shared()
    la.configure(tmp_path / "authority.db")
    instance = la.shared()
    yield instance
    la.reset_shared()


@pytest.fixture()
def chain(monkeypatch):
    """A brain chain with one NONLOCAL primary and one LOCAL fallback,
    derived exactly the way production derives it (brain_router.resolve)."""
    monkeypatch.setattr(la, "_brain_candidates", lambda: [
        {"pool": "brain_groq", "model": "gpt-oss-20b"},
        {"pool": "local", "model": "qwen3:8b"},
    ])


class TestBulkSwitch:
    def test_bulk_enables_nonlocal_only(self, auth, chain):
        out = auth.apply_global_ai(
            True, local_ids=("chr_friday", "chr_local"),
            is_local=lambda cid: cid == "chr_local")
        assert out["ok"] is True
        assert "chr_friday" in out["changed"]
        assert out["skipped_local"] == ["chr_local"]
        assert auth.life_override("chr_friday") == la.LIFE_AI
        assert auth.life_override("chr_local") == ""   # untouched

    def test_bridge_bulk_uses_cast_classification(self, auth, chain):
        bridge = WebviewBridge(None, None)
        out = bridge.lab_set_global_ai(True)
        assert out["ok"] is True, out
        # nonlocal primary (index 0 -> chr_friday) enabled
        assert auth.life_override("chr_friday") == la.LIFE_AI
        # the LOCAL fallback is skipped by the authority's own rule
        assert auth.life_override("chr_local") == ""
        assert "chr_local" in (out.get("skipped_local") or [])
        assert "chr_friday" in (out.get("changed") or [])
        assert out.get("cast_source"), out

    def test_bridge_bulk_off_opts_everyone_out(self, auth, chain):
        bridge = WebviewBridge(None, None)
        bridge.lab_set_global_ai(True)
        out = bridge.lab_set_global_ai(False)
        assert out["ok"] is True, out
        assert auth.life_override("chr_friday") == la.LIFE_NPC

    def test_primary_opt_out_persists(self, auth, chain):
        bridge = WebviewBridge(None, None)
        bridge.lab_set_global_ai(True)
        out = bridge.lab_set_life_mode("chr_friday", "npc")
        assert out["ok"] is True, out
        assert auth.life_override("chr_friday") == la.LIFE_NPC
        # and it survives an explicit re-read
        assert la.shared().life_override("chr_friday") == la.LIFE_NPC


class TestIndividualAndEmergencySeparation:
    def test_local_resident_only_changes_individually(self, auth, chain):
        bridge = WebviewBridge(None, None)
        bridge.lab_set_global_ai(True)                # skips chr_local
        assert auth.life_override("chr_local") == ""
        out = bridge.lab_set_life_mode("chr_local", la.LIFE_AI)
        assert out["ok"] is True, out
        assert auth.life_override("chr_local") == la.LIFE_AI

    def test_invalid_mode_and_empty_id_refused(self, auth, chain):
        bridge = WebviewBridge(None, None)
        assert bridge.lab_set_life_mode("chr_local", "banana").get("ok") \
            is False
        assert bridge.lab_set_life_mode("", "ai").get("ok") is False
        assert auth.life_override("chr_local") == ""

    def test_unconfigured_authority_is_honest(self):
        la.reset_shared()
        bridge = WebviewBridge(None, None)
        out = bridge.lab_set_global_ai(True)
        assert out.get("ok") is False and out.get("configured") is False
        out = bridge.lab_set_life_mode("chr_friday", "ai")
        assert out.get("ok") is False and out.get("configured") is False

    def test_emergency_axis_never_buys_ai_life(self, auth, chain):
        bridge = WebviewBridge(None, None)
        bridge.lab_set_global_ai(False)
        before = auth.life_override("chr_friday")
        auth.set_axis("emergency", la.EMERGENCY_LOCAL_ONLY, "cloud down")
        assert auth.life_override("chr_friday") == before, (
            "emergency/runtime state must never change life mode")

    def test_local_availability_never_writes_life_override(self, auth,
                                                           chain):
        # the local model being present/healthy in the chain (derive_cast
        # runs against real inventory) must not buy AI life by itself
        cast = la.derive_cast()
        assert cast["cast"], cast
        assert auth.life_override("chr_local") == ""
        assert auth.life_override("chr_friday") == ""


class TestAuthoritativeLifeShape:
    def test_public_state_life_override_wins(self, auth, chain):
        auth.set_life_override("chr_friday", la.LIFE_AI)
        payload = la.public_state(auth)
        life = payload["life"]
        assert life["chr_friday"] == {
            "mode": la.LIFE_AI, "override": True, "local": False,
            "source": "override"}, life["chr_friday"]
        assert life["chr_local"]["mode"] == la.LIFE_NPC
        assert life["chr_local"]["local"] is True
        assert life["chr_local"]["source"] != "override"

    def test_life_defaults_never_override_by_availability(self, auth, chain):
        payload = la.public_state(auth)
        # nothing written: the payload is a projection, not a mutation
        assert auth.life_override("chr_friday") == ""
        assert payload["life"]["chr_friday"]["mode"] in ("npc", "ai")


class TestUISourceContract:
    """The shipped UI must act on the ONE authority payload (source-level;
    live behaviour is proven by the isolated harness acceptance run)."""

    def test_ui_wires_bulk_and_individual_controls(self):
        from pathlib import Path
        root = Path(__file__).parent / "source" / "server" / "ui"
        js = (root / "app.js").read_text(encoding="utf-8")
        html = (root / "index.html").read_text(encoding="utf-8")
        assert 'call("lab_set_global_ai", on)' in js
        assert 'call("lab_set_life_mode", charId, next)' in js
        assert 'auth.life' in js or "auth.life" in js.replace("'", '"')
        assert 'id="lab-ai"' in html
        assert 'id="lab-ai-on"' in html and 'id="lab-ai-off"' in html
