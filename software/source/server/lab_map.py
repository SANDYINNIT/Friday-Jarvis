"""Turn FRIDAY's live simulation layout into a renderable pixel-art map.

WHY THIS MODULE EXISTS
---------------------
The HUD used to draw a *schematic room-card grid* (`computeLayout` in app.js)
and simply ignored character `x/y` entirely - so the "world" view never showed
movement, occupancy or furniture. The lab needs a real tile map, but it must be
generated from the SAME layout the simulation actually walks, otherwise the
picture and the engine would disagree about where anyone is.

Design rules:

* **One coordinate space.** Everything comes from `world.sim`'s `WALKABLE`,
  `LOCATIONS` (which now carries an exact `rect` per room) and `WALKABLE`
  membership. `lab_map` never invents cells, rooms or positions.
* **Tile choices come from `ui/art/tiles.json` at runtime** - never a
  hard-coded index - so re-running `tools/gen_art.py` cannot silently
  mis-map a room.
* **The renderer never overrides pathfinding.** `solid` on an object is
  display metadata for the inspector; movement decisions stay with the
  simulation's own A* over `WALKABLE`. Furniture is placed *where the sim
  already puts its anchors* (desks, beds, parked cars), so a character
  standing at its desk is correct, not a collision bug.
* **Deterministic and bounded**: same world -> byte-identical JSON; grid size
  capped; missing/invalid art manifest degrades to an honest error payload
  instead of raising.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

_MAX_CELLS = 65536          # 256x256 - a companion town, not a continent
_PAD = 1                    # one-cell margin so facades/trees are visible

_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
_MANIFEST_PATH = os.path.join(_UI_DIR, "art", "tiles.json")

_lock = threading.RLock()
_manifest_cache: Optional[Dict[str, Any]] = None
_manifest_error: str = ""

_FLOOR_BY_KIND = {
    "office": "wood_floor", "collaboration": "tile_floor",
    "library": "wood_floor", "kitchen": "tile_floor", "bedroom": "carpet",
    "hall": "stone_floor", "park": "grass", "street": "grass",
}
_ROOM_TAG = {
    "office": "office", "collaboration": "collaboration",
    "library": "library", "kitchen": "kitchen", "bedroom": "bedroom",
    "hall": "building", "park": "park", "street": "street",
    "desk": "office", "bed": "bedroom", "car": "street", "path": "outdoor",
    "door": "building",
}
_POINT_FURNITURE = {"desk": "desk", "bed": "bed", "car": "car_blue"}


def load_manifest(path: Optional[str] = None) -> Dict[str, Any]:
    """Read + validate `tiles.json`, cached. Returns an error payload on failure."""
    global _manifest_cache, _manifest_error
    path = path or _MANIFEST_PATH
    with _lock:
        if _manifest_cache is not None:
            return _manifest_cache
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            tiles = data.get("tiles")
            if not isinstance(tiles, list) or not tiles:
                raise ValueError("manifest has no tiles")
            index_by_name = {}
            for tile in tiles:
                if not isinstance(tile, dict):
                    raise ValueError("tile entry is not an object")
                index = tile.get("index")
                name = tile.get("name")
                if not isinstance(index, int) or not isinstance(name, str):
                    raise ValueError("tile entry missing index/name")
                index_by_name[name] = index
            data["_index_by_name"] = index_by_name
            data["_columns"] = int(data.get("columns") or 8)
            data["_tile_size"] = int(data.get("tile_size") or 16)
            _manifest_cache = data
            _manifest_error = ""
        except Exception as error:
            _manifest_error = "%s: %s" % (type(error).__name__, error)
            _manifest_cache = {"tiles": [], "_index_by_name": {},
                               "_columns": 8, "_tile_size": 16,
                               "_error": _manifest_error}
        return _manifest_cache


def _idx(manifest: Dict[str, Any], name: str, fallback: int = 0) -> int:
    """Resolve a tile NAME to its index, else a safe fallback (never -1)."""
    value = manifest.get("_index_by_name", {}).get(name)
    if isinstance(value, int):
        return value
    return int(fallback)


def _floor_for(kind: str, tag_index: Dict[str, int]) -> int:
    return tag_index.get(_FLOOR_BY_KIND.get(kind, "grass"),
                         tag_index.get("grass", 0))


def _in_rect(rect, x: int, y: int) -> bool:
    if not rect:
        return False
    try:
        x0, y0, x1, y1 = rect
    except Exception:
        return False
    return int(x0) <= x <= int(x1) and int(y0) <= y <= int(y1)


def _room_for(locations, x: int, y: int):
    for loc in locations:
        if _in_rect(getattr(loc, "rect", None), x, y):
            return loc
    return None


def snapshot(world: Any = None, *, live=None) -> Dict[str, Any]:
    """Build the renderable map from the REAL sim layout."""
    try:
        from .world import sim as _sim
        if live is None and world is None:
            world = _sim.new_world()
        walkable = set(world.walkable if world is not None else _sim.WALKABLE)
        locations = list(world.locations if world is not None else _sim.LOCATIONS)
        snapshot_data = live if live is not None else _sim.snapshot(world)
        if not isinstance(snapshot_data, dict):
            snapshot_data = {}
    except Exception as error:
        return {"ok": False, "error": "%s: %s" % (type(error).__name__, error)}

    manifest = load_manifest()
    if manifest.get("_error") or not manifest.get("tiles"):
        return {"ok": False, "error": manifest.get("_error") or "no tiles",
                "manifest": _MANIFEST_PATH}

    by_name = manifest["_index_by_name"]
    columns = manifest["_columns"]

    if not walkable:
        return {"ok": False, "error": "simulation layout has no walkable cells"}

    xs = [c[0] for c in walkable]
    ys = [c[1] for c in walkable]
    x_lo, x_hi = min(xs) - _PAD, max(xs) + _PAD
    y_lo, y_hi = min(ys) - _PAD, max(ys) + _PAD
    width, height = x_hi - x_lo + 1, y_hi - y_lo + 1
    if width * height > _MAX_CELLS:
        return {"ok": False, "error": "layout too large: %d cells"
                % (width * height), "width": width, "height": height}

    def pos(x: int, y: int) -> int:
        return (y - y_lo) * width + (x - x_lo)

    def valid(x: int, y: int) -> bool:
        return x_lo <= x <= x_hi and y_lo <= y <= y_hi

    grass = _idx(manifest, "grass")
    # name -> the full tile record, so `solid`/`layer`/`tag` come from art too
    tile_meta = {t.get("name"): t for t in manifest.get("tiles", [])
                 if isinstance(t, dict)}
    ground = [grass] * (width * height)
    object_ = [-1] * (width * height)
    overhead = [-1] * (width * height)

    park = next((l for l in locations if l.kind == "park"), None)
    street = next((l for l in locations if l.kind == "street"), None)

    # ---- ground ------------------------------------------------------------
    for x in range(x_lo, x_hi + 1):
        for y in range(y_lo, y_hi + 1):
            p = pos(x, y)
            if not valid(x, y):
                continue
            room = _room_for(locations, x, y)
            if room is not None and room.rect is not None:
                ground[p] = _floor_for(room.kind, by_name)
            elif (x, y) in walkable:
                if _in_rect(getattr(street, "rect", None), x, y):
                    ground[p] = _idx(manifest, "road", grass)
                elif y > y_hi - _PAD - 4:
                    # exterior but not street/park -> path or lawn
                    if x == 10 or y == 14:
                        ground[p] = _idx(manifest, "dirt_path", grass)
                    else:
                        ground[p] = grass
                else:
                    ground[p] = _idx(manifest, "wood_floor", grass)
            else:
                # outdoor ground everywhere else (water handled below)
                ground[p] = grass

    # water band along the bottom-right of the park, as outdoor ambience
    for x in range(x_lo, x_hi + 1):
        for y in range(y_hi - 2, y_hi + 1):
            if valid(x, y) and (x, y) not in walkable and x >= 12:
                ground[pos(x, y)] = _idx(manifest, "water", grass)

    # ---- point anchors: desks, beds, parked cars ---------------------------
    objects: List[Dict[str, Any]] = []

    def place(x: int, y: int, name: str, *, solid: Optional[bool] = None,
              obj_id: str = "", purpose: str = "", char_id: Any = None) -> bool:
        if not valid(x, y):
            return False
        p = pos(x, y)
        tile = _idx(manifest, name, -1)
        if tile < 0:
            return False
        object_[p] = tile
        meta = tile_meta.get(name) or {}
        entry = {
            "id": obj_id or ("%s_%d_%d" % (name, x, y)),
            "tile": tile, "x": x, "y": y, "name": name,
            "purpose": purpose or _PURPOSE.get(name, ""),
            "solid": bool(meta.get("solid", True)) if solid is None else bool(solid),
            "char_id": char_id,
        }
        objects.append(entry)
        return True

    # character positions first, so objects can carry their occupant
    chars = snapshot_data.get("characters") or []
    occupancy: Dict[Tuple[int, int], str] = {}
    for character in chars:
        try:
            occupancy[(int(character["x"]), int(character["y"]))] = str(
                character.get("id") or "")
        except Exception:
            continue

    for loc in locations:
        furniture = _POINT_FURNITURE.get(loc.kind)
        if furniture:
            place(int(loc.x), int(loc.y), furniture, obj_id=loc.id,
                  solid=loc.kind == "car",
                  purpose=_PURPOSE.get(furniture, loc.name),
                  char_id=occupancy.get((int(loc.x), int(loc.y))))

    for room_id, fixtures in _sim.ROOM_FIXTURES.items():
        for x, y, name in fixtures:
            place(x, y, name, obj_id=f"{room_id}_{name}", solid=True)
    for entry in snapshot_data.get("locations", []):
        if entry.get("kind") == "kitchen":
            for x, y in entry.get("interaction_cells", []):
                place(x, y, "cafe_chair", obj_id=f"kitchen-seat-{x}-{y}",
                      purpose="eat here", solid=False, char_id=occupancy.get((x, y)))

    # ---- facade + roof + greenery on NON-walkable cells --------------------
    walk_adjacent = set()
    for (x, y) in walkable:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if valid(nx, ny) and (nx, ny) not in walkable:
                walk_adjacent.add((nx, ny))

    wall_tile = _idx(manifest, "wall_brick", -1)
    roof_tile = _idx(manifest, "roof_blue", -1)
    tree_trunk = _idx(manifest, "tree_trunk", -1)
    tree_top = _idx(manifest, "tree_canopy", -1)
    bush_tile = _idx(manifest, "bush", -1)

    for x in range(x_lo, x_hi + 1):
        for y in range(y_lo, y_hi + 1):
            if not valid(x, y) or (x, y) in walkable:
                continue
            p = pos(x, y)
            inside_building_band = y <= y_hi - _PAD - 6
            if (x, y) in walk_adjacent and inside_building_band and wall_tile >= 0:
                if object_[p] < 0:
                    object_[p] = wall_tile
                    if roof_tile >= 0:
                        overhead[p] = roof_tile
                continue
            if (x, y) in walk_adjacent and wall_tile >= 0 and y <= y_hi - _PAD:
                # low garden wall / fence around the grounds
                fence = _idx(manifest, "fence", -1)
                if fence >= 0 and object_[p] < 0:
                    object_[p] = fence
                continue
            # scattered greenery, deterministic by coordinate
            if object_[p] < 0 and ground[p] == grass:
                if (x * 7 + y * 13) % 17 == 0 and tree_trunk >= 0:
                    object_[p] = tree_trunk
                    if tree_top >= 0:
                        overhead[p] = tree_top
                elif (x * 5 + y * 11) % 23 == 0 and bush_tile >= 0:
                    object_[p] = bush_tile

    placed = {(o["x"], o["y"]) for o in objects}
    # ---- cars on the street (visual anchors) ------------------------------
    for loc in locations:
        if loc.kind == "car" and (int(loc.x), int(loc.y)) not in placed:
            colour = ["car_blue", "car_red", "car_yellow", "car_grey"][
                int(loc.id.split("-")[-1]) % 4] if "-" in loc.id else "car_grey"
            place(int(loc.x), int(loc.y), colour, obj_id=loc.id,
                  purpose="parked vehicle",
                  char_id=occupancy.get((int(loc.x), int(loc.y))))

    # ---- rooms ------------------------------------------------------------
    rooms = []
    for loc in locations:
        rect = getattr(loc, "rect", None)
        if rect:
            x0, y0, x1, y1 = (int(v) for v in rect)
            w, h = x1 - x0 + 1, y1 - y0 + 1
        else:
            x0 = y0 = int(loc.x)
            w = h = 1
        rooms.append({
            "id": loc.id, "name": loc.name, "kind": loc.kind,
            "tag": _ROOM_TAG.get(loc.kind, "outdoor"),
            "x": x0, "y": y0, "w": w, "h": h,
            "capacity": int(getattr(loc, "capacity", 1) or 1),
            "occupied": snapshot_data.get("_occupancy", {}).get(loc.id)
            if isinstance(snapshot_data.get("_occupancy"), dict) else None,
        })

    # occupancy straight from the snapshot's locations (ids as strings)
    for entry in (snapshot_data.get("locations") or []):
        try:
            lid = entry.get("id")
            occupied = entry.get("occupied")
            for room in rooms:
                if room["id"] == lid:
                    room["occupied"] = occupied if isinstance(
                        occupied, str) else None
        except Exception:
            continue

    spawn = sorted("%d,%d" % (x, y) for (x, y) in walkable)[:64]

    return {
        "ok": True,
        "tile_size": manifest["_tile_size"],
        "columns": columns,
        "width": width, "height": height,
        "origin": {"x": x_lo, "y": y_lo},
        "layers": {"ground": ground, "object": object_, "overhead": overhead},
        "rooms": rooms,
        "objects": objects,
        "spawn": spawn,
        "art": {"tiles": "art/tiles.png",
                "characters": "art/characters.png",
                "manifest": "art/tiles.json"},
        "characters": [{key: c.get(key) for key in (
                            "id", "x", "y", "room", "state", "goal", "task_ref",
                            "name", "needs", "bar", "blocked_reason")}
                       for c in chars if isinstance(c, dict)],
        "counts": {"cells": width * height, "walkable": len(walkable),
                   "rooms": len(rooms), "objects": len(objects)},
    }


_PURPOSE = {
    "desk": "work at", "computer": "work at", "chair": "sit at",
    "big_table": "meet at", "chair2": "sit at", "whiteboard": "plan on",
    "sofa": "rest on", "bookshelf": "read from", "bookshelf_row": "read from",
    "reading_table": "read at", "archive_box": "store records",
    "counter": "prepare food", "stove": "cook on", "fridge": "store food",
    "sink": "wash up", "kitchen_table": "eat at", "cafe_chair": "sit at",
    "cup": "drink from", "bed": "sleep on", "pillow": "sleep on",
    "dresser": "store clothes", "lamp": "light", "plant": "decor",
    "rug_small": "decor", "cabinet": "store", "bench": "sit on",
    "bench_park": "sit on", "fountain": "decor", "sign": "read",
    "server_rack": "diagnostics", "console_panel": "diagnostics",
    "crate": "store", "chalkboard": "learn from", "student_desk": "learn at",
    "teacher_desk": "teach from", "car_blue": "parked vehicle",
    "car_red": "parked vehicle", "car_yellow": "parked vehicle",
    "car_grey": "parked vehicle", "tree_trunk": "shade", "bush": "decor",
    "fence": "boundary", "wall_brick": "structure", "roof_blue": "roof",
    "roof_red": "roof",
}
