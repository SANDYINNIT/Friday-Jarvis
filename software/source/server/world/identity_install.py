"""Explicit installation into an already owned simulation; no startup wiring.

lab_state holds its world lock through this transaction and publication. Catalog
discovery is fenced until the world/provenance commit finishes. Rollback refuses
later activity rather than replacing memories or history with an old snapshot.
"""
from collections import deque
from copy import deepcopy
from dataclasses import asdict
import json
import time

from . import sim
from .identity_reader import IdentityReader, _digest
from .identity_backup import create_backup


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS identity_installations ("
    "id TEXT PRIMARY KEY, state TEXT NOT NULL, proposal TEXT NOT NULL, before_world TEXT NOT NULL, "
    "before_links TEXT NOT NULL, after_world_hash TEXT NOT NULL, after_history_hash TEXT NOT NULL, "
    "created_at REAL NOT NULL, rolled_back_at REAL)",
    "CREATE TABLE IF NOT EXISTS identity_resident_links ("
    "namespace TEXT NOT NULL, resident_id TEXT NOT NULL, character_id TEXT NOT NULL UNIQUE, "
    "record TEXT NOT NULL, batch_id TEXT NOT NULL, PRIMARY KEY(namespace,resident_id))",
)
_BACKUP_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS identity_installation_backups ("
    "batch_id TEXT PRIMARY KEY REFERENCES identity_installations(id), backup_id TEXT NOT NULL UNIQUE)"
)


class BackupFailed(ValueError):
    pass


def _has_table(store, table):
    return store._conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()


def _encode(world):
    data = asdict(world)
    data["walkable"] = sorted(world.walkable)
    data["events"] = list(world.events)
    return json.dumps(data, sort_keys=True)


def _decode(record):
    data = json.loads(record)
    data["characters"] = [sim.Character(**{**row, "path": [tuple(p) for p in row["path"]]})
                          for row in data["characters"]]
    data["locations"] = [sim.Location(**{**row, "rect": tuple(row["rect"]) if row.get("rect") else None})
                         for row in data["locations"]]
    data["walkable"] = {tuple(cell) for cell in data["walkable"]}
    data["events"] = deque(data["events"], maxlen=500)
    return sim.World(**data)


def _state_hash(world):
    data = json.loads(_encode(world))
    data["events"] = [{k: v for k, v in row.items() if k != "id"} for row in data["events"]]
    return _digest(data)


def _history_hash(store):
    # These tables are preserved by installation and rollback. Any later edit,
    # task assignment, journal occurrence or checkpoint blocks restoration.
    return _digest({table: [list(row) for row in store._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("events", "agents", "roster_tasks", "edges", "checkpoints")})


def _links_rows(store):
    return [tuple(row) for row in store._conn.execute(
        "SELECT namespace,resident_id,character_id,record,batch_id FROM identity_resident_links ORDER BY namespace,resident_id")]


def _put_link(store, link, batch_id):
    record = json.dumps(link, sort_keys=True)
    store._exec_tx("INSERT INTO identity_resident_links VALUES (?,?,?,?,?)",
                   (link["namespace"], link["resident_id"], link["character_id"], record, batch_id))


def operator_blocks(store, world, view):
    """Additional requirements for the backup-backed local operator path."""
    proposal = view["installation_proposal"]
    blocks = []
    if not world.paused:
        blocks.append("Pause the simulation before installing.")
    if world.pending_events or _state_hash(store.load()) != _state_hash(world):
        blocks.append("Save the paused simulation before installing.")
    if store.identity_links() != world.identity_links:
        blocks.append("Loaded links differ from saved links. Review or reload the simulation.")
    if view["roster"]["mapping_issues"] or any(
            issue["kind"] == "alias" for issue in view["roster"]["unresolved"]):
        blocks.append("Resolve catalog aliases or conflicting ownership evidence first.")
    if view["town"]["source_issues"]:
        blocks.append("Existing residents occupy invalid or shared cells.")
    if proposal["unplaced_character_ids"]:
        blocks.append("There are not enough reachable cells for every proposed resident.")
    resident_ids = {resident.id for resident in world.characters}
    if any(row["character_id"] in resident_ids for row in proposal["new_residents"]):
        blocks.append("A proposed character UUID already belongs to a resident without a verified link.")
    if world.model_budget != 0:
        blocks.append("The simulation must use its zero-model-budget NPC setting.")
    if not proposal["new_residents"]:
        blocks.append("There are no new residents to install.")
    if _has_table(store, "identity_installations") and store._conn.execute(
            "SELECT 1 FROM identity_installations WHERE id=?", (_digest(proposal),)).fetchone():
        blocks.append("This exact proposal already has a receipt and cannot be applied again.")
    return blocks


def operator_confirmation(store, proposal):
    return _digest([proposal, _history_hash(store)])


def apply(catalog, store, world, reader, proposal, *, links=(), confirmation=None):
    """Return (committed candidate, receipt); caller publishes under its owner lock."""
    if not reader.enabled or not reader.shadow.enabled:
        raise ValueError("identity reader gate disabled")
    if reader.shadow._catalog != catalog.path:
        raise ValueError("reader and installation catalog differ")
    if store.in_transaction():
        raise ValueError("installation requires its own transaction")
    batch_id = _digest(proposal)
    with catalog.hold_revision(proposal["expected_catalog_revision"]), store.transaction():
        prior = (store._conn.execute("SELECT state FROM identity_installations WHERE id=?", (batch_id,)).fetchone()
                 if _has_table(store, "identity_installations") else None)
        if prior:
            if confirmation is not None:
                raise ValueError("installation proposal already has a receipt")
            return world, {"ok": prior[0] == "applied", "batch_id": batch_id,
                           "status": "duplicate" if prior[0] == "applied" else "already_rolled_back", "events": []}
        if store.identity_links() != world.identity_links:
            raise ValueError("stale installed link state")
        owned_reader = IdentityReader(reader.shadow, lambda: deepcopy(world), enabled=True, namespace=reader.namespace)
        fresh = owned_reader.snapshot(links=links)
        if not fresh.get("available") or fresh["installation_proposal"] != proposal:
            raise ValueError("stale source, mappings or placement proposal")
        if fresh["roster"]["mapping_issues"] or fresh["town"]["source_issues"]:
            raise ValueError("unresolved mapping evidence or invalid source position")
        if proposal["unplaced_character_ids"]:
            raise ValueError("insufficient reachable map capacity")
        if world.model_budget != 0:
            raise ValueError("installation requires NPC world with zero model budget")
        backup_id = None
        if confirmation is not None:
            if operator_blocks(store, world, fresh) or confirmation != operator_confirmation(store, proposal):
                raise ValueError("installation prerequisites changed")
            try:
                backup_id = create_backup(catalog, store, proposal, batch_id)
            except Exception:
                raise BackupFailed("Pre-install backup could not be verified.") from None
        for statement in _SCHEMA:
            store._exec_tx(statement)
        before_world, before_links = _encode(world), _links_rows(store)
        candidate = deepcopy(world)
        known = {char.id for char in candidate.characters}
        by_id = {row["id"]: row for row in fresh["roster"]["characters"]}
        existing_links = set(json.dumps(link, sort_keys=True) for link in world.identity_links)
        for link in proposal["existing_links"]:
            if json.dumps(link, sort_keys=True) not in existing_links:
                _put_link(store, link, batch_id)
        for entry in proposal["new_residents"]:
            cid = entry["character_id"]
            if cid in known:
                raise ValueError("source ID collision without verified link")
            x, y = entry["cell"]
            room = next((loc.id for loc in world.locations if loc.rect and
                         loc.rect[0] <= x <= loc.rect[2] and loc.rect[1] <= y <= loc.rect[3]), "path")
            candidate.characters.append(sim.Character(id=cid, name=by_id[cid]["name"], x=x, y=y, room=room))
            known.add(cid)
            _put_link(store, {"namespace": reader.namespace, "resident_id": cid, "character_id": cid,
                              "decision_id": "install-" + batch_id[:16] + "-" + cid, "verified": True}, batch_id)
        candidate.identity_links = store.identity_links()
        pending = tuple(candidate.pending_events)
        events = store._save_world_tx(candidate, pending)
        candidate.pending_events.clear()
        store._exec_tx("INSERT INTO identity_installations VALUES (?,?,?,?,?,?,?,?,NULL)",
                       (batch_id, "applied", json.dumps(proposal, sort_keys=True), before_world,
                        json.dumps(before_links), _state_hash(candidate), _history_hash(store), time.time()))
        if backup_id:
            store._exec_tx(_BACKUP_SCHEMA)
            store._exec_tx("INSERT INTO identity_installation_backups VALUES (?,?)", (batch_id, backup_id))
    receipt = {"ok": True, "status": "installed", "batch_id": batch_id,
               "created": len(proposal["new_residents"]), "events": events}
    if backup_id:
        receipt["backup_id"] = backup_id
    return candidate, receipt


def _rollback_block(store, world, row):
    latest = store._conn.execute(
        "SELECT id FROM identity_installations WHERE state='applied' ORDER BY rowid DESC LIMIT 1").fetchone()
    if latest is None or latest[0] != row["id"]:
        return "newer installation exists; restore the most recent installation first"
    disk = store.load()
    if (_state_hash(world) != row["after_world_hash"] or _state_hash(disk) != row["after_world_hash"]
            or _history_hash(store) != row["after_history_hash"]):
        return "post-install activity or history exists; restoration refused, all residents and history retained"
    return None


def operator_receipt(store, world):
    if not _has_table(store, "identity_installation_backups"):
        return None
    row = store._conn.execute(
        "SELECT i.*, b.backup_id FROM identity_installations i "
        "JOIN identity_installation_backups b ON i.id=b.batch_id "
        "ORDER BY (i.state='applied') DESC, i.rowid DESC LIMIT 1").fetchone()
    if row is None:
        return None
    reason = (_rollback_block(store, world, row) if row["state"] == "applied"
              else "This batch has already been rolled back.")
    return {"batch_id": row["id"], "backup_id": row["backup_id"], "state": row["state"],
            "rollback_allowed": reason is None, "rollback_block": reason}


def rollback(store, world, batch_id, *, operator=False):
    if store.in_transaction():
        raise ValueError("rollback requires its own transaction")
    with store.transaction():
        if operator and (not world.paused or not _has_table(store, "identity_installation_backups")
                         or not store._conn.execute(
                             "SELECT 1 FROM identity_installation_backups WHERE batch_id=?", (batch_id,)).fetchone()):
            raise ValueError("rollback requires a paused world and an operator batch")
        row = store._conn.execute("SELECT * FROM identity_installations WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("installation not found")
        if row["state"] == "rolled_back":
            return world, {"ok": True, "status": "already_rolled_back", "events": []}
        if reason := _rollback_block(store, world, row):
            raise ValueError(reason)
        candidate = _decode(row["before_world"])
        store._exec_tx("DELETE FROM identity_resident_links")
        for link_row in json.loads(row["before_links"]):
            store._exec_tx("INSERT INTO identity_resident_links VALUES (?,?,?,?,?)", link_row)
        events = store._save_world_tx(candidate, tuple(candidate.pending_events))
        candidate.pending_events.clear()
        store._exec_tx("UPDATE identity_installations SET state='rolled_back',rolled_back_at=? WHERE id=?",
                       (time.time(), batch_id))
    return candidate, {"ok": True, "status": "rolled_back", "batch_id": batch_id, "events": events}
