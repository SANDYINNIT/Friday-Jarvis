"""Select and verify an installed catalog before ordinary Light startup."""
from contextlib import closing
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sqlite3

from .identity_integration import IdentityIntegration, discover_inventory
from .world.identity_catalog import (
    IdentityCatalog, Route, discover_configuration, read_shadow_projection, route_fields,
)
from .world.identity_shadow import DutyShadow

CONFIG_ERROR_EXIT = 78
_SELECTORS = ("FRIDAY_IDENTITY_DEPLOYMENTS", "FRIDAY_IDENTITY_CATALOG_DB", "FRIDAY_IDENTITY_HISTORY_DB")
_GATES = ("FRIDAY_IDENTITY_INTEGRATION", "FRIDAY_IDENTITY_READER")
_MAX_MANIFEST_BYTES = 64 * 1024


class CatalogStartupError(ValueError):
    """An actionable startup error without endpoint, credential or path values."""


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate manifest key")
        obj[key] = value
    return obj


def _load_manifest(value):
    if len(value) > _MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds size limit")
    manifest = json.loads(value, object_pairs_hook=_unique_object)
    if (not isinstance(manifest, dict) or set(manifest) != {"complete", "deployments", "services"}
            or manifest["complete"] is not True or not isinstance(manifest["deployments"], dict)
            or not manifest["deployments"] or not isinstance(manifest["services"], list)):
        raise ValueError("complete manifest required")
    return manifest


def _existing_file(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("expected an existing absolute file path")
    return path.resolve(strict=True)


def _check_world(world, catalog_view):
    # Select only schema and linkage metadata. Never deserialize the personal world here.
    uri = world.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("BEGIN")
        required = {
            "meta": "key,value", "characters": "id,name,persona,x,y,room,state,needs_json,goal,task_ref,blocked_reason,memory_json,dwell",
            "reservations": "loc_id,char_id", "events": "id,tick,actor,kind,text,event_key,created_at",
            "agents": "id,name,role,model,state,task_id,permissions_json,budget_json,last_error,created_at,updated_at",
            "roster_tasks": "id,goal,owner,state,steps,artifacts_json,error,approval_required,paused_by,created_at,updated_at,category",
            "edges": "id,src,dst,kind,state,at,message", "checkpoints": "tick,created_at,snapshot_json",
        }
        for table, columns in required.items():
            conn.execute(f"SELECT {columns} FROM {table} LIMIT 0")
        indexes = {row[1]: row[2] for row in conn.execute("PRAGMA index_list(events)")}
        if indexes.get("world_event_key") != 1:
            raise ValueError("world schema requires review")
        linked = set()
        rows = conn.execute(
            "SELECT l.namespace,l.resident_id,l.character_id,l.record,i.state,c.id "
            "FROM identity_resident_links l LEFT JOIN identity_installations i ON i.id=l.batch_id "
            "LEFT JOIN characters c ON c.id=l.resident_id")
        characters = catalog_view["characters"]
        for namespace, resident_id, character_id, raw, state, existing_id in rows:
            link = json.loads(raw)
            if (state != "applied" or existing_id is None or namespace != "town"
                    or character_id not in characters or link.get("verified") is not True
                    or link.get("namespace") != namespace or link.get("resident_id") != resident_id
                    or link.get("character_id") != character_id):
                raise ValueError("world contains an incompatible installed link")
            linked.add(character_id)
        active = {cid for cid, row in characters.items() if row["status"] != "retired"}
        if not active or not active <= linked:
            raise ValueError("world is missing verified installed residents")


def _check_inventory(manifest, router, vision, catalog_view):
    rows, matchers = discover_inventory(manifest, router, vision, require_all=True)
    expected = set(discover_configuration(rows))
    actual = set()
    for field, kind in (("bindings", "reasoning"), ("services", "service")):
        for row in catalog_view[field].values():
            if row["status"] != "retired":
                if row["status"] not in ("configured", "disabled"):
                    raise ValueError("unexpected catalog state")
                actual.add((Route(**route_fields(row)), row["capability"], kind, row["status"] == "configured"))
    if expected != actual or catalog_view["unresolved"]:
        raise ValueError("resolver inventory and enrolled catalog differ")
    return matchers


@dataclass(frozen=True)
class InstalledCatalog:
    world: Path
    catalog: Path
    history: Path
    revision: int
    matchers: dict = field(repr=False)

    def attach(self):
        # Attach to the reviewed revision. Never reconcile or create these stores at boot.
        try:
            catalog = IdentityCatalog.open_existing(self.catalog, self.revision)
            shadow = DutyShadow(catalog.path, self.history, enabled=True, existing=True)
        except (ValueError, sqlite3.Error, OSError):
            raise CatalogStartupError("The reviewed catalog or duty history could not be attached. "
                                      "Check that the installed stores are accessible and unchanged.") from None
        return IdentityIntegration(catalog, shadow, self.matchers)


def prepare(env=None, home=None, *, resolver=None):
    """Read-only selection and parity validation. Called before profiles and services."""
    env = os.environ if env is None else env
    mode = env.get("FRIDAY_CATALOG_MODE", "installed")
    if env.get("FRIDAY_USER_ROUTES") and mode != "legacy":
        raise CatalogStartupError("Local user-route edits require a separately reviewed catalog inventory/enrollment. Existing catalog selection was not changed.")
    if mode not in ("installed", "explicit", "legacy"):
        raise CatalogStartupError("FRIDAY_CATALOG_MODE must be installed, explicit or legacy.")
    if env.get("FRIDAY_TOWN_INSTALL_OPERATOR", "0") not in ("", "0"):
        raise CatalogStartupError("Ordinary startup requires FRIDAY_TOWN_INSTALL_OPERATOR to be unset or 0.")
    if mode == "legacy":
        if any(env.get(name) for name in _SELECTORS) or any(env.get(name, "0") not in ("", "0") for name in _GATES):
            raise CatalogStartupError("Legacy mode conflicts with identity selectors or enabled identity gates. "
                                      "Clear them before starting in legacy mode.")
        return None
    if any(env.get(name, "1") != "1" for name in _GATES):
        raise CatalogStartupError("Catalog startup requires both identity gates enabled. "
                                  "Clear conflicting gates or deliberately select legacy mode.")

    home = Path.home() if home is None else Path(home)
    profile = home / ".friday"
    default_world = profile / "world.db"
    configured_world = env.get("FRIDAY_WORLD_DB")
    if mode == "installed" and any(name in env for name in _SELECTORS):
        raise CatalogStartupError("Installed mode uses the protected production manifest and database pair. "
                                  "Clear identity selector overrides or choose explicit mode.")
    if mode == "explicit" and (not all(env.get(name) for name in _SELECTORS) or not configured_world):
        raise CatalogStartupError("Explicit mode requires a complete inline manifest, both identity database "
                                  "selectors, and FRIDAY_WORLD_DB.")

    try:
        world = _existing_file(configured_world or default_world)
        if mode == "installed" and world != _existing_file(default_world):
            raise ValueError("world selector conflicts with installed mode")
    except (ValueError, OSError, RuntimeError):
        raise CatalogStartupError("The selected saved Town is missing or conflicts with the installed world. "
                                  "Check FRIDAY_WORLD_DB and the saved world selector.") from None
    try:
        if mode == "installed":
            directory = profile.resolve() / "catalog-activation"
            selected_paths = [directory / name for name in
                              ("deployments.json", "identity-catalog.db", "identity-duty-history.db")]
            manifest_path, catalog, history = map(_existing_file, selected_paths)
            if [manifest_path, catalog, history] != selected_paths:
                raise ValueError("protected configuration was redirected")
            with manifest_path.open("rb") as stream:
                manifest = _load_manifest(stream.read(_MAX_MANIFEST_BYTES + 1))
        else:
            manifest = _load_manifest(env[_SELECTORS[0]])
            catalog = _existing_file(env[_SELECTORS[1]])
            history = _existing_file(env[_SELECTORS[2]])
        if len({world, catalog, history}) != 3:
            raise ValueError("stores must be distinct")
    except (ValueError, OSError, RuntimeError):
        raise CatalogStartupError("The selected manifest or identity database pair is missing or invalid. "
                                  "Check the approved manifest and both existing database files.") from None

    try:
        catalog_view = read_shadow_projection(catalog)
        revision = catalog_view["revision"]
        if type(revision) is not int or revision < 1:
            raise ValueError("catalog has no published revision")
        DutyShadow.check_existing_history(history)
    except (ValueError, KeyError, TypeError, sqlite3.Error, OSError):
        raise CatalogStartupError("The selected catalog revision or duty history is invalid. "
                                  "Check the reviewed database pair.") from None
    try:
        if resolver is None:
            from . import brain_router, screen_understanding
            resolver = (brain_router, screen_understanding)
        matchers = _check_inventory(manifest, *resolver, catalog_view)
    except Exception:
        raise CatalogStartupError("The current resolver inventory differs from the installed catalog or manifest. "
                                  "Review route and deployment configuration before starting FRIDAY.") from None
    try:
        _check_world(world, catalog_view)
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY_ROLLBACK:
            raise CatalogStartupError("The saved Town has a pending SQLite rollback journal. "
                                      "Keep FRIDAY stopped and arrange SQLite recovery before relaunching. "
                                      "Do not remove the journal.") from None
        raise CatalogStartupError("The saved Town could not be read. Check access to the selected world "
                                  "and its SQLite files.") from None
    except (ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, OSError):
        raise CatalogStartupError("The saved Town schema or installed links do not match this catalog. "
                                  "Review the selected world and installation receipt.") from None
    return InstalledCatalog(world, catalog, history, revision, matchers)

