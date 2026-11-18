"""Opt-in capability observations and paired catalog-backed projections.

Callers supply exact public deployment references at trusted runtime
boundaries. This module neither selects providers nor consumes legacy router
notifications that lack deployment, request and attempt ownership.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .identity_catalog import Route, _public_token, read_shadow_projection, serving_editable

_CAPABILITIES = ("brain", "vision")
_HISTORY_ID = 0x46524459
TOWN_FAILURE_SECONDS = 30


@dataclass(frozen=True, eq=False)
class Request:
    """Process-local admission handle. The ID is an opaque trace label."""

    id: str
    order: int


@dataclass(frozen=True, eq=False)
class Attempt:
    """Only the admitted attempt can publish its terminal observation."""

    request: Request
    capability: str
    generation: int


class DutyShadow:
    def __init__(self, catalog_path=None, history_path=None, *, enabled=False, existing=False):
        if type(enabled) is not bool:
            raise ValueError("shadow gate must be boolean")
        self.enabled = enabled
        self._lock = threading.RLock()
        self._requests = {}
        self._next_order = 0
        self._current = {}
        self._watermarks = {}
        self._failures = {}
        self._rest_offered = set()
        if not enabled:
            return
        if catalog_path is None or history_path is None:
            raise ValueError("shadow requires explicit catalog and history paths")
        self._catalog = Path(catalog_path).resolve()
        self._history = Path(history_path).resolve()
        self._history_existing = existing
        read_shadow_projection(self._catalog)
        self._epoch = uuid4().hex
        with self._history_writer() as conn:
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            if (not existing and not app_id
                    and not conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()):
                conn.execute("CREATE TABLE duty_session (id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL)")
                conn.execute("CREATE TABLE duty_success (capability TEXT PRIMARY KEY, record TEXT NOT NULL)")
                conn.execute(f"PRAGMA application_id={_HISTORY_ID}")
            elif app_id != _HISTORY_ID:
                raise ValueError("expected a dedicated duty history database")
            conn.execute("INSERT OR REPLACE INTO duty_session VALUES (1, ?)", (self._epoch,))

    @contextmanager
    def _history_writer(self):
        uri = self._history.as_uri() + ("?mode=rw" if self._history_existing else "?mode=rwc")
        with closing(sqlite3.connect(uri, uri=True)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def check_existing_history(path):
        uri = Path(path).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            if conn.execute("PRAGMA application_id").fetchone()[0] != _HISTORY_ID:
                raise ValueError("expected a dedicated duty history database")
            if not conn.execute("SELECT epoch FROM duty_session WHERE id=1").fetchone():
                raise ValueError("missing duty history session")
            conn.execute("SELECT capability, record FROM duty_success LIMIT 0")

    def _owns_history(self, conn):
        row = conn.execute("SELECT epoch FROM duty_session WHERE id=1").fetchone()
        return row is not None and row[0] == self._epoch

    @contextmanager
    def _history_reader(self):
        uri = self._history.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.execute("BEGIN")
            yield conn

    def _owns_session(self):
        with self._history_reader() as conn:
            return self._owns_history(conn)

    def begin_request(self, request_id):
        if not self.enabled:
            return None
        request_id = _public_token(request_id)
        with self._lock:
            if not self._owns_session():
                return None
            if request_id in self._requests:
                raise ValueError("request already admitted")
            self._next_order += 1
            request = Request(request_id, self._next_order)
            self._requests[request_id] = request
            return request

    def _admitted(self, request):
        return isinstance(request, Request) and self._requests.get(request.id) is request

    def observe_selected(self, request, capability, generation, route):
        """Accept an exact selection observation or return None without mutation.

        The capability watermark survives terminal callbacks, so an older
        request or generation cannot reclaim the slot after it has been cleared.
        An unknown route is explicitly unbound; a known disabled/retired route
        is refused. The tracker does not select a replacement.
        """
        if not self.enabled:
            return None
        if (capability not in _CAPABILITIES or type(generation) is not int
                or generation < 1 or not isinstance(route, Route)):
            raise ValueError("expected capability, positive generation and public Route")
        with self._lock:
            if not self._admitted(request) or not self._owns_session():
                return None
            key = (request.order, generation)
            if key <= self._watermarks.get(capability, (0, 0)):
                return None
            catalog = read_shadow_projection(self._catalog)
            route_fields = route.public()
            binding = next((b for b in catalog["bindings"].values()
                            if b["capability"] == capability
                            and all(b[field] == value for field, value in route_fields.items())), None)
            if binding is not None and binding["status"] != "configured":
                return None
            attempt = Attempt(request, capability, generation)
            self._current[capability] = (attempt, {
                "capability": capability, "request_id": request.id, "generation": generation,
                "route": route_fields, "admitted_revision": catalog["revision"],
                "binding_id": binding["id"] if binding else None,
                "character_id": binding["character_id"] if binding else None,
                "resolution": "bound" if binding else "unbound",
            })
            self._watermarks[capability] = key
            return attempt

    def finish(self, attempt, outcome):
        """Accept exactly one terminal observation, scoped to its capability."""
        if not self.enabled or not isinstance(attempt, Attempt):
            return False
        if outcome not in ("success", "failure", "cancelled"):
            raise ValueError("invalid observation outcome")
        with self._lock:
            entry = self._current.get(attempt.capability)
            if entry is None or entry[0] is not attempt or not self._admitted(attempt.request):
                return False
            if outcome == "success":
                record = {**entry[1], "completed_at": time.time()}
                with self._history_writer() as conn:
                    if not self._owns_history(conn):
                        return False
                    conn.execute("INSERT OR REPLACE INTO duty_success VALUES (?,?)",
                                 (attempt.capability, json.dumps(record, sort_keys=True)))
            elif not self._owns_session():
                return False
            binding_id = entry[1]["binding_id"]
            if binding_id and outcome == "failure":
                self._failures.pop(binding_id, None)
                self._failures[binding_id] = {**entry[1], "observed_at": time.monotonic()}
                self._rest_offered.discard(binding_id)
                if len(self._failures) > 128:
                    expired = next(iter(self._failures))
                    del self._failures[expired]
                    self._rest_offered.discard(expired)
            elif binding_id and outcome == "success":
                self._failures.pop(binding_id, None)
                self._rest_offered.discard(binding_id)
            del self._current[attempt.capability]
            return True

    def claim_rest(self, failure):
        """Offer a procedural rest at most once for this exact failed attempt."""
        with self._lock:
            key = failure["binding_id"]
            if (self._failures.get(key) != failure or key in self._rest_offered or
                    any(record["character_id"] == failure["character_id"]
                        for _, record in self._current.values())):
                return False
            self._rest_offered.add(key)
            return True

    def has_rest_cues(self):
        with self._lock:
            return any(key not in self._rest_offered
                       and time.monotonic() - row["observed_at"] < TOWN_FAILURE_SECONDS
                       for key, row in self._failures.items())

    def end_request(self, request):
        if not self.enabled:
            return False
        with self._lock:
            if not self._admitted(request) or not self._owns_session():
                return False
            del self._requests[request.id]
            for cap, (attempt, _) in list(self._current.items()):
                if attempt.request is request:
                    del self._current[cap]
            return True

    def snapshot(self, selected_character_id=None, *, catalog_revision=None):
        """Project both views from one catalog revision and one exact selection."""
        if not self.enabled:
            return {"enabled": False, "reason": "shadow gate disabled"}
        with self._lock:
            with self._history_reader() as conn:
                if not self._owns_history(conn):
                    return {"enabled": False, "reason": "shadow session replaced"}
                history = {cap: json.loads(row)
                           for cap, row in conn.execute("SELECT capability,record FROM duty_success")}
            catalog = read_shadow_projection(self._catalog, revision=catalog_revision)
            characters = catalog["characters"]
            selected = selected_character_id if isinstance(selected_character_id, str) and selected_character_id in characters else None
            binding_map = catalog["bindings"]
            failures = [{**record, "route": dict(record["route"])} for key, record in self._failures.items()
                        if key in binding_map and serving_editable(catalog, binding_map[key])]
            current = {}
            for cap, (_, record) in self._current.items():
                binding = binding_map.get(record["binding_id"])
                # A revision change never reassigns an admitted observation.
                # An admitted binding can finish after retirement. Rollback may
                # hide it entirely, in which case the current view is unbound.
                current[cap] = {
                    **record,
                    "route": dict(record["route"]),
                    "binding_status": binding["status"] if binding else None,
                    "binding_id": record["binding_id"] if binding else None,
                    "character_id": record["character_id"] if binding else None,
                    "resolution": "bound" if binding else "unbound",
                }
            by_character = {}
            for binding in binding_map.values():
                by_character.setdefault(binding["character_id"], []).append({
                    **binding,
                    "saved_serving": binding.get("can_serve"),
                    "serving_allowed": (binding.get("can_serve", True)
                                        if binding["status"] == "configured" else None),
                    "serving_editable": serving_editable(catalog, binding),
                })
            identities = [
                {**character, "bindings": by_character.get(cid, []),
                 "duty": sorted(cap for cap, item in current.items() if item["character_id"] == cid)}
                for cid, character in sorted(characters.items(), key=lambda row: (row[1]["name"], row[0]))
            ]
            common = {"catalog_revision": catalog["revision"], "selected_character_id": selected}
            services = list(catalog["services"].values())
            return {
                "enabled": True, **common,
                "duty": {"current": current, "last_success": history},
                "route_failures": failures,
                "roster": {**common, "characters": identities, "services": services,
                           "unresolved": catalog["unresolved"]},
                "town": {**common, "residents": [row for row in identities if row["status"] == "configured"],
                         "services": [row for row in services if row["status"] != "retired"]},
            }

