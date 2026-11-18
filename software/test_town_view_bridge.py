"""Shipped Town reads and transport against an installed synthetic world."""
from contextlib import closing
from types import SimpleNamespace

import pytest

from source.server import lab_state as lab
from source.server.status_ui import StatusBus, WebviewBridge
from source.server.world import Store, sim
from test_identity_reader import linked, setup_reader  # pytest fixture reuse


def test_unloaded_town_never_starts_or_opens_stores(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Town opened or started an unloaded store")

    monkeypatch.setattr(lab, "_world", None)
    monkeypatch.setattr(lab, "_store", None)
    monkeypatch.setattr(lab, "_load_world", forbidden)
    monkeypatch.setattr(lab, "ensure_started", forbidden)
    monkeypatch.setenv("FRIDAY_WORLD_DB", str(tmp_path / "must-not-create.db"))
    monkeypatch.delenv("FRIDAY_IDENTITY_READER", raising=False)
    bridge = WebviewBridge(StatusBus())
    assert not bridge.get_town_view()["available"]
    assert not bridge.town_transport("step")["ok"]
    assert not bridge.town_transport("prune")["ok"]
    assert not list(tmp_path.iterdir())


def test_town_projects_installed_links_and_committed_events_without_memory_payload(setup_reader, tmp_path, monkeypatch):
    catalog, shadow, world, reader, _, ids = setup_reader
    monkeypatch.setenv("FRIDAY_IDENTITY_READER", "1")
    bridge = WebviewBridge(StatusBus(), SimpleNamespace(
        server_state={"identity_integration": SimpleNamespace(shadow=shadow)}))
    with closing(Store(tmp_path / "simulation.db")) as store:
        monkeypatch.setattr(lab, "_store", store)
        monkeypatch.setattr(lab, "_emit", lambda *_args: None)
        assert store.save(world)["ok"]
        link = linked(ids)
        plan = reader.snapshot(links=[link])["installation_proposal"]
        assert lab.install_catalog_residents(catalog, reader, plan, links=[link])["ok"]
        before = bridge.get_identity_views(ids["primary"])
        town = bridge.get_town_view(ids["primary"], before["catalog_revision"])
        assert town["selected_character_id"] == before["selected_character_id"]
        assert town["catalog_revision"] == before["catalog_revision"]
        assert {row["character_id"] for row in town["residents"] if row["status"] == "installed"} == set(ids.values())
        assert any(row["status"] == "unresolved" for row in town["residents"])
        assert all("memory" not in row for row in town["residents"])
        assert town["services"][0]["representation"] == "procedural"
        walk = {tuple(cell) for cell in town["walkable"]}
        assert not any((item["x"], item["y"]) in walk for item in town["fixtures"])

        sim._note(lab._world, lab._world.characters[0], "diagnostic", "pending fixture event")
        assert not any(event["text"] == "pending fixture event" for event in bridge.get_town_view()["journal"])
        assert bridge.town_transport("step")["ok"]
        committed = bridge.get_town_view()
        event_ids = [event["event_key"] for event in committed["journal"]]
        assert len(event_ids) == len(set(event_ids)) and event_ids
        assert committed["tick"] == town["tick"] + 1
        assert bridge.town_transport("save")["ok"] and bridge.town_transport("load")["ok"]
        assert bridge.get_town_view()["journal"] == committed["journal"]
        assert lab._world.characters[0].memory == world.characters[0].memory
        assert lab._world.planner_calls == 0

        monkeypatch.delenv("FRIDAY_IDENTITY_READER", raising=False)
        monkeypatch.setattr(shadow, "snapshot", lambda *_a, **_k: pytest.fail("disabled identity reader was used"))
        legacy = bridge.get_town_view()
        assert legacy["available"] and not legacy["identity_available"]
        assert all(row["status"] == "legacy" and row["character_id"] is None for row in legacy["residents"])
