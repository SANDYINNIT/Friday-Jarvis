"""Gated, read-only Roster/Town projection and proposed resident placement.

This reader does not install characters, tick a world, resolve legacy ownership,
or write stores. Its source callback must capture a detached World under the
world owner's lock. Catalog identity remains owned by IdentityCatalog.
"""
from collections import Counter, deque
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from uuid import UUID

from . import sim
from .identity_catalog import _public_token


@dataclass(frozen=True)
class ResidentLink:
    namespace: str
    resident_id: str
    character_id: str
    decision_id: str
    verified: bool = False

    def __post_init__(self):
        for value in (self.namespace, self.resident_id, self.decision_id):
            _public_token(value)
        if str(UUID(self.character_id)) != self.character_id:
            raise ValueError("expected exact catalog character UUID")
        if type(self.verified) is not bool:
            raise ValueError("verification must be explicit")


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reachable(world):
    front = next(((loc.x, loc.y) for loc in world.locations if loc.id == "front"), None)
    anchor = front if front in world.walkable else min(world.walkable, default=None)
    found = set() if anchor is None else {anchor}
    todo = deque(found)
    while todo:
        x, y = todo.popleft()
        for cell in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if cell in world.walkable and cell not in found:
                found.add(cell)
                todo.append(cell)
    return anchor, found


def _resolve_links(links, namespace, characters, residents):
    unique = set(links)
    source_counts = Counter(link.resident_id for link in unique)
    target_counts = Counter(link.character_id for link in unique)
    decision_counts = Counter(link.decision_id for link in unique)
    accepted, unresolved = {}, []
    for link in sorted(unique, key=lambda item: (item.resident_id, item.character_id, item.decision_id)):
        reason = ""
        if (source_counts[link.resident_id] > 1 or target_counts[link.character_id] > 1
                or decision_counts[link.decision_id] > 1):
            reason = "conflicting mapping evidence"
        elif not link.verified:
            reason = "mapping is unverified"
        elif link.namespace != namespace:
            reason = "different source namespace"
        elif link.character_id not in characters:
            reason = "character absent from selected catalog revision"
        elif link.resident_id not in residents:
            reason = "source resident absent"
        if reason:
            unresolved.append({**asdict(link), "reason": reason})
        else:
            accepted[link.character_id] = link
    return accepted, unresolved


class IdentityReader:
    def __init__(self, shadow=None, capture_world=None, *, enabled=False, namespace="town"):
        if type(enabled) is not bool:
            raise ValueError("reader gate must be boolean")
        self.enabled = enabled
        if not enabled:
            return
        if shadow is None or not callable(capture_world):
            raise ValueError("reader requires explicit shadow and detached world source")
        self.shadow = shadow
        self.capture_world = capture_world
        self.namespace = _public_token(namespace)

    def snapshot(self, selected_character_id=None, *, links=(), catalog_revision=None, enrolled=None, life=None):
        if not self.enabled:
            return {"enabled": False, "reason": "identity reader gate disabled"}
        # The shadow reads the catalog once. No later catalog reads can mix
        # revisions into this result, including an explicitly pinned rollback.
        pair = self.shadow.snapshot(selected_character_id, catalog_revision=catalog_revision)
        if not pair.get("enabled"):
            return pair
        world = self.capture_world()
        if world is None:
            return {"enabled": True, "available": False, "reason": "source world not loaded"}
        if not isinstance(world, sim.World):
            raise ValueError("source must supply a detached World")
        world = deepcopy(world)
        residents = {char.id: asdict(char) for char in world.characters}
        if len(residents) != len(world.characters):
            raise ValueError("source contains duplicate resident IDs")
        source = sim.snapshot(world)
        source["characters"] = list(residents.values())
        source["reservations"] = dict(world.reservations)
        source["walkable"] = sorted(world.walkable)
        source["steps_per_tick"] = world.steps_per_tick
        source["model_budget"] = world.model_budget
        source["identity_links"] = deepcopy(world.identity_links)
        source["history_state"] = _digest([
            [{k: v for k, v in event.items() if k != "id"} for event in world.events],
            world.pending_events,
        ])
        source["fixtures"] = [
            {"x": x, "y": y, "kind": kind}
            for room, items in sim.ROOM_FIXTURES.items()
            if any(loc.id == room for loc in world.locations)
            for x, y, kind in items if (x, y) not in world.walkable
        ]
        # Keep original journal row references in the view. Occurrence identity
        # alone participates in the world token, including before/after save.
        event_occurrences = [
            {key: value for key, value in event.items() if key != "id"}
            for event in source["last_events"]
        ]
        world_id = _digest({**source, "last_events": event_occurrences})
        common = {"catalog_revision": pair["catalog_revision"],
                  "selected_character_id": pair["selected_character_id"],
                  "selection_status": ("selected" if pair["selected_character_id"] else
                                       "unavailable" if selected_character_id else "none"),
                  "world_snapshot_id": world_id, "world_tick": world.tick}
        characters = {row["id"]: row for row in pair["roster"]["characters"]}
        persisted = [ResidentLink(**link) for link in world.identity_links]
        accepted, issues = _resolve_links([*persisted, *links], self.namespace, characters, residents)
        counts = Counter((char.x, char.y) for char in world.characters)
        occupied = set(counts)
        anchor, reachable = _reachable(world)
        # Spawn only on plain walkable floor, leaving destination furniture and
        # routine interaction positions free for normal simulation use.
        targets = {(loc.x, loc.y) for loc in world.locations}
        targets.update(cell for loc in world.locations for cell in sim.interaction_cells(world, loc))
        free = reachable - occupied - targets
        roster, staged = [], []
        for cid, row in sorted(characters.items()):
            entry = deepcopy(row)
            for binding in entry["bindings"]:
                binding["serving_editable"] = bool(
                    binding["serving_editable"] and enrolled is not None and enrolled(binding))
            link = accepted.get(cid)
            state = deepcopy(residents[link.resident_id]) if link else None
            placement = {"status": "archived", "cell": None, "simulation": "none"}
            if state is not None:
                cell = (state["x"], state["y"])
                usable = cell in reachable and counts[cell] == 1
                placement = {"status": "existing" if usable else "invalid_source_position",
                             "cell": list(cell), "simulation": "existing_source_resident",
                             "source_resident_id": link.resident_id,
                             "decision_id": link.decision_id,
                             "reason": "" if usable else "source cell blocked, disconnected or shared"}
            elif row["status"] == "configured":
                cell = min(free, key=lambda p: (_digest([cid, p]), p)) if free else None
                placement = {"status": "proposed" if cell else "unplaced",
                             "cell": list(cell) if cell else None, "simulation": "not_installed",
                             "reason": "" if cell else "insufficient reachable map capacity"}
                if cell:
                    free.remove(cell)
            entry.update(placement=placement, source_state=state,
                         background_life={"mode": "ai" if row.get("life_binding_id") else "npc",
                                          "state": "unavailable" if row.get("life_binding_id") else "npc",
                                          "active_mode": "npc" if state is not None else None,
                                          "ai_available": False})
            if life is not None:
                entry["background_life"] = life(entry, world.paused)
            roster.append(entry)
            if row["status"] == "configured":
                staged.append(deepcopy(entry))
        roster.sort(key=lambda row: (row["name"], row["id"]))
        staged.sort(key=lambda row: (row["name"], row["id"]))
        source_links = {link.resident_id: cid for cid, link in accepted.items()}
        legacy = [{"namespace": self.namespace, "source_resident_id": rid,
                   "character_id": source_links.get(rid),
                   "resolution": "verified" if rid in source_links else "unresolved",
                   "source_state": deepcopy(state)} for rid, state in sorted(residents.items())]
        source_issues = [{"cell": list(cell), "reason": "overlapping source residents"}
                         for cell, count in sorted(counts.items()) if count > 1]
        source_issues.extend({"cell": list(cell), "reason": "source cell blocked or disconnected"}
                             for cell in sorted(occupied - reachable))
        result = {**pair, **common, "available": True, "source_world": source,
                  "roster": {**pair["roster"], **common, "characters": roster,
                             "legacy_residents": legacy, "mapping_issues": issues},
                  "town": {**pair["town"], **common, "residents": staged,
                           "legacy_residents": legacy, "source_issues": source_issues,
                           "anchor": list(anchor) if anchor else None,
                           "capacity": {"unplaced": sum(c["placement"]["status"] == "unplaced" for c in staged),
                                        "remaining_cells": len(free)},
                           "placement_only": any(c["placement"]["simulation"] == "not_installed" for c in staged)}}
        result["installation_proposal"] = {
            "status": "requires_separate_simulation_integration",
            "expected_catalog_revision": common["catalog_revision"],
            "expected_world_snapshot_id": world_id,
            "existing_links": [asdict(link) for _, link in sorted(accepted.items())],
            "new_residents": [{"character_id": row["id"], "cell": row["placement"]["cell"]}
                              for row in staged if row["placement"]["status"] == "proposed"],
            "unplaced_character_ids": [row["id"] for row in staged if row["placement"]["status"] == "unplaced"],
        }
        result["read_id"] = _digest(result)
        for view in (result["roster"], result["town"]):
            view["read_id"] = result["read_id"]
        return result


def from_environment(shadow=None, capture_world=None, *, env=None):
    env = os.environ if env is None else env
    return IdentityReader(shadow, capture_world, enabled=env.get("FRIDAY_IDENTITY_READER") == "1")
