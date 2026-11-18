"""Future Lab runtime state â€” the living world, the roster and their events.

One process-wide singleton so the HUD bridge, HTTP routes and tests all see the
same world. Everything here is **procedural and offline**: the tick thread does
no model calls, takes no server locks, and is a daemon, so it can never block
voice or an urgent stop.

The ticker is started lazily (`ensure_started`) the first time the HUD asks for
any lab data, which means it runs whenever the FRIDAY window exists (including
a tray auto-start, hidden) and simply does not run in a process with no UI.

Character needs/memory live ONLY in `world.db`. They never read or write FRIDAY
personal memory, provider quota or `friday_tasks.db`.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import threading
import time
from collections import deque

from .world import Roster, Store, new_world, snapshot, tick as world_tick

TICK_SECONDS = 1.0          # one world tick per second (10 in-world seconds)
SAVE_EVERY_TICKS = 60       # periodic durability, not per tick
EVENT_RING = 400

_lock = threading.RLock()
_world = None
_store = None
_roster = None
_thread = None
_stop = threading.Event()
_events = deque(maxlen=EVENT_RING)
_seen_world_events = 0
_started = False
_start_error = ""
_background_life = None
_installed_world_path = None
_preserve_startup_pause = False


def select_installed_world(path):
    """Pin the preflighted world before the normal server or HUD can load it."""
    global _installed_world_path
    with _lock:
        if _store is not None or _started:
            raise RuntimeError("the saved Town must be selected before startup")
        _installed_world_path = path


def db_path() -> str:
    """Single source of truth for the LAB database path.

    Delegates to `world.store.default_path()` (which honours `FRIDAY_WORLD_DB`)
    instead of duplicating the `~/.friday` literal - the two had already drifted
    into separate copies of the same string.
    """
    from .world.store import default_path
    return str(_installed_world_path if _installed_world_path is not None else default_path())


def _emit(kind, data):
    try:
        _events.appendleft({"type": kind, "at": round(time.time(), 3),
                            "data": data if isinstance(data, dict) else {}})
    except Exception:
        pass


def ensure_started() -> bool:
    """Idempotent: start the tick thread on first use."""
    global _thread, _started, _start_error
    with _lock:
        if _started:
            return _thread is not None
        _started = True            # reserve: prevents concurrent double-start
    try:
        _load_world()
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="friday-world", daemon=True)
        _thread.start()
        _emit("world_started", {"db": db_path()})
        return True
    except Exception as error:
        _start_error = "%s: %s" % (type(error).__name__, error)
        # LAB-3R6: a FAILED start must be retryable. Production has no
        # reset() caller, and the old code latched _started=True BEFORE the
        # try, so one bad boot permanently disabled the ticker for the whole
        # process (reproduced: every later ensure_started() returned False
        # even after the fault cleared). Release the reservation instead.
        with _lock:
            _started = False
        # Never swallow a startup failure silently: it must reach the console
        # log, otherwise "the world never ticks" is invisible from outside.
        try:
            print("[world] ticker start failed: %s" % _start_error, flush=True)
        except Exception:
            pass
        return False


def start():
    """Explicit start (used by tests and by future server wiring)."""
    return ensure_started()


def stop():
    global _thread, _started
    _stop.set()
    invalidate_background_life()
    with _lock:
        thread = _thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    with _lock:
        _thread = None
        _started = False


def reset():
    """Full teardown: stop the ticker AND drop in-memory state.

    `stop()` alone deliberately keeps the world loaded (a HUD reopen should not
    throw away unsaved ticks), so tests must use `reset()` to get a clean slate.
    """
    global _world, _store, _roster, _seen_world_events, _background_life
    global _installed_world_path, _preserve_startup_pause
    stop()
    with _lock:
        store = _store
        _world = None
        _store = None
        _roster = None
        _background_life = None
        _installed_world_path = None
        _preserve_startup_pause = False
        _events.clear()
        _seen_world_events = 0
    if store is not None:
        try:
            store.close()
        except Exception:
            pass


def _load_world():
    global _world, _store, _roster, _preserve_startup_pause
    with _lock:
        if _world is not None:
            return _world, _roster
        if _installed_world_path is not None:
            store = Store(_installed_world_path, existing=True)
        else:
            store = Store(db_path())
        try:
            world = store.load()
            if _installed_world_path is not None and not world.characters:
                raise ValueError("installed Town could not be read")
            roster = Roster(store)
            if not world.characters:
                world = new_world()
                store.save(world)
        except Exception:
            store.close()
            raise
        _store, _world, _roster = store, world, roster
        _preserve_startup_pause = _installed_world_path is not None and world.paused
        if not _roster.agents():
            _seed_agents(_roster)
        _grant_default_permissions(_roster)
        return _world, _roster


def _seed_agents(roster):
    """A usable roster out of the box â€” no fake states, all budgeted.

    `llm` is the permission the executor checks before spending a model call;
    without it a task is REFUSED with a visible reason rather than silently
    doing nothing. Character/simulated life never grants it - this is task
    execution only.
    """
    defaults = (
        ("friday", "FRIDAY", "coordinator",
         ["assign", "review", "speak", "llm"],
         {"max_steps": 20, "max_model_calls": 10, "max_seconds": 120}),
        ("research", "Research", "researcher", ["read", "search", "llm"],
         {"max_steps": 8, "max_model_calls": 4, "max_seconds": 90}),
        ("code", "Code", "builder", ["read", "llm"],
         {"max_steps": 6, "max_model_calls": 3, "max_seconds": 90}),
        ("vision", "Vision", "reviewer", ["read", "llm"],
         {"max_steps": 6, "max_model_calls": 3, "max_seconds": 90}),
    )
    for agent_id, name, role, permissions, budget in defaults:
        roster.add_agent(agent_id, name, role, model="runtime selected",
                         permissions=permissions, budget=budget)
    roster._save()


# What each SEEDED agent is allowed to do. Used only for the four agents this
# module creates; an agent Sir defines himself is never touched.
_DEFAULT_PERMISSIONS = {
    "friday": {"assign", "review", "speak", "llm"},
    "research": {"read", "search", "llm"},
    "code": {"read", "llm"},
    "vision": {"read", "llm"},
}


def _grant_default_permissions(roster):
    """Upgrade a roster that was seeded before the executor existed.

    Measured failure: agents created before ROUND ZB-EX have no `llm`
    permission, so every assignment is refused with `lacks permission 'llm'`
    and no provider is ever called. Grant only what is missing, only to the
    known seeded ids - never invent permissions for a user-defined agent.
    """
    changed = False
    for agent_id, required in _DEFAULT_PERMISSIONS.items():
        agent = roster.agent(agent_id)
        if not agent:
            continue
        current = set(agent.get("permissions") or ())
        missing = required - current
        if missing:
            roster._agents[agent_id]["permissions"] = sorted(current | required)
            changed = True
    if changed:
        roster._save()
    return changed


# --- the executor + DURABLE QUEUE (ROUND LAB-2A) -----------------------------
_executor = None
_executor_lock = threading.Lock()      # also guards the execution queue below
_executor_thread = None
# Admission queue: entries {"task_id", "kwargs", "payload_hash", "at"}.
# Capacity is an engineering choice (the user's A-brief): default 3, one owner.
EXECUTION_QUEUE_CAP_DEFAULT = 3
_execution_queue = collections.deque()
_active_execution = None               # reserved before a thread can run
_executor_identity = None
# task_id -> category ("user_work" | "life"); consulted by `note()` so every
# downstream executor event re-reads the honest bucket. Popped after a run.
_task_buckets = {}


def _queue_cap() -> int:
    """Configurable queue capacity (engineering choice, labelled as such).

    `FRIDAY_EXECUTION_QUEUE_CAP` overrides at call time; the default matches
    the plan's conservative bounded-FIFO proposal (3). Clamped 1..50.
    """
    try:
        value = int(os.environ.get("FRIDAY_EXECUTION_QUEUE_CAP",
                                  EXECUTION_QUEUE_CAP_DEFAULT))
    except Exception:
        value = EXECUTION_QUEUE_CAP_DEFAULT
    return max(1, min(50, value))


def _payload_hash(kwargs) -> str:
    """Stable identity for ONE task admission: task_id identifies the row and
    this hash identifies the payload (~system prompt, timeout). Deterministic.
    """
    try:
        payload = json.dumps(
            {k: v for k, v in (kwargs or {}).items() if k != "background"},
            sort_keys=True, default=str)
    except Exception:
        payload = str(sorted((kwargs or {}).items()))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def executor():
    """Process-wide executor for the loaded roster."""
    global _executor
    with _executor_lock:
        _, roster = _load_world()
        if _executor is None or _executor.roster is not roster:
            from .world.executor import AgentExecutor
            _executor = AgentExecutor(roster, note=note, identity=_executor_identity)
        return _executor


def configure_execution_identity(integration):
    """Reuse the startup catalog authority without opening one for workers."""
    global _executor_identity
    with _executor_lock:
        _executor_identity = integration
        if _executor is not None:
            _executor.identity = integration


def _live_duty_active():
    try:
        from . import lab_authority
        authority = lab_authority.shared()
        return bool(authority and authority.serving().get("active") is True)
    except Exception:
        return False


def _start_next_locked(runner):
    global _active_execution, _executor_thread
    if _active_execution is not None or _live_duty_active():
        return
    while _execution_queue:
        entry = _execution_queue.popleft()
        if not runner.roster.owns_attempt(entry["task_id"], entry["attempt"], "queued"):
            continue
        _active_execution = entry
        _task_buckets[entry["task_id"]] = entry["bucket"]
        try:
            _executor_thread = threading.Thread(target=_worker, args=(runner, entry),
                                                name="friday-agent-exec", daemon=True)
            _executor_thread.start()
            return
        except Exception as error:
            reason = "worker start failed: " + type(error).__name__
            runner.roster.fail(entry["task_id"], reason, attempt=entry["attempt"])
            _task_buckets.pop(entry["task_id"], None)
            _active_execution = _executor_thread = None
            note("task_failed", {"task": entry["task_id"], "attempt": entry["attempt"],
                                 "bucket": entry["bucket"], "reason": reason})


def _drain_execution_queue():
    with _executor_lock:
        if _executor is not None and _execution_queue:
            _start_next_locked(_executor)


def execute_task(task_id, *, background=True, category=None, retry=False,
                 **kwargs) -> dict:
    """One bounded FIFO and one durable admission for both entry modes."""
    global _active_execution
    runner = executor()
    roster = runner.roster
    task_id = str(task_id)
    with _executor_lock:
        task = roster.task(task_id)
        if not task:
            return {"ok": False, "queued": False, "reason": "unknown task %s" % task_id}
        bucket = str(category or task.get("category") or "user_work")
        payload_hash = _payload_hash({**kwargs, "category": bucket})
        if _active_execution is not None and _active_execution["task_id"] == task_id:
            return {"ok": False, "queued": False, "reason": "task is already being executed"}
        for entry in _execution_queue:
            if entry["task_id"] != task_id:
                continue
            if entry["payload_hash"] == payload_hash:
                return {"ok": True, "queued": True, "dedup": True,
                        "task_id": task_id, "attempt": entry["attempt"]}
            return {"ok": False, "queued": False, "reason": "already queued with a different payload"}

        _start_next_locked(runner)
        deferred = _live_duty_active()
        wait = _active_execution is not None or deferred
        capacity = _queue_cap()
        if len(_execution_queue) >= capacity:
            reason = "execution queue is full (%d queued); admission refused" % capacity
            note("task_queue_refused", {"task": task_id, "reason": reason})
            return {"ok": False, "queued": False, "reason": reason,
                    "state": task["state"], "capacity": capacity}
        if bucket == "life":
            from . import lab_budget
            budget = lab_budget.shared()
            if budget is not None:
                pending = sum(entry["bucket"] == "life" for entry in _execution_queue)
                outcome = budget.check("life", str(task.get("owner") or ""),
                                       role="worker", pending_reserved=pending)
                if not outcome.get("ok"):
                    reason = str(outcome.get("reason") or "life budget denied")
                    roster.pause_for_rest(task_id, "background budget")
                    note("task_queue_refused", {"task": task_id, "bucket": bucket, "reason": reason})
                    return {"ok": False, "queued": False, "reason": reason}

        admitted = roster.admit(task_id, category=bucket, retry=retry)
        if not admitted.get("ok"):
            return {**admitted, "queued": False}
        entry = {"task_id": task_id, "attempt": admitted["attempt"], "kwargs": kwargs,
                 "payload_hash": payload_hash, "bucket": bucket}
        if background or wait:
            _execution_queue.append(entry)
            position = len(_execution_queue)
            note("task_queued", {"task": task_id, "attempt": entry["attempt"], "bucket": bucket,
                                 "position": position, "capacity": capacity})
            _start_next_locked(runner)
            persisted = roster.task(task_id)
            if persisted.get("state") == "failed":
                return {"ok": False, "queued": False, "state": "failed",
                        "reason": persisted.get("error"), "attempt": entry["attempt"]}
            return {"ok": True, "queued": True, "task_id": task_id,
                    "attempt": entry["attempt"],
                    **({"started": True} if _active_execution is entry else {}),
                    "position": position, "capacity": capacity,
                    "deferred_for_live_duty": deferred}
        _active_execution = entry
        _task_buckets[task_id] = bucket
    return _worker(runner, entry)


def _worker(runner, entry):
    """Hold the slot through result persistence, then drain the next admission."""
    global _active_execution, _executor_thread
    task_id, attempt = entry["task_id"], entry["attempt"]
    try:
        return _safe_execute(runner, task_id, attempt, entry["kwargs"])
    finally:
        with _executor_lock:
            if _active_execution is entry:
                _task_buckets.pop(task_id, None)
                _active_execution = _executor_thread = None
                _start_next_locked(runner)


def _safe_execute(runner, task_id, attempt, kwargs):
    try:
        result = runner.execute(task_id, attempt=attempt, **kwargs)
        if isinstance(result, dict):
            if not result.get("ok") and runner.roster.owns_attempt(task_id, attempt, "queued", "running"):
                reason = result.get("reason") or "execution could not start or finish"
            else:
                return result
        else:
            reason = "executor returned no result"
    except Exception as error:
        reason = "execution error: " + type(error).__name__
    failed = runner.roster.fail(task_id, reason, attempt=attempt)
    if failed.get("ok"):
        note("task_failed", {"task": task_id, "attempt": attempt, "reason": reason})
    return {"ok": False, "reason": reason, "attempt": attempt,
            "state": runner.roster.task(task_id).get("state")}


def cancel_execution(reason="user", task_id=None, *, expected_attempt=None) -> dict:
    """Global Stop parks the current attempt. An explicit id can park queued work."""
    runner = executor()
    with _executor_lock:
        entry = _active_execution
        if task_id and (entry is None or entry["task_id"] != str(task_id)):
            entry = next((row for row in _execution_queue if row["task_id"] == str(task_id)), None)
        if entry is None:
            return {"ok": True, "cancelled": False, "reason": "no matching attempt"}
        if expected_attempt is not None and entry["attempt"] != expected_attempt:
            return {"ok": False, "cancelled": False, "reason": "The active attempt changed."}
        result = runner.cancel(reason, task_id=entry["task_id"], attempt=entry["attempt"])
        if result.get("cancelled") and entry is not _active_execution:
            _execution_queue.remove(entry)
            _start_next_locked(runner)
        return result


def queue_state() -> dict:
    with _executor_lock:
        return {"capacity": _queue_cap(),
                "queued": [e["task_id"] for e in _execution_queue],
                "payload_hashes": {e["task_id"]: e["payload_hash"] for e in _execution_queue},
                "worker_alive": _active_execution is not None}


def _execution_queue_reset() -> None:
    """Tests/harnesses only: clear the in-memory scheduler."""
    global _active_execution, _executor_thread
    with _executor_lock:
        _active_execution = _executor_thread = None
        _execution_queue.clear()
        _task_buckets.clear()


def _reload_world():
    """Explicit reload from disk (the UI `load` command)."""
    global _world
    with _lock:
        _store_ref = _store or Store(db_path())
        _world = _store_ref.load()
        invalidate_background_life()
        return _world


def _loop():
    while not _stop.wait(TICK_SECONDS):
        try:
            _drain_execution_queue()
            identity = _executor_identity
            observations = None
            if identity is not None and os.environ.get("FRIDAY_IDENTITY_READER") == "1":
                try:
                    if identity.shadow.has_rest_cues():
                        observations = identity.shadow.snapshot()
                except Exception:
                    pass
            with _lock:
                world, _roster_ref = _load_world()
                if observations and identity is _executor_identity:
                    _offer_serving_rest(world, identity, observations)
                _advance_world(world)
            _service_background_life()
        except Exception:
            # A broken world must never take the process down.
            continue


def _offer_serving_rest(world, identity, observations):
    from .world.identity_reader import ResidentLink, _resolve_links
    from .world.identity_shadow import TOWN_FAILURE_SECONDS
    from .world.sim import offer_route_rest

    if world.paused or not observations.get("enabled"):
        return
    try:
        residents = {char.id: char for char in world.characters}
        characters = {char["id"]: char for char in observations["roster"]["characters"]}
        links, _ = _resolve_links([ResidentLink(**row) for row in world.identity_links],
                                  "town", characters, residents)
        for failure in observations.get("route_failures", ()):
            link = links.get(failure["character_id"])
            if link is not None and time.monotonic() - failure["observed_at"] < TOWN_FAILURE_SECONDS:
                offer_route_rest(world, residents[link.resident_id],
                                 lambda: identity.shadow.claim_rest(failure))
    except Exception:
        # A passive visual cue must never prevent the world from ticking.
        pass


def configure_background_life(identity, foreground):
    global _background_life
    from . import lab_budget
    from .world.background_life import BackgroundLife

    with _lock:
        invalidate_background_life()
        _background_life = BackgroundLife(
            identity, lab_budget.shared,
            lambda: foreground() or _active_execution is not None or bool(_execution_queue)
        ) if identity is not None else None
        return _background_life


def background_life():
    return _background_life


def invalidate_background_life():
    if _background_life is not None:
        _background_life.invalidate()


def _service_background_life():
    controller = _background_life
    if controller is None:
        return
    # Catalog and budget reads stay off the world lock. UI reads do not call
    # this scheduler, and manual stepping and catch-up remain procedural.
    view = controller.catalog_view()
    budget = controller.budget()
    enabled = budget.try_life_enabled() if budget else None
    if enabled is None and budget is not None:
        return
    with _lock:
        if controller is _background_life and _world is not None:
            controller.enabled = enabled
            controller.budget_observed_at = time.time() if enabled is not None else None
            if controller.cycle(_world, view):
                _save_world(_world)


def set_catalog_life(identity, character_id, binding_id, mode, revision):
    from .world.background_life import installed_links

    with _lock:
        controller = _background_life
        if controller is None or controller.identity is not identity or _world is None:
            raise ValueError("background scheduler unavailable")
        view = controller.catalog_view()
        updated, changed = identity.set_background_life(
            character_id, binding_id, mode, revision, installed_links(_world, view))
        if changed:
            controller.invalidate()
        return updated, changed


def _save_world(world):
    result = _store.save(world)
    for entry in result.pop("events", ()):
        _emit("world_event", dict(entry))
    return result


def _advance_world(world, *, force=False, pause_after=None, persist=True):
    """Advance under _lock and publish only events from a committed save."""
    if persist and world.pending_events:
        result = _save_world(world)
        if not result["ok"]:
            return result
    if world.paused and not force:
        return {"ok": True}
    paused = world.paused
    try:
        if force:
            world.paused = False
        world_tick(world)
    finally:
        world.paused = paused if pause_after is None else pause_after
    if persist and (force or world.pending_events or world.tick % SAVE_EVERY_TICKS == 0):
        return _save_world(world)
    return {"ok": True}


def install_catalog_residents(catalog, reader, proposal, *, links=(), confirmation=None):
    """Explicit caller only. No startup activation installs residents."""
    global _world
    from .world.identity_install import BackupFailed, apply

    with _lock:
        if _world is None or _store is None:
            return {"ok": False, "reason": "source world not loaded"}
        try:
            candidate, result = apply(catalog, _store, _world, reader, proposal,
                                      links=links, confirmation=confirmation)
            _world = candidate
            invalidate_background_life()
            for event in result.pop("events", ()):
                _emit("world_event", dict(event))
            return result
        except Exception as error:
            if confirmation is not None:
                if isinstance(error, BackupFailed):
                    return {"ok": False, "status": "backup_failed", "reason": str(error)}
                return {"ok": False, "status": "conflict" if isinstance(error, ValueError) else "error",
                        "reason": "Installation was not confirmed. Refresh the preview before retrying."}
            return {"ok": False, "reason": str(error)}


def rollback_catalog_installation(batch_id, *, operator=False):
    global _world
    from .world.identity_install import rollback

    with _lock:
        if _world is None or _store is None:
            return {"ok": False, "reason": "source world not loaded"}
        try:
            candidate, result = rollback(_store, _world, batch_id, operator=operator)
            _world = candidate
            invalidate_background_life()
            for event in result.pop("events", ()):
                _emit("world_event", dict(event))
            return result
        except Exception as error:
            if operator:
                return {"ok": False, "status": "conflict" if isinstance(error, ValueError) else "error",
                        "reason": "Rollback was not confirmed. Refresh to check for changes or later activity."}
            return {"ok": False, "reason": str(error)}


def town_install_operator(identity, action="preview", token=None):
    """Local operator boundary. Never starts Town, loads missing stores or trusts browser proposals."""
    from .world.identity_install import operator_blocks, operator_confirmation, operator_receipt
    from .world.identity_reader import IdentityReader

    def refused(reason, status="unavailable"):
        return {"ok": False, "status": status, "reason": reason}

    with _lock:
        if (os.environ.get("FRIDAY_IDENTITY_READER") != "1" or
                os.environ.get("FRIDAY_IDENTITY_INTEGRATION") != "1" or identity is None):
            return refused("The catalog integration and reader must already be available.")
        if _world is None or _store is None:
            return refused("Town is not loaded. No store was opened.")
        enabled = os.environ.get("FRIDAY_TOWN_INSTALL_OPERATOR") == "1"
        if action != "preview":
            if (action not in ("install", "rollback") or not isinstance(token, str)
                    or len(token) != 64 or any(c not in "0123456789abcdef" for c in token)):
                return refused("An exact preview or batch reference is required.", "conflict")
            if not enabled:
                return refused("Installation and rollback are disabled for this launch.")
        if action == "rollback":
            return rollback_catalog_installation(token, operator=True)
        try:
            catalog = identity.catalog
            if _store.path.resolve() == catalog.path:
                return refused("World and catalog must use separate databases.")
            reader = IdentityReader(identity.shadow, lambda: _world, enabled=True)
            with _store._lock:
                # Read-only SQLite snapshot of the saved state and history.
                if _store._conn.in_transaction:
                    return refused("Town storage is busy. Refresh before trying again.")
                _store._conn.execute("BEGIN")
                try:
                    view = reader.snapshot()
                    if not view.get("available") or not view.get("catalog_revision"):
                        return refused("The active catalog revision or Town snapshot is unavailable.")
                    proposal = view["installation_proposal"]
                    blocks = operator_blocks(_store, _world, view)
                    if not enabled:
                        blocks.append("Installation and rollback are disabled for this launch.")
                    confirmation = None if blocks else operator_confirmation(_store, proposal)
                    receipt = operator_receipt(_store, _world)
                finally:
                    _store._conn.rollback()
        except Exception:
            return refused("The installation preview could not be read. No installation was attempted.", "error")
        if action == "install":
            if confirmation is None or token != confirmation:
                return refused("The preview is stale or installation is blocked. Refresh and review it again.",
                               "conflict")
            return install_catalog_residents(catalog, reader, proposal, confirmation=confirmation)
        if receipt and (not enabled or not _world.paused) and receipt["rollback_allowed"]:
            receipt.update(rollback_allowed=False,
                           rollback_block="Pause Town and use an authorized operator launch to roll back.")
        names = {row["id"]: row["name"] for row in view["roster"]["characters"]}
        return {
            "ok": True, "catalog_revision": view["catalog_revision"],
            "world_snapshot_id": view["world_snapshot_id"], "blocks": blocks,
            "confirmation": confirmation, "receipt": receipt, "capacity": view["town"]["capacity"],
            "verified_links": [{"resident": row["resident_id"], "character": row["character_id"],
                                "name": names[row["character_id"]]}
                               for row in proposal["existing_links"]],
            "placements": [{**row, "name": names[row["character_id"]]} for row in proposal["new_residents"]],
            "unplaced": [{"character_id": cid, "name": names[cid]} for cid in proposal["unplaced_character_ids"]],
            "unresolved": [{"id": row["source_resident_id"], "name": row["source_state"]["name"]}
                           for row in view["town"]["legacy_residents"] if row["resolution"] == "unresolved"],
            "conflicts": [{"resident": row["resident_id"], "character": row["character_id"], "reason": row["reason"]}
                          for row in view["roster"]["mapping_issues"]],
            "invalid_cells": view["town"]["source_issues"],
            "alias_conflicts": sum(row["kind"] == "alias" for row in view["roster"]["unresolved"]),
        }


def capture_identity_world():
    """Detach the loaded world under its owner lock, without starting or saving it."""
    from copy import deepcopy

    with _lock:
        return deepcopy(_world)


def resident_readiness(characters, identity):
    """Read link and scheduler metadata from the loaded world without opening a store."""
    from .world.identity_reader import ResidentLink, _resolve_links

    with _lock:
        controller = _background_life
        attached = controller is not None and controller.identity is identity
        result = {"state": "unavailable", "scheduler_attached": attached,
                  "life_budget_enabled": controller.enabled if attached else None,
                  "life_budget_observed_at": controller.budget_observed_at if attached else None}
        if _world is None:
            return result, set()
        residents = {row.id for row in _world.characters}
        links = [ResidentLink(**row) for row in _world.identity_links]
        verified, unresolved = _resolve_links(links, "town", characters, residents)
        if len(residents) != len(_world.characters):
            verified, unresolved = {}, links
        result.update(state="loaded", paused=bool(_world.paused),
                      ticking=bool(_thread and _thread.is_alive()),
                      verified_links=len(verified), unresolved_link_records=len(unresolved),
                      unlinked_residents=len(_world.characters) - len(verified))
        return result, set(verified)


def capture_town_view():
    """Read the loaded simulation and its committed journal at one boundary."""
    with _lock:
        world = capture_identity_world()
        journal = list(reversed(_store.events(limit=60))) if world is not None and _store is not None else None
        return world, journal, bool(_thread and _thread.is_alive())


def town_transport(action):
    """Use existing world controls without the legacy lazy startup path."""
    if action not in ("play", "pause", "step", "save", "load"):
        return {"ok": False, "reason": "Unsupported Town control."}
    with _lock:
        if _world is None or _store is None:
            return {"ok": False, "reason": "Town simulation is not loaded."}
        return command(action)


def world_snapshot() -> dict:
    """JSON-safe world snapshot. Honest unavailable state if the world is down."""
    try:
        with _lock:
            world, _roster = _load_world()
            payload = snapshot(world)
            payload["last_events"] = list(reversed(_store.events(limit=40)))
        payload["running"] = True
        payload["db"] = db_path()
        return payload
    except Exception as error:
        return {"running": False, "error": "%s: %s" % (type(error).__name__, error),
                "characters": [], "locations": [], "last_events": [],
                "clock": 0, "tick": 0, "speed": 1.0, "paused": True,
                "stats": {"ticks": 0, "planner_calls": 0,
                          "model_budget_left": 0, "characters": 0}}


def roster_snapshot(*, artifact_reader=None) -> dict:
    try:
        _world, roster = _load_world()
        with roster._lock:
            payload = roster.snapshot()
            if artifact_reader is not None:
                payload["artifact_shelf"] = artifact_reader()
        payload["running"] = True
        return payload
    except Exception as error:
        return {"running": False, "error": "%s: %s" % (type(error).__name__, error),
                "agents": [], "tasks": [], "edges": []}


def command(cmd: str, args=None) -> dict:
    """World transport/control. Every unknown command returns ok=False."""
    global _preserve_startup_pause
    args = args or {}
    try:
        world, _roster = _load_world()
    except Exception as error:
        return {"ok": False, "reason": "%s: %s" % (type(error).__name__, error)}
    cmd = str(cmd or "").strip().lower()
    try:
        with _lock:
            world, _roster = _load_world()
            if cmd == "pause":
                world.paused = True
                invalidate_background_life()
                _emit("world_control", {"cmd": cmd})
                return {"ok": True, "world": world_snapshot()}
            if cmd == "play":
                world.paused = False
                _preserve_startup_pause = False
                _emit("world_control", {"cmd": cmd})
                return {"ok": True, "world": world_snapshot()}
            if cmd == "step":
                invalidate_background_life()
                result = _advance_world(world, force=True, pause_after=True)
                if not result["ok"]:
                    return result
                _emit("world_control", {"cmd": cmd, "tick": world.tick})
                return {"ok": True, "world": world_snapshot()}
            if cmd == "speed":
                value = float(args.get("speed", 1.0))
                if value <= 0 or value > 600:
                    return {"ok": False, "reason": "speed must be between 0 and 600"}
                world.speed = value
                _emit("world_control", {"cmd": cmd, "speed": value})
                return {"ok": True, "world": world_snapshot()}
            if cmd == "save":
                result = _save_world(world)
                if result.get("ok"):
                    _emit("world_saved", {"tick": world.tick})
                return result if isinstance(result, dict) else {"ok": False,
                                                                "reason": "save failed"}
            if cmd == "checkpoint":
                result = _store.checkpoint(world)
                if result.get("ok"):
                    _emit("world_checkpoint", {"tick": world.tick})
                return result if isinstance(result, dict) else {"ok": False,
                                                                "reason": "checkpoint failed"}
            if cmd == "load":
                if world.pending_events:
                    result = _save_world(world)
                    if not result["ok"]:
                        return result
                reloaded = _reload_world()               # fresh read from sqlite
                _emit("world_loaded", {"tick": reloaded.tick})
                return {"ok": True, "world": world_snapshot()}
            if cmd == "replay":
                from_tick = int(args.get("from_tick", 0) or 0)
                rows = _store.replay(from_tick=from_tick)
                return {"ok": True, "events": rows[:500],
                        "checkpoints": _store.list_checkpoints()}
            if cmd == "inspect":
                return {"ok": True, "world": world_snapshot(),
                        "roster": roster_snapshot()}
            if cmd == "status":
                return {"ok": True, "world": world_snapshot()}
            if cmd == "prune":
                # Bounded retention for the LAB's own journal (see store.maintenance
                # for the exact tables). Personal memory is a different file.
                result = _store.maintenance() if _store is not None else {}
                _emit("world_pruned", result)
                return {"ok": True, "maintenance": result}
        if cmd == "execute":
            # Manual/again trigger for a specific task (the HUD's assign path
            # already auto-executes; this is for resume and retry).
            task_id = str(args.get("task_id") or "")
            if not task_id:
                return {"ok": False, "reason": "execute needs a task_id"}
            return execute_task(task_id, background=bool(args.get("background", True)),
                                retry=bool(args.get("retry", False)))
        if cmd == "cancel_execution":
            expected_attempt = args.get("attempt")
            if expected_attempt is not None and not args.get("task_id"):
                return {"ok": False, "cancelled": False, "reason": "An attempt requires a task."}
            return cancel_execution(str(args.get("reason") or "ui"), task_id=args.get("task_id"),
                                    expected_attempt=expected_attempt)
        return {"ok": False, "reason": "unknown command %r" % cmd}
    except Exception as error:
        return {"ok": False, "reason": "%s: %s" % (type(error).__name__, error)}


def assign_task(agent_id, text, budget=None, *, auto_execute=True,
                approval_required=False) -> dict:
    """Assign a goal to an agent, then (by default) actually EXECUTE it.

    The executor runs in a background daemon thread so the UI never blocks on a
    model call; pass auto_execute=False to leave it purely as a state change.
    `approval_required=True` parks the result at `awaiting_review` so a human
    must approve it - that is the reviewable end of the assignment flow.
    """
    try:
        _world, roster = _load_world()
        goal = str(text or "").strip()
        if not goal:
            return {"ok": False, "reason": "an assignment needs an explicit goal"}
        assigned = roster.create_assignment(goal, agent_id, budget=budget,
                                             approval_required=bool(approval_required))
        if not assigned.get("ok"):
            return assigned
        _emit("task_assigned", {"task": assigned["task"].get("id"),
                                "agent": str(agent_id)})
        result = dict(assigned)
        if auto_execute:
            started = execute_task(assigned["task"]["id"], background=True)
            result["execution"] = started
            result["task"] = roster.task(assigned["task"]["id"])
        return result
    except Exception as error:
        return {"ok": False, "reason": "%s: %s" % (type(error).__name__, error)}


def review_task(task_id, verdict, attempt=None) -> dict:
    try:
        _world, roster = _load_world()
        result = roster.review(task_id, verdict, attempt=attempt)
        if result.get("ok"):
            _emit("task_reviewed", {"task": str(task_id),
                                    "verdict": str(verdict)})
        return result
    except Exception as error:
        return {"ok": False, "reason": "%s: %s" % (type(error).__name__, error)}


def events(limit=100) -> dict:
    try:
        return {"events": list(_events)[:max(1, min(int(limit), EVENT_RING))]}
    except Exception:
        return {"events": []}


def note(kind, data=None):
    """Public hook so other modules can push a UI event into the same ring.

    ROUND LAB-2A: task serving facts (which provider/model actually ran a
    worker task, including failures/retries) are persisted through the lab
    authority so 'who served this task' survives a restart - the executor's
    stats used to be in-memory only.

    ROUND LAB-2C: the same events are the USAGE producer for the durable
    background budget (dedup-safe attempt key from task+kind+attempt). One
    event = ONE attempt row; a restart or a repeated note can never double-
    count. Best-effort: an unavailable authority/budget must never break the
    event path.

    """
    payload = data if isinstance(data, dict) else {}
    _emit(str(kind or "note"), payload)
    try:
        task_id = str(payload.get("task") or "")
        if task_id and str(kind) in ("task_started", "task_completed",
                                     "task_failed", "task_retry",
                                     "task_paused", "task_cancel"):
            # A queued execution carries its OWN bucket (`task_note_bucket`)
            # from the admission; an immediate-start writes it into a module
            # map so every downstream event re-reads it
            # ROUND LAB-3R3: the authoritative category is the ROSTER'S own
            # persisted `category` column, not the process-local `_task_buckets`
            # map. Fall back to `_task_buckets` for an in-flight run that hasn't
            # been re-loaded from disk yet.
            try:
                _world_ref, roster_entry = _load_world()
                _roster_ref = roster_entry
                roster_task = _roster_ref.task(task_id) if _roster_ref else {}
                _authoritative = str(roster_task.get("category") or "") or \
                    _task_buckets.get(task_id, "user_work")
            except Exception:
                _authoritative = _task_buckets.get(task_id, "user_work")
            payload.setdefault("bucket", _authoritative)
            try:
                from . import lab_authority as _auth
                authority = _auth.shared()
                if authority is not None:
                    authority.record_task_serving(str(kind), payload)
            except Exception:
                pass
            try:
                from . import lab_budget as _budget
                budget = _budget.shared()
                if budget is not None:
                    budget.record_from_note(str(kind), payload)
            except Exception:
                pass
            # Round LAB-3: the character experience recorder uses the real
            # authority's life_override (the persistent per-character mode),
            # NOT a hardcoded "npc" string. This is the critical NPC-zero-
            # memory rule: an NPC-powered character records zero AI
            # autobiographical memory.
            try:
                from . import lab_memory as _lm
                _memory = _lm.shared()
                if _memory is not None and str(kind) == "task_completed" \
                        and _task_buckets.get(task_id, "user_work") == "life":
                    char_note = str(payload.get("agent") or "")
                    if char_note:
                        from . import lab_authority as _la
                        authority = _la.shared()
                        effective_mode = authority.life_override(char_note) \
                            if authority else ""
                        if effective_mode not in ("npc", ""):
                            _memory.record_experience(
                                char_note, tick=0,
                                kind="task_completed",
                                text=str(payload.get(
                                    "text") or "")[:200])
            except Exception:
                pass
    except Exception:
        pass


def health() -> dict:
    return {"running": _thread is not None and _thread.is_alive(),
            "started": _started, "start_error": _start_error,
            "presence": presence(), "db": db_path()}


# ---------------------------------------------------------------------------
# Presence + bounded deterministic catch-up  (ROUND LAB-2C)
#
# "The assistant is available even when the town is closed" â€” so presence is a
# *background-life* switch only: it never touches duty, voice, or the router.
# Closing the town/HUD pauses the tick loop (life reduced away, zero cost);
# opening it runs a BOUNDED, deterministic, zero-model-call catch-up so there
# are no token-spending replays and no fake NPC back-conversations.
# ---------------------------------------------------------------------------
_PRESENCE_KEY = "lab_presence"
_PRESENCE_STATES = ("open", "away", "closed")
_CATCHUP_TICKS_DEFAULT = 60         # ~97% of a 1-minute offline window
_PRESENCE_LOCK_CAPPING = _presence_capped = None


def set_presence(state: str, *, visibility=False) -> dict:
    """Set the app/town presence from REAL lifecycle events (tray open/close,
    HUD visibility). Persisted in the world store so a restart keeps it."""
    global _preserve_startup_pause
    state = str(state or "").strip().lower()
    if state not in _PRESENCE_STATES:
        return {"ok": False, "reason": "presence must be one of %s"
                % (_PRESENCE_STATES,)}
    with _lock:
        world, _roster_ref = _load_world()
        previous = presence()
        if visibility and _preserve_startup_pause:
            return {"ok": True, "previous": previous, "state": previous,
                    "note": "saved installation pause respected"}
        if state == "open":
            _preserve_startup_pause = False
        _set_presence_meta(state)
        if state != "open":
            invalidate_background_life()
        if state == "closed":
            # A closed town does NOT mean "Exit FRIDAY" (the tray still runs the
            # assistant); life is just paused with zero model calls.
            world.paused = True
            _save_world(world)
        elif state == "away":
            world.paused = True
            _save_world(world)
        else:                                    # open
            world.paused = False
            _save_world(world)
        _emit("presence", {"state": state, "previous": previous})
        return {"ok": True, "previous": previous, "state": state}


def presence() -> str:
    try:
        value = _store.meta_get(_PRESENCE_KEY, '{"state":"open"}') \
            if _store is not None else '{"state":"open"}'
        data = json.loads(value or "{}")
        state = str((data or {}).get("state") or "open")
        return state if state in _PRESENCE_STATES else "open"
    except Exception:
        return "open"


def _set_presence_meta(state: str) -> None:
    try:
        store = _store
        if store is None:
            return
        store.meta_set(_PRESENCE_KEY, json.dumps(
            {"state": state, "at": time.time()}))
    except Exception:
        pass


def catch_up(max_ticks: int = None) -> dict:
    """Deterministic, ZERO-model-call catch-up after a hidden/closed window.

    Bounded (default 60 ticks): no token spending, no NPC autobiographical AI
    memory, no replayed conversations. Movement is the same `world_tick` the
    live loop uses; everything persists and is replay-safe via the ledger.
    """
    cap = int(max_ticks if max_ticks is not None else _CATCHUP_TICKS_DEFAULT)
    cap = max(0, min(1440, cap))            # one day's worth maximum
    with _lock:
        world, _roster_ref = _load_world()
        invalidate_background_life()
        before_tick = int(world.tick)
        ran = 0
        result = _save_world(world) if world.pending_events else {"ok": True}
        while result["ok"] and ran < cap:
            _advance_world(world, force=True, persist=False)
            ran += 1
            if ran % SAVE_EVERY_TICKS == 0:
                result = _save_world(world)
        if result["ok"] and ran % SAVE_EVERY_TICKS:
            result = _save_world(world)
        return {**result, "ticks": ran, "from_tick": before_tick,
                "to_tick": int(world.tick), "model_calls": 0, "presence": presence()}
