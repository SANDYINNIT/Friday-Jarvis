"""Opt-in identity discovery and a read-only shadow projection.

The caller supplies an authoritative *configured* inventory, not a health or
quota snapshot. This module has no runtime/credential imports or default path.
Use a dedicated catalog database; legacy stores are never reconciliation inputs.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

_APPLICATION_ID = 0x46524943
_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}")
_SLUG = re.compile(r"[a-z][a-z0-9_-]{0,95}")
_MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*(?:/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?(?::[a-zA-Z0-9][a-zA-Z0-9_.-]*)?")
_SERVICE_CAPABILITIES = {"stt", "tts", "listen", "voice_out", "workflow", "n8n"}
_NAMES = ("Aster", "Reed", "Moss", "Mira", "Lark", "Fern", "Sage", "Rowan")

_SCHEMA = """
CREATE TABLE catalog_character (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE catalog_route_owner (
    route TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES catalog_character(id)
);
CREATE TABLE catalog_alias_claim (
    decision_id TEXT PRIMARY KEY,
    route TEXT NOT NULL,
    character_id TEXT NOT NULL,
    verified INTEGER NOT NULL CHECK(verified IN (0, 1))
);
CREATE INDEX catalog_alias_by_route ON catalog_alias_claim(route);
CREATE TABLE catalog_revision (
    id INTEGER PRIMARY KEY,
    parent INTEGER REFERENCES catalog_revision(id),
    projection TEXT NOT NULL
);
CREATE TABLE catalog_head (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    revision INTEGER REFERENCES catalog_revision(id)
);
INSERT INTO catalog_head VALUES (1, NULL);
"""


def _public_token(value, pattern=_TOKEN):
    if (not isinstance(value, str) or len(value) > 160
            or not pattern.fullmatch(value)):
        raise ValueError("expected a non-secret public configuration identifier")
    return value


def _flag(value):
    if type(value) is not bool:
        raise ValueError("configuration flags must be boolean")
    return value


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def route_fields(binding):
    return {field: binding[field] for field in ("provider", "deployment", "model")}


def serving_editable(view, binding):
    return (binding["status"] == "configured"
            and binding["capability"] in ("brain", "vision")
            and not any(row["kind"] == "alias" and row["route"] == route_fields(binding)
                        for row in view["unresolved"]))


@dataclass(frozen=True, order=True)
class Route:
    """Opaque deployment reference, not a hostname, URL, credential or account."""

    provider: str
    deployment: str
    model: str

    def __post_init__(self):
        _public_token(self.provider, _SLUG)
        _public_token(self.deployment, _SLUG)
        _public_token(self.model, _MODEL)

    @property
    def key(self):
        return _dump((self.provider, self.deployment, self.model))

    def public(self):
        return {"provider": self.provider, "deployment": self.deployment, "model": self.model}


@dataclass(frozen=True)
class AliasClaim:
    """A supplied decision from the caller. This module never verifies remotely."""

    route: Route
    character_id: str
    decision_id: str
    verified: bool = False

    def __post_init__(self):
        if not isinstance(self.route, Route):
            raise ValueError("alias requires a public route")
        _public_token(self.decision_id)
        _flag(self.verified)
        try:
            if str(UUID(self.character_id)) != self.character_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("alias target must be a catalog character ID") from None


def discover_configuration(configurations):
    """Reduce supplied configured routes to public route/capability records.

    Rows may contain transport metadata, but only the declared public fields
    are read. A row is configured by default, including when it has zero
    credentials. Only explicit removal/absence in a complete inventory
    retires a route. Duplicate inputs coalesce by exact route and capability.
    """
    found = {}
    for item in configurations:
        if not _flag(item.get("configured", True)):
            continue
        kind = item.get("kind")
        if kind not in ("reasoning", "service"):
            raise ValueError("route kind must be reasoning or service")
        route = Route(item.get("provider"), item.get("deployment"), item.get("model"))
        capabilities = item.get("capabilities")
        if not isinstance(capabilities, (tuple, list)) or not capabilities:
            raise ValueError("a route must declare at least one capability")
        enabled = _flag(item.get("enabled", True))
        for cap in capabilities:
            cap = _public_token(cap)
            if kind == "reasoning" and cap in _SERVICE_CAPABILITIES:
                raise ValueError("transport capabilities must use a service record")
            key = (route, cap)
            value = (kind, enabled)
            if key in found and found[key] != value:
                raise ValueError("conflicting declarations for the same route and capability")
            found[key] = value
    return [(route, cap, *found[route, cap]) for route, cap in sorted(found)]


def _empty():
    return {"characters": {}, "bindings": {}, "services": {}, "unresolved": []}


def _read(conn, revision=None):
    if revision is None:
        revision = conn.execute("SELECT revision FROM catalog_head").fetchone()[0]
    if revision is None:
        return None, _empty()
    row = conn.execute("SELECT projection FROM catalog_revision WHERE id=?", (revision,)).fetchone()
    if row is None:
        raise ValueError("catalog revision does not exist")
    return revision, json.loads(row[0])


def _check_database(conn):
    if conn.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
        raise ValueError("expected a dedicated identity catalog database")


def read_shadow_projection(path, *, revision=None, timeout=5.0):
    """Return a detached snapshot via SQLite read-only mode. Never initializes."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=timeout)) as conn:
        _check_database(conn)
        selected, view = _read(conn, revision)
        return {"revision": selected, **view}


class IdentityCatalog:
    @classmethod
    def open_existing(cls, path, revision):
        catalog = cls.__new__(cls)
        catalog.path = Path(path).resolve()
        if read_shadow_projection(catalog.path)["revision"] != revision:
            raise ValueError("catalog revision changed during startup")
        return catalog

    def __init__(self, path):
        self.path = Path(path).resolve()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("PRAGMA application_id").fetchone()[0]
            if current == 0 and not conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            _check_database(conn)

    @contextmanager
    def _write(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)) as conn, conn:
            _check_database(conn)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @contextmanager
    def hold_revision(self, revision):
        """Fence concurrent discovery while a caller commits an installation."""
        with self._write() as conn:
            current = conn.execute("SELECT revision FROM catalog_head WHERE singleton=1").fetchone()[0]
            if current != revision:
                raise ValueError("stale catalog revision")
            yield

    def activate_revision(self, revision):
        """Restore an existing projection, without deleting later history.

        Identity allocations and submitted alias claims remain reserved across
        revisions. Rollback changes the view, not ownership decisions.
        """
        with self._write() as conn:
            if revision is not None:
                _read(conn, revision)
            conn.execute("UPDATE catalog_head SET revision=?", (revision,))

    def set_serving(self, character_id, binding_id, capability, allowed, revision, enrolled):
        """Commit an exact binding preference as an ordinary catalog revision."""
        if type(allowed) is not bool or type(revision) is not int or revision < 1:
            raise ValueError("invalid serving preference or revision")
        if not isinstance(character_id, str) or not isinstance(binding_id, str):
            raise ValueError("exact character and binding required")
        with self._write() as conn:
            head, view = _read(conn)
            if head != revision:
                raise ValueError("stale catalog revision")
            binding = view["bindings"].get(binding_id)
            if (not binding or binding["character_id"] != character_id
                    or binding["capability"] != capability or not serving_editable(view, binding)
                    or not enrolled(binding)):
                raise ValueError("binding cannot be edited")
            if binding.get("can_serve", True) is allowed:
                return head
            binding["can_serve"] = allowed
            updated = conn.execute("INSERT INTO catalog_revision(parent,projection) VALUES (?,?)",
                                   (head, _dump(view))).lastrowid
            conn.execute("UPDATE catalog_head SET revision=?", (updated,))
            return updated

    def set_life(self, character_id, binding_id, mode, revision, installed, resolve):
        """Commit independent life preferences with an exact revision and route."""
        if mode not in ("npc", "ai") or type(revision) is not int or revision < 1:
            raise ValueError("invalid background life preference")
        with self._write() as conn:
            head, view = _read(conn)
            if head != revision:
                raise ValueError("stale catalog revision")
            chars = view["characters"]
            bulk = character_id is None and binding_id is None and mode == "ai"
            if not bulk and (not isinstance(character_id, str) or character_id not in chars):
                raise ValueError("exact character required")
            changed = 0
            for cid in (sorted(installed) if bulk else [character_id]):
                char = chars.get(cid)
                bindings = [row for row in view["bindings"].values() if row["character_id"] == cid]
                if not char or (mode == "ai" and (cid not in installed or char["status"] != "configured")):
                    if bulk:
                        continue
                    raise ValueError("verified installed resident required")
                selected = None
                if mode == "ai":
                    if bulk and any(row["status"] != "retired" and row["provider"] == "ollama"
                                    for row in bindings):
                        continue
                    if bulk and char.get("life_binding_id"):
                        continue
                    routes = [row for row in bindings if (bulk or row["id"] == binding_id)
                              and serving_editable(view, row) and resolve(row) is not None]
                    if len(routes) != 1:
                        if bulk:
                            continue
                        raise ValueError("exact available background binding required")
                    selected = routes[0]["id"]
                if selected != char.get("life_binding_id"):
                    char["life_binding_id"] = selected
                    changed += 1
            if not changed:
                return head, 0
            updated = conn.execute("INSERT INTO catalog_revision(parent,projection) VALUES (?,?)",
                                   (head, _dump(view))).lastrowid
            conn.execute("UPDATE catalog_head SET revision=?", (updated,))
            return updated, changed

    @staticmethod
    def _remember_alias(conn, claim):
        row = (claim.route.key, claim.character_id, int(claim.verified))
        old = conn.execute(
            "SELECT route,character_id,verified FROM catalog_alias_claim WHERE decision_id=?",
            (claim.decision_id,)).fetchone()
        if old is not None and old != row:
            raise ValueError("alias decision ID was already used for different evidence")
        conn.execute("INSERT OR IGNORE INTO catalog_alias_claim VALUES (?,?,?,?)",
                     (claim.decision_id, *row))

    @staticmethod
    def _resolve_owner(conn, route):
        owner = conn.execute("SELECT character_id FROM catalog_route_owner WHERE route=?",
                             (route.key,)).fetchone()
        claims = conn.execute(
            "SELECT character_id,verified,decision_id FROM catalog_alias_claim "
            "WHERE route=? ORDER BY decision_id", (route.key,)).fetchall()
        targets = {row[0] for row in claims}
        if claims:
            candidate = next(iter(targets)) if len(targets) == 1 else None
            approved = candidate and any(row[1] for row in claims)
            exists = approved and conn.execute(
                "SELECT 1 FROM catalog_character WHERE id=?", (candidate,)).fetchone()
            conflict = not exists or (owner is not None and owner[0] != candidate)
            if conflict:
                return (owner[0] if owner else None), {
                    "kind": "alias", "route": route.public(),
                    "reason": "conflicting, unverified or unknown alias target",
                    "decision_ids": [row[2] for row in claims],
                }
            if owner is None:
                conn.execute("INSERT INTO catalog_route_owner VALUES (?,?)", (route.key, candidate))
                return candidate, None
        if owner:
            return owner[0], None
        count = conn.execute("SELECT COUNT(*) FROM catalog_character").fetchone()[0]
        name = _NAMES[count % len(_NAMES)]
        if count >= len(_NAMES):
            name += f" {1 + count // len(_NAMES)}"
        cid = str(uuid4())
        conn.execute("INSERT INTO catalog_character VALUES (?,?)", (cid, name))
        conn.execute("INSERT INTO catalog_route_owner VALUES (?,?)", (route.key, cid))
        return cid, None

    def reconcile(self, configurations, *, complete, aliases=(), legacy_refs=()):
        """Publish a complete synthetic inventory atomically, returning its revision.

        `legacy_refs` are (source namespace, legacy ID) pairs. They only produce
        unresolved references. This slice cannot link or write any legacy row.
        """
        if complete is not True:
            raise ValueError("complete configuration inventory required")
        observed = discover_configuration(configurations)
        legacy = {(_public_token(source), _public_token(key)) for source, key in legacy_refs}
        with self._write() as conn:
            previous, view = _read(conn)
            baseline = _dump(view)
            for claim in aliases:
                self._remember_alias(conn, claim)

            # Retain historical bindings in the selected branch. Only routes
            # declared in the current complete inventory become configured.
            for character in view["characters"].values():
                character["status"] = "archived"
            for collection in ("bindings", "services"):
                for row in view[collection].values():
                    row["status"] = "retired"

            legacy.update((row["namespace"], row["legacy_id"])
                          for row in view["unresolved"] if row["kind"] == "legacy")
            unresolved = [{"kind": "legacy", "namespace": ns, "legacy_id": key,
                           "reason": "historical ownership not established"}
                          for ns, key in sorted(legacy)]
            owners = {}
            for route, cap, kind, enabled in observed:
                status = "configured" if enabled else "disabled"
                fields = {**route.public(), "capability": cap, "status": status}
                if kind == "service":
                    sid = str(uuid5(NAMESPACE_URL, _dump(("friday-service", route.key, cap))))
                    view["services"][sid] = {"id": sid, "kind": "service",
                                             "representation": "procedural", **fields}
                    continue
                if route not in owners:
                    owner, issue = self._resolve_owner(conn, route)
                    owners[route] = owner
                    if issue:
                        unresolved.append(issue)
                owner = owners[route]
                if not owner:
                    continue
                name = conn.execute("SELECT name FROM catalog_character WHERE id=?", (owner,)).fetchone()[0]
                view["characters"][owner] = {**view["characters"].get(owner, {}),
                                             "id": owner, "name": name, "status": "configured"}
                bid = str(uuid5(UUID(owner), _dump((route.key, cap))))
                preference = view["bindings"].get(bid, {}).get("can_serve")
                view["bindings"][bid] = {"id": bid, "character_id": owner, **fields}
                if preference is not None:
                    view["bindings"][bid]["can_serve"] = preference

            # Preserve inactive unresolved decisions for audit even if the
            # declared route is absent in this revision. Re-evaluate observed
            # routes from the stored claims above instead of carrying stale issues.
            unresolved.extend(row for row in view["unresolved"]
                              if row["kind"] == "alias" and Route(**row["route"]) not in owners)
            view["unresolved"] = sorted(unresolved, key=_dump)
            payload = _dump(view)
            if payload == baseline:
                return previous
            rev = conn.execute("INSERT INTO catalog_revision(parent,projection) VALUES (?,?)",
                               (previous, payload)).lastrowid
            conn.execute("UPDATE catalog_head SET revision=?", (rev,))
            return rev

