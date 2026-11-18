"""Shipped Roster bridge against real isolated catalog/world/duty stores."""
from hashlib import sha256
from contextlib import closing
from types import SimpleNamespace

import pytest

from source.server import lab_state
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world import Store, sim
from source.server.world.identity_catalog import IdentityCatalog, Route
from source.server.world.identity_shadow import DutyShadow


def item(route, caps, kind="reasoning"):
    return {**route.public(), "capabilities": caps, "kind": kind}


@pytest.fixture
def source(tmp_path, monkeypatch):
    primary, fallback = Route("fixture", "primary", "primary-model"), Route("fixture", "fallback", "fallback-model")
    inventory = [item(primary, ["brain", "vision"]), item(fallback, ["brain"]),
                 item(Route("local", "local", "local-model"), ["brain"]),
                 item(Route("fixture", "listen", "listen-model"), ["listen"], "service")]
    catalog = IdentityCatalog(tmp_path / "catalog.db")
    catalog.reconcile(inventory, complete=True, legacy_refs=[("memory", "research")])
    shadow = DutyShadow(catalog.path, tmp_path / "duty.db", enabled=True)
    world = sim.new_world(seed=19)
    world.characters[0].memory = ["Synthetic original ownership"]
    with closing(Store(tmp_path / "world.db")) as store:
        store.save(world)
        world = store.load()
    monkeypatch.setattr(lab_state, "_world", world)
    def forbidden(*args, **kwargs):
        pytest.fail("Roster attempted startup, enrollment or world loading")
    monkeypatch.setattr(lab_state, "ensure_started", forbidden)
    monkeypatch.setattr(lab_state, "_load_world", forbidden)
    bridge = WebviewBridge(StatusBus(), SimpleNamespace(server_state={"identity_integration": SimpleNamespace(shadow=shadow)}))
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "1")
    return bridge, catalog, shadow, primary, fallback, inventory, world


def test_disabled_reader_never_bootstraps_or_opens_a_store(source, monkeypatch, tmp_path):
    bridge, _, shadow, *_ = source
    monkeypatch.delenv("FRIDAY_IDENTITY_READER", raising=False)
    monkeypatch.setattr(shadow, "snapshot", lambda *_a, **_k: pytest.fail("disabled shadow read"))
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    result = bridge.get_identity_views()
    assert not result["enabled"] and not result["available"]
    assert result["legacy_residents"][0]["resolution"] == "unresolved"
    assert "memory" not in result["legacy_residents"][0]
    assert result["selected_character_id"] is None
    assert before == {p.name: sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}


def test_reader_without_observation_source_is_honest_and_does_not_enroll(source):
    bridge, *_ = source
    bridge._interpreter.server_state.clear()
    result = bridge.get_identity_views()
    assert result["enabled"] and not result["available"]
    assert "Enrollment was not started" in result["reason"]


def test_exact_pair_and_selection_keep_services_and_legacy_separate(source, tmp_path):
    bridge, _, _, _, _, _, world = source
    before = sha256((tmp_path / "world.db").read_bytes()).hexdigest()
    pair = bridge.get_identity_views()
    selected = pair["roster"]["characters"][0]["id"]
    selected_pair = bridge.get_identity_views(selected, pair["catalog_revision"])
    assert selected_pair["selected_character_id"] == selected
    assert selected_pair["roster"]["read_id"] == selected_pair["town"]["read_id"]
    assert selected_pair["roster"]["catalog_revision"] == selected_pair["town"]["catalog_revision"]
    assert selected_pair["roster"]["services"][0]["representation"] == "procedural"
    assert selected_pair["roster"]["unresolved"][0]["legacy_id"] == "research"
    assert world.characters[0].memory == ["Synthetic original ownership"]
    assert before == sha256((tmp_path / "world.db").read_bytes()).hexdigest()


def test_split_duty_and_history_are_exact_not_name_inferences(source):
    bridge, _, shadow, primary, fallback, *_ = source
    request = shadow.begin_request("synthetic-owner")
    brain = shadow.observe_selected(request, "brain", 1, primary)
    vision = shadow.observe_selected(request, "vision", 1, primary)
    assert shadow.finish(brain, "failure")
    brain = shadow.observe_selected(request, "brain", 2, fallback)
    pair = bridge.get_identity_views()
    current = pair["duty"]["current"]
    assert current["brain"]["character_id"] != current["vision"]["character_id"]
    assert current["vision"]["route"] == primary.public()
    assert pair["duty"]["last_success"] == {}
    assert shadow.finish(brain, "success")
    finished = bridge.get_identity_views()
    assert "brain" not in finished["duty"]["current"]
    assert finished["duty"]["last_success"]["brain"]["character_id"] == current["brain"]["character_id"]
    assert finished["duty"]["current"]["vision"]["character_id"] == current["vision"]["character_id"]
    assert shadow.finish(vision, "cancelled")


@pytest.mark.parametrize("selected,revision", [("Aster", None),("not-a-uuid",None),(None,True),(None,-1),(None,"1")])
def test_invalid_selection_and_revision_do_not_fall_back_to_another_identity(source, selected, revision):
    bridge, *_ = source
    result = bridge.get_identity_views(selected, revision)
    assert not result["available"] and result["selected_character_id"] is None
    assert "Invalid" in result["reason"]


def test_removal_reappearance_and_pinned_revision_preserve_uuid(source):
    bridge, catalog, _, primary, _, inventory, _ = source
    before = bridge.get_identity_views()
    original = next(row for row in before["roster"]["characters"] if any(b["deployment"] == primary.deployment for b in row["bindings"]))
    catalog.reconcile([row for row in inventory if row["deployment"] != primary.deployment], complete=True)
    archived = bridge.get_identity_views(original["id"])
    assert next(row for row in archived["roster"]["characters"] if row["id"] == original["id"])["status"] == "archived"
    pinned = bridge.get_identity_views(original["id"], before["catalog_revision"])
    assert pinned["catalog_revision"] == before["catalog_revision"] and pinned["selected_character_id"] == original["id"]
    catalog.reconcile(inventory, complete=True)
    restored = bridge.get_identity_views(original["id"])
    assert next(row for row in restored["roster"]["characters"] if row["id"] == original["id"])["status"] == "configured"


def test_reader_failure_does_not_expose_private_paths_or_claim_success(source, monkeypatch):
    bridge, _, shadow, *_ = source
    def broken(*_args, **_kwargs):
        raise RuntimeError("private-path-and-secret")
    monkeypatch.setattr(shadow, "snapshot", broken)
    result = bridge.get_identity_views()
    assert not result["available"] and "private" not in result["reason"]
