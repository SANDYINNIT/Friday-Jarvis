"""Tests for the pixel-art map builder (must reflect the REAL sim layout)."""

import json

import pytest

from source.server import lab_map
from source.server.world import sim as world_sim


@pytest.fixture()
def payload():
    return lab_map.snapshot(world_sim.new_world())


def test_snapshot_is_ok_and_json_safe(payload):
    assert payload["ok"] is True, payload
    json.dumps(payload)                      # must not raise


def test_layers_are_exactly_width_times_height(payload):
    cells = payload["width"] * payload["height"]
    assert cells <= lab_map._MAX_CELLS
    for name in ("ground", "object", "overhead"):
        assert len(payload["layers"][name]) == cells, name


def test_every_tile_index_is_valid_for_the_manifest(payload):
    manifest = lab_map.load_manifest()
    valid = {t["index"] for t in manifest["tiles"]}
    for name, layer in payload["layers"].items():
        for value in layer:
            assert value in valid or value == -1, (name, value)


def test_building_is_a_square_not_a_line(payload):
    # guards against a mis-scaled grid that would render a 1px sliver
    assert payload["width"] >= 20 and payload["height"] >= 15


def test_snapshot_is_deterministic():
    first = lab_map.snapshot(world_sim.new_world())
    second = lab_map.snapshot(world_sim.new_world())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_character_coordinates_land_on_real_ground(payload):
    """A character must never be drawn on water or outside the grid."""
    manifest = lab_map.load_manifest()
    by_name = {t["name"]: t["index"] for t in manifest["tiles"]}
    water = by_name.get("water")
    origin = payload["origin"]
    ground = payload["layers"]["ground"]
    assert payload["characters"], "the world must have characters"
    for character in payload["characters"]:
        x = int(character["x"]) - origin["x"]
        y = int(character["y"]) - origin["y"]
        assert 0 <= x < payload["width"], character
        assert 0 <= y < payload["height"], character
        tile = ground[y * payload["width"] + x]
        if water is not None:
            assert tile != water, character


def test_expected_room_kinds_are_present(payload):
    kinds = {room["kind"] for room in payload["rooms"]}
    # what build_layout() actually provides today
    for wanted in ("office", "collaboration", "library", "kitchen",
                   "bedroom", "park", "street"):
        assert wanted in kinds, "missing room kind: %s" % wanted


def test_rooms_carry_their_rect_and_capacity(payload):
    room = next(r for r in payload["rooms"] if r["kind"] == "bedroom")
    assert room["w"] >= 2 and room["h"] >= 2
    assert room["capacity"] >= 1
    assert isinstance(room["name"], str) and room["name"]


def test_occupancy_is_an_id_string_or_null_never_invented(payload):
    for room in payload["rooms"]:
        assert room["occupied"] is None or isinstance(room["occupied"], str), room


def test_objects_have_real_purposes_and_coords(payload):
    assert payload["objects"], "furniture must be placed"
    for obj in payload["objects"]:
        assert obj["purpose"], obj
        assert isinstance(obj["solid"], bool)
        assert 0 <= obj["x"] and 0 <= obj["y"]
        assert obj["name"]


def test_furniture_is_inside_the_map(payload):
    for obj in payload["objects"]:
        assert obj["x"] < payload["width"] + 1
        assert obj["y"] < payload["height"] + 1


def test_desks_and_beds_exist_for_the_anchors(payload):
    names = {o["name"] for o in payload["objects"]}
    assert "desk" in names, "office desks must be rendered"
    assert "bed" in names, "bedroom beds must be rendered"


def test_art_paths_are_relative_for_the_page(payload):
    assert payload["art"]["tiles"].startswith("art/")
    assert payload["art"]["characters"].startswith("art/")
    assert payload["art"]["manifest"].startswith("art/")


def test_missing_manifest_degrades_to_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(lab_map, "_MANIFEST_PATH",
                        str(tmp_path / "nope.json"))
    monkeypatch.setattr(lab_map, "_manifest_cache", None)
    out = lab_map.snapshot(world_sim.new_world())
    assert out["ok"] is False and out["error"]


def test_manifest_helper_reports_a_real_file():
    manifest = lab_map.load_manifest()
    assert manifest.get("_error") is None, manifest.get("_error")
    assert manifest["tiles"], "the generated art must have tiles"
    assert manifest["_index_by_name"]["grass"] >= 0
