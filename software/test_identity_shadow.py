"""Synthetic end-to-end checks of the opt-in catalog and duty shadow."""
import pytest

from source.server.lab_memory import LabMemory
from source.server.world import sim
from source.server.world.identity_catalog import IdentityCatalog, Route
from source.server.world.identity_shadow import Attempt, DutyShadow

PRIMARY = Route("modal", "primary", "vendor/model")
FALLBACK = Route("groq", "secondary", "vendor/model")
OTHER_VISION = Route("other", "vision", "vendor/vision")
UNBOUND = Route("modal", "different-deployment", "vendor/model")


def config(route, *caps, kind="reasoning", enabled=True):
    return {**route.public(), "kind": kind, "capabilities": caps, "enabled": enabled}


@pytest.fixture
def shadow(tmp_path):
    catalog = IdentityCatalog(tmp_path / "catalog.db")
    catalog.reconcile([config(PRIMARY, "brain", "vision"), config(FALLBACK, "brain"),
                       config(OTHER_VISION, "vision"),
                       config(Route("voice", "stt", "whisper"), "listen", kind="service")],
                      complete=True, legacy_refs=[("memory", "friday")])
    return catalog, DutyShadow(catalog.path, tmp_path / "history.db", enabled=True)


def test_default_gate_does_not_open_either_store(tmp_path):
    catalog, history = (tmp_path / name for name in ("not-created.db", "also-not-created.db"))
    s = DutyShadow(catalog, history)
    assert s.begin_request("fixture") is None
    assert s.observe_selected(None, "brain", 1, PRIMARY) is None
    assert not s.finish(None, "success") and not s.end_request(None)
    assert s.snapshot() == {"enabled": False, "reason": "shadow gate disabled"}
    assert not catalog.exists() and not history.exists()


def test_primary_holds_both_then_only_brain_falls_back(shadow):
    _, s = shadow
    r = s.begin_request("turn-a")
    brain = s.observe_selected(r, "brain", 1, PRIMARY)
    vision = s.observe_selected(r, "vision", 1, PRIMARY)
    both = s.snapshot()["duty"]["current"]
    assert both["brain"]["character_id"] == both["vision"]["character_id"]
    assert both["brain"]["binding_id"] != both["vision"]["binding_id"]
    assert s.finish(brain, "failure")
    fallback = s.observe_selected(r, "brain", 2, FALLBACK)
    assert not s.finish(brain, "success")
    assert not s.finish(Attempt(r, "brain", 2), "success")
    after = s.snapshot()["duty"]
    assert after["current"]["brain"]["character_id"] != both["brain"]["character_id"]
    assert after["current"]["vision"] == both["vision"]
    assert after["current"]["brain"]["generation"] == 2
    assert after["current"]["brain"]["request_id"] == "turn-a"
    assert after["last_success"] == {}
    assert s.finish(fallback, "success")
    assert not s.finish(fallback, "success")
    mid = s.snapshot()["duty"]
    assert set(mid["current"]) == {"vision"}
    assert mid["last_success"]["brain"]["route"] == FALLBACK.public()
    assert s.finish(vision, "success")
    assert not s.snapshot()["duty"]["current"]
    assert set(s.snapshot()["duty"]["last_success"]) == {"brain", "vision"}


def test_stale_request_and_generation_never_retake_or_clear_duty(shadow):
    _, s = shadow
    old = s.begin_request("older")
    newer = s.begin_request("newer")
    a = s.observe_selected(old, "brain", 1, PRIMARY)
    b = s.observe_selected(newer, "brain", 1, FALLBACK)
    v = s.observe_selected(old, "vision", 1, OTHER_VISION)
    assert not s.finish(a, "failure")
    assert s.observe_selected(old, "brain", 99, PRIMARY) is None
    assert s.end_request(old)
    assert not s.finish(v, "success")
    assert not s.end_request(old)
    assert s.snapshot()["duty"]["current"]["brain"]["request_id"] == "newer"
    assert s.finish(b, "cancelled")
    assert s.observe_selected(newer, "brain", 1, FALLBACK) is None
    assert s.observe_selected(old, "brain", 100, PRIMARY) is None
    assert s.snapshot()["duty"] == {"current": {}, "last_success": {}}
    assert s.end_request(newer)
    reopened_id = s.begin_request("newer")
    assert s.observe_selected(reopened_id, "brain", 1, FALLBACK)
    assert not s.end_request(newer)


def test_restart_retains_only_history_and_fences_previous_writer(shadow, tmp_path):
    catalog, old = shadow
    request = old.begin_request("same-id")
    assert old.finish(old.observe_selected(request, "brain", 1, PRIMARY), "success")
    pending = old.observe_selected(request, "brain", 2, FALLBACK)
    new = DutyShadow(catalog.path, tmp_path / "history.db", enabled=True)
    assert not new.snapshot()["duty"]["current"]
    assert not new.finish(pending, "success")
    assert not old.finish(pending, "success")
    assert not old.finish(pending, "failure")
    assert old.snapshot()["reason"] == "shadow session replaced"
    saved = new.snapshot()["duty"]["last_success"]
    assert saved["brain"]["route"] == PRIMARY.public()
    request = new.begin_request("same-id")
    failure = new.observe_selected(request, "brain", 1, FALLBACK)
    assert new.finish(failure, "failure")
    assert new.snapshot()["duty"]["last_success"] == saved


def test_exact_unbound_observation_never_promotes_model_or_service(shadow):
    _, s = shadow
    r = s.begin_request("unmatched")
    unknown = s.observe_selected(r, "brain", 1, UNBOUND)
    row = s.snapshot()["duty"]["current"]["brain"]
    assert row["resolution"] == "unbound" and row["route"] == UNBOUND.public()
    assert row["binding_id"] is row["character_id"] is None
    assert not any(c["duty"] for c in s.snapshot()["roster"]["characters"])
    assert s.finish(unknown, "success")
    assert s.snapshot()["duty"]["last_success"]["brain"]["resolution"] == "unbound"
    assert s.observe_selected(r, "vision", 1, FALLBACK)
    assert s.snapshot()["duty"]["current"]["vision"]["resolution"] == "unbound"
    assert s.observe_selected(r, "vision", 2, OTHER_VISION)
    assert s.snapshot()["duty"]["current"]["vision"]["resolution"] == "bound"


def test_reconciliation_rejects_disabled_retired_then_reuses_binding(shadow):
    catalog, s = shadow
    r = s.begin_request("catalog-change")
    first = s.observe_selected(r, "brain", 1, PRIMARY)
    base = s.snapshot()["duty"]["current"]["brain"]
    catalog.reconcile([config(PRIMARY, "brain", enabled=False)], complete=True)
    assert s.observe_selected(r, "brain", 2, PRIMARY) is None
    assert s.observe_selected(r, "vision", 1, PRIMARY) is None  # retired capability
    assert s.snapshot()["duty"]["current"]["brain"]["binding_status"] == "disabled"
    assert s.finish(first, "success")  # already admitted
    catalog.reconcile([], complete=True)
    assert s.observe_selected(r, "brain", 2, PRIMARY) is None
    archived = s.snapshot(base["character_id"])
    assert not archived["town"]["residents"]
    assert archived["roster"]["selected_character_id"] == archived["town"]["selected_character_id"]
    catalog.reconcile([config(PRIMARY, "brain")], complete=True)
    assert s.observe_selected(r, "brain", 2, PRIMARY)
    new = s.snapshot()["duty"]["current"]["brain"]
    assert (new["binding_id"], new["character_id"]) == (base["binding_id"], base["character_id"])


def test_coherent_identity_selection_and_rollback_preserve_observation_provenance(shadow):
    catalog, s = shadow
    earlier = s.snapshot()["catalog_revision"]
    fresh = Route("other", "new", "vendor/new")
    latest = catalog.reconcile([config(fresh, "vision")], complete=True)
    r = s.begin_request("selected")
    v = s.observe_selected(r, "vision", 1, fresh)
    char_id = s.snapshot()["duty"]["current"]["vision"]["character_id"]
    pair = s.snapshot(char_id)
    for view in (pair["roster"], pair["town"]):
        assert (view["catalog_revision"], view["selected_character_id"]) == (latest, char_id)
    assert pair["town"]["residents"][0]["id"] == char_id
    assert all("character_id" not in svc for svc in pair["roster"]["services"])
    assert pair["roster"]["unresolved"][0]["legacy_id"] == "friday"
    assert s.snapshot(pair["town"]["residents"][0]["name"])["selected_character_id"] is None
    catalog.activate_revision(earlier)
    after = s.snapshot(char_id)
    assert after["catalog_revision"] == earlier and after["selected_character_id"] is None
    assert after["duty"]["current"]["vision"]["resolution"] == "unbound"
    assert after["duty"]["current"]["vision"]["admitted_revision"] == latest
    assert s.finish(v, "success")
    assert s.snapshot()["duty"]["last_success"]["vision"]["character_id"] == char_id


def test_shadow_and_npc_preserve_legacy_experience_owner(shadow, tmp_path):
    _, s = shadow
    memory_path = tmp_path / "legacy.db"
    memory = LabMemory(memory_path, world_db=tmp_path / "unused-world.db")
    memory.record_experience("friday", tick=1, kind="observation", text="Original owner")
    world = sim.new_world()
    before = {c.id: list(c.memory) for c in world.characters}
    db_before = memory_path.read_bytes()
    calls = []
    for _ in range(120):
        sim.tick(world, planner=lambda *args: calls.append(args))
        s.snapshot()
    assert not calls and world.planner_calls == 0
    assert {c.id: c.memory for c in world.characters} == before
    assert memory_path.read_bytes() == db_before
    assert memory.scrapbook("friday")["experiences"][0]["char_id"] == "friday"

