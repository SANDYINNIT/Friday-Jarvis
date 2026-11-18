"""Agent roster + real task lifecycle (Future Lab, ROUND ZB-WORLD).

This is the *operational* half of the lab: agents, bounded subtask requests,
review/consolidation, budgets and ownership. It is deliberately independent of
character personas — a persona resting in the world must never be able to drop
a real task, so `pause_for_rest()` parks it explicitly instead.

Verdict vocabulary is `approve` / `reject` (that is what the UI sends) and maps
to the states `approved` / `rejected`.
"""

from __future__ import annotations

import json
import hashlib
import threading
import time
import uuid
from contextlib import nullcontext
from copy import deepcopy
from functools import wraps
from typing import Optional

from .store import Store

AGENT_STATES = ("idle", "working", "blocked", "awaiting_review", "done", "error")
# `queued` (ROUND LAB-2A) means: admitted to the execution queue, not yet owned
# by a worker thread. Only `running` means a worker actually holds it - the old
# code set state=running at ASSIGN time, which is how a second assignment could
# leave the first stranded as "falsely running" with no worker at all.
TASK_STATES = ("pending", "assigned", "queued", "running", "awaiting_review",
               "approved", "rejected", "failed", "paused")
# ROUND LAB-3R3: the category column is durable (matches the DDL's additive
# column in store.py and roster's own INSERT / SELECT).
#
# States for which an execution may be admitted (executor). `paused` stays
# deliberately OUT: resuming parked work is always an explicit human action.
ADMISSIBLE_STATES = ("pending", "assigned")
_ATTEMPT_KEY = "lab_task_attempts:"
_ORIGIN_KEY = "lab_task_origin:"
_REVISION_KEY = "lab_roster_revision"


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:8])


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


class _Refused(Exception):
    pass


def _persisted(method):
    """Publish a roster mutation only after its database transaction commits."""
    @wraps(method)
    def apply(self, *args, **kwargs):
        with self._lock:
            before = deepcopy((self._agents, self._tasks, self._edges, self._revision))
            try:
                with self.store.transaction() if self.store is not None else nullcontext():
                    if self.store is not None and self._revision != self._stored_revision():
                        raise _Refused({"ok": False, "reason": "roster changed on disk; reopen before writing"})
                    result = method(self, *args, **kwargs)
                    if not result.get("ok"):
                        raise _Refused(result)
                    if not result.get("dedup"):
                        saved = self._save()
                        if not saved.get("ok"):
                            raise _Refused(saved)
                return deepcopy(result)
            except Exception as error:
                self._agents, self._tasks, self._edges, self._revision = before
                if isinstance(error, _Refused):
                    return error.args[0]
                return {"ok": False, "reason": "task persistence failed: %s" % type(error).__name__}
    return apply


class Roster:
    """Agents, tasks and handoff edges. Never raises on bad input."""

    def __init__(self, store: Optional[Store] = None):
        self.store = store
        self._agents: dict = {}
        self._tasks: dict = {}
        self._edges: list = []
        self._seq = 0
        self._lock = threading.RLock()
        self._revision = 0
        if store is not None:
            with store.transaction():
                self._revision = self._stored_revision()
                loaded = self._load()
                if not loaded.get("ok"):
                    raise RuntimeError(loaded["reason"])

    # -- storage ------------------------------------------------------------
    def _stored_revision(self):
        row = self.store._conn.execute("SELECT value FROM meta WHERE key=?", (_REVISION_KEY,)).fetchone()
        return int(row[0]) if row is not None else 0

    def _load(self):
        reconciled = 0
        recovered = False
        try:
            for row in self.store._conn.execute("SELECT * FROM agents"):
                self._agents[row["id"]] = {
                    "id": row["id"], "name": row["name"], "role": row["role"],
                    "model": row["model"], "state": row["state"],
                    "task_id": row["task_id"],
                    "permissions": json.loads(row["permissions_json"] or "[]"),
                    "budget": json.loads(row["budget_json"] or "{}"),
                    "last_error": row["last_error"],
                    "created_at": row["created_at"], "updated_at": row["updated_at"],
                }
            attempts = {key[len(_ATTEMPT_KEY):]: json.loads(value)
                        for key, value in self.store._conn.execute(
                            "SELECT key,value FROM meta WHERE key GLOB ?",
                            (_ATTEMPT_KEY + "*",))}
            origins = {key[len(_ORIGIN_KEY):]: value for key, value in self.store._conn.execute(
                "SELECT key,value FROM meta WHERE key GLOB ?", (_ORIGIN_KEY + "*",))}
            for row in self.store._conn.execute("SELECT * FROM roster_tasks"):
                state = row["state"]
                paused_by = row["paused_by"]
                # RESTART RECOVERY: an execution that was in flight when the
                # process died cannot be resumed blindly - re-running it would
                # duplicate whatever the provider already produced. Park it, so
                # `resume()` is an explicit human/UI decision instead.
                if state == "running":
                    state = "paused"
                    paused_by = paused_by or "interrupted by restart"
                    recovered = True
                elif state == "queued":
                    # A task that was queued but never started died with the
                    # process: there is no queue left to hand it to, so park it
                    # with the same explicit-recovery rule (never auto-replay).
                    state = "paused"
                    paused_by = paused_by or "queued work interrupted by restart"
                    recovered = True
                history = attempts.get(row["id"], [])
                if state == "paused" and history and history[-1]["state"] in ("running", "queued"):
                    history[-1].update(state="interrupted", reason=paused_by, updated_at=_now())
                self._tasks[row["id"]] = {
                    "id": row["id"], "goal": row["goal"], "owner": row["owner"],
                    "original_owner": origins.get(row["id"]),
                    "state": state, "steps": int(row["steps"] or 0),
                    "artifacts": json.loads(row["artifacts_json"] or "[]"),
                    "error": row["error"], "paused_by": paused_by,
                    "approval_required": bool(row["approval_required"]),
                    "category": str(row["category"] or "user_work"),
                    "attempt": history[-1].get("id") if history else None,
                    "attempts": history,
                    "created_at": row["created_at"], "updated_at": row["updated_at"],
                }
            # AGENT RECONCILIATION: the old loader read agent rows verbatim, so a
            # crash could leave an agent stuck "working" on a task that came
            # back paused/awaiting_review/gone. Now the agent state is derived
            # from the RECOVERED task state, never silently trusted.
            for agent in self._agents.values():
                if agent.get("state") != "working":
                    continue
                task = self._tasks.get(agent.get("task_id") or "")
                if task is None:
                    agent["state"] = "idle"
                    agent["task_id"] = None
                    agent["updated_at"] = _now()
                    reconciled += 1
                elif task["state"] != "running":
                    # The task survived, but no worker owns it; the agent is
                    # 'blocked' on parked work, not 'working'.
                    agent["state"] = ("awaiting_review"
                                      if task["state"] == "awaiting_review"
                                      else "blocked")
                    agent["updated_at"] = _now()
                    reconciled += 1
            for row in self.store._conn.execute(
                    "SELECT src,dst,kind,state,at,message FROM edges ORDER BY id"):
                # The table stores src/dst, but every in-memory edge (and the
                # AI Roster graph) uses from/to. `_save()` writes `from`/`to`,
                # so an older build that loaded rows un-renamed round-tripped
                # them to NULL - measured: an edge with both ends None. Rename
                # on load, and skip rows whose BOTH ends were lost: an edge
                # with no source and no target carries no information and the
                # UI cannot draw it anyway.
                src = row["src"] or ""
                dst = row["dst"] or ""
                if not src and not dst:
                    continue
                # LAB-3R6: normalize LEGACY rows through the same resolver
                # the writer uses ("FRIDAY" label -> `friday` id; "?" stays
                # as-is so historical rows are preserved and the renderer
                # counts them honestly instead of silently vanishing them).
                if src and src != "?":
                    src = self._edge_endpoint(src) or src
                if dst and dst != "?":
                    dst = self._edge_endpoint(dst) or dst
                self._edges.append({"from": src, "to": dst,
                                    "kind": row["kind"], "state": row["state"],
                                    "at": row["at"], "message": row["message"]})
        except Exception:
            return {"ok": False, "reason": "roster could not be loaded"}
        if recovered or reconciled:
            saved = self._save()
            if not saved["ok"]:
                return saved
        return {"ok": True, "reconciled_agents": reconciled}

    def _save(self):
        """Persist agents/tasks/edges ATOMICALLY.

        ROUND LAB-2A: this used to be one `_exec` (auto-commit) per statement
        after three unconditional DELETEs, so a crash mid-rewrite could leave
        the tables empty or half-filled and `_load` would silently swallow the
        damage. The whole rewrite is now ONE `BEGIN IMMEDIATE` transaction that
        commits on success and rolls back on any failure, so existing roster
        data always survives a failed save intact.
        """
        if self.store is None:
            return {"ok": True, "reason": "no store"}
        try:
            store = self.store
            with self._lock, store.transaction():
                if self._revision != self._stored_revision():
                    return {"ok": False, "reason": "roster changed on disk; reopen before writing"}
                revision = self._revision + 1
                store._exec_tx("DELETE FROM agents")
                store._exec_tx("DELETE FROM roster_tasks")
                store._exec_tx("DELETE FROM edges")
                for agent in self._agents.values():
                    store._exec_tx(
                        "INSERT INTO agents (id,name,role,model,state,task_id,"
                        "permissions_json,budget_json,last_error,created_at,updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (agent["id"], agent["name"], agent["role"], agent["model"],
                         agent["state"], agent["task_id"],
                         json.dumps(agent.get("permissions") or []),
                         json.dumps(agent.get("budget") or {}),
                         agent.get("last_error"), agent.get("created_at", _now()),
                         agent.get("updated_at", _now())))
                for task in self._tasks.values():
                    store._exec_tx(
                        "INSERT INTO roster_tasks (id,goal,owner,state,steps,"
                        "artifacts_json,error,approval_required,paused_by,"
                        "created_at,updated_at,category) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (task["id"], task["goal"], task["owner"], task["state"],
                         int(task.get("steps") or 0),
                         json.dumps(task.get("artifacts") or []),
                         task.get("error"), 1 if task.get("approval_required") else 0,
                         task.get("paused_by"), task.get("created_at", _now()),
                         task.get("updated_at", _now()),
                         str(task.get("category") or "user_work")))
                    if task.get("attempts"):
                        store._exec_tx(
                            "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)",
                            (_ATTEMPT_KEY + task["id"], json.dumps(task["attempts"])))
                    if task.get("original_owner"):
                        store._exec_tx(
                            "INSERT OR IGNORE INTO meta (key,value) VALUES (?,?)",
                            (_ORIGIN_KEY + task["id"], task["original_owner"]))
                for edge in self._edges:
                    store._exec_tx(
                        "INSERT INTO edges (src,dst,kind,state,at,message)"
                        " VALUES (?,?,?,?,?,?)",
                        (edge.get("from"), edge.get("to"), edge.get("kind"),
                         edge.get("state"), edge.get("at", _now()),
                         edge.get("message")))
                store._exec_tx("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)",
                               (_REVISION_KEY, str(revision)))
            self._revision = revision
            return {"ok": True}
        except Exception as error:
            # The rolled-back transaction left the previous data intact; the
            # in-memory state may be ahead of disk until the next save.
            return {"ok": False, "reason": "%s: %s"
                    % (type(error).__name__, error)}

    # -- agents -------------------------------------------------------------
    @_persisted
    def add_agent(self, agent_id, name, role, model="", permissions=(),
                  budget=None) -> dict:
        if not agent_id or not name:
            return {"ok": False, "reason": "agent id and name are required"}
        record = {
            "id": str(agent_id), "name": str(name), "role": str(role or "worker"),
            "model": str(model or ""), "state": "idle", "task_id": None,
            "permissions": list(permissions or ()),
            "budget": dict(budget or {"max_steps": 12, "max_model_calls": 4}),
            "last_error": "", "created_at": _now(), "updated_at": _now(),
        }
        self._agents[record["id"]] = record
        return {"ok": True, "agent": record}

    def agent(self, agent_id) -> dict:
        with self._lock:
            return deepcopy(self._agents.get(str(agent_id)) or {})

    def agents(self) -> list:
        with self._lock:
            return deepcopy(list(self._agents.values()))

    # -- tasks --------------------------------------------------------------
    @_persisted
    def create_task(self, goal, owner="", approval_required=False,
                    category="user_work") -> dict:
        return self._create_task(goal, owner, approval_required, category)

    def _create_task(self, goal, owner="", approval_required=False,
                     category="user_work") -> dict:
        goal = str(goal or "").strip()
        if not goal:
            return {"ok": False, "reason": "a task needs an explicit goal"}
        task = {
            "id": _new_id("task"), "goal": goal, "owner": str(owner or ""),
            "original_owner": str(owner) if owner else None,
            "state": "pending", "steps": 0, "artifacts": [], "error": "",
            "paused_by": None, "approval_required": bool(approval_required),
            "category": str(category or "user_work"), "attempt": None, "attempts": [],
            "created_at": _now(), "updated_at": _now(),
        }
        self._tasks[task["id"]] = task
        return {"ok": True, "task": _json_safe(task)}

    def task(self, task_id) -> dict:
        with self._lock:
            return deepcopy(self._tasks.get(str(task_id)) or {})

    def tasks(self) -> list:
        with self._lock:
            return deepcopy(list(self._tasks.values()))

    def _owner_count(self, task_id):
        return sum(1 for t in self._tasks.values()
                   if t["id"] == task_id and t.get("owner"))

    @_persisted
    def assign(self, task_id, agent_id, *, budget=None) -> dict:
        return self._assign(task_id, agent_id, budget=budget)

    @_persisted
    def create_assignment(self, goal, agent_id, *, budget=None, approval_required=False):
        created = self._create_task(goal, approval_required=approval_required)
        if not created["ok"]:
            return created
        return self._assign(created["task"]["id"], agent_id, budget=budget)

    def _assign(self, task_id, agent_id, *, budget=None) -> dict:
        task = self._tasks.get(str(task_id))
        agent = self._agents.get(str(agent_id))
        if task is None:
            return {"ok": False, "reason": "unknown task %s" % task_id}
        if agent is None:
            return {"ok": False, "reason": "unknown agent %s" % agent_id}
        if task["state"] not in ADMISSIBLE_STATES:
            return {"ok": False, "reason": "task is already %s" % task["state"]}
        if task["state"] == "assigned" and task.get("owner") not in (None, "", agent["id"]):
            return {"ok": False, "reason": "owned by %s; transfer explicitly first" % task["owner"]}
        task["owner"] = agent["id"]
        task["state"] = "assigned"
        task["updated_at"] = _now()
        if isinstance(budget, dict):
            for key in ("max_steps", "max_model_calls", "max_seconds"):
                if key in budget:
                    try:
                        agent["budget"][key] = max(1, int(budget[key]))
                    except (TypeError, ValueError, OverflowError):
                        pass
        self._add_edge(agent["id"], "FRIDAY", "assign", "active",
                       "assigned: %s" % task["goal"][:80])
        return {"ok": True, "task": _json_safe(task)}

    @_persisted
    def request_subtask(self, from_id, to_id, goal, budget=None, permissions=()):
        """Bounded agent→agent request. Goal + budget are mandatory."""
        goal = str(goal or "").strip()
        budget = budget or {}
        if not goal:
            return {"ok": False, "reason": "subtask needs an explicit goal"}
        try:
            max_steps = int(budget.get("max_steps", 0))
        except Exception:
            max_steps = 0
        if max_steps <= 0:
            return {"ok": False, "reason": "subtask needs a positive max_steps budget"}
        if str(from_id) not in self._agents or str(to_id) not in self._agents:
            return {"ok": False, "reason": "unknown agent in request"}
        created = self._create_task(goal, owner=str(to_id))
        if not created.get("ok"):
            return created
        created["task"]["owner"] = str(to_id)
        created["task"]["original_owner"] = str(from_id)
        created["task"]["state"] = "assigned"
        created["task"]["updated_at"] = _now()
        self._tasks[created["task"]["id"]] = created["task"]
        target = self._agents[str(to_id)]
        target["budget"] = dict(budget)
        if permissions:
            target["permissions"] = list(dict.fromkeys(
                list(target.get("permissions") or []) + list(permissions)))
        self._add_edge(str(from_id), str(to_id), "subtask", "active",
                       goal[:160])
        return {"ok": True, "task": _json_safe(created["task"]),
                "edge": self._edges[-1] if self._edges else None}

    def _update_attempt(self, task, state, **fields):
        task["state"] = state
        task["updated_at"] = _now()
        if task["attempts"]:
            task["attempts"][-1].update(state=state, updated_at=task["updated_at"], **fields)

    def _matches(self, task, attempt, *states):
        return bool(attempt and task and task["attempt"] == attempt and task["state"] in states)

    def owns_attempt(self, task_id, attempt, *states):
        with self._lock:
            if not self._matches(self._tasks.get(str(task_id)), attempt, *states):
                return False
            if self.store is None:
                return True
            with self.store._lock:
                return self._revision == self._stored_revision()

    def _agent_after(self, task, state):
        agent = self._agents.get(task.get("owner"))
        if agent and agent["task_id"] == task["id"]:
            agent.update(state=state, updated_at=_now(),
                         task_id=task["id"] if state == "awaiting_review" else None)
            if state == "error":
                agent["last_error"] = task["error"]

    @_persisted
    def admit(self, task_id, *, category=None, retry=False) -> dict:
        task = self._tasks.get(str(task_id))
        if not task:
            return {"ok": False, "reason": "unknown task %s" % task_id}
        state = task["state"]
        if state not in ADMISSIBLE_STATES and not (retry and state in ("paused", "failed")):
            return {"ok": False, "state": state, "reason": "task is %s, not admissible" % state}
        if not task["attempts"] and task.get("error"):
            # Keep pre-attempt history when explicitly retrying a legacy failure.
            task["attempts"].append({"state": state, "error": task["error"],
                                     "updated_at": task["updated_at"]})
        attempt = uuid.uuid4().hex
        task.update(attempt=attempt, paused_by=None, error="", steps=0)
        if category is not None:
            task["category"] = category
        worker = self._agents.get(task.get("owner")) or {}
        task["attempts"].append({"id": attempt, "created_at": _now(), "worker": {
            "id": task.get("owner"), "name": worker.get("name"), "role": worker.get("role")}})
        self._update_attempt(task, "queued")
        return {"ok": True, "task": task, "attempt": attempt}

    @_persisted
    def review(self, task_id, verdict, *, attempt=None) -> dict:
        verdict = str(verdict or "").strip().lower()
        mapping = {"approve": "approved", "approved": "approved",
                   "reject": "rejected", "rejected": "rejected"}
        if verdict not in mapping:
            return {"ok": False, "reason": "verdict must be approve or reject"}
        task = self._tasks.get(str(task_id))
        if task is None:
            return {"ok": False, "reason": "unknown task %s" % task_id}
        if task["state"] != "awaiting_review" or (attempt and task["attempt"] != attempt):
            return {"ok": False, "reason": "task is %s or its review changed" % task["state"]}
        self._update_attempt(task, mapping[verdict])
        self._agent_after(task, "done" if verdict.startswith("approve") else "idle")
        self._add_edge(task.get("owner") or "?", "FRIDAY",
                       "review", "done", "%s: %s" % (verdict, task["goal"][:80]))
        return {"ok": True, "task": task, "state": task["state"]}

    @_persisted
    def record_execution(self, task_id, attempt, call, candidates):
        task = self._tasks.get(str(task_id))
        if not self._matches(task, attempt, "running"):
            return {"ok": False, "reason": "attempt no longer owns execution evidence"}
        history = task["attempts"][-1].setdefault("execution_calls", [])
        record = {"call": call, "candidates": deepcopy(candidates)}
        if type(call) is not int or call < 1 or call > len(history) + 1:
            return {"ok": False, "reason": "execution observation is out of sequence"}
        if call <= len(history):
            if history[call - 1] == record:
                return {"ok": True, "dedup": True}
            return {"ok": False, "reason": "execution observation already recorded"}
        history.append(record)
        return {"ok": True}

    @_persisted
    def complete(self, task_id, artifact=None, *, attempt=None, writer=None,
                 provider="", model="", steps=0, execution_call=None) -> dict:
        task = self._tasks.get(str(task_id))
        if self._matches(task, attempt, "awaiting_review", "approved"):
            return {"ok": True, "dedup": True, "task": task, "state": task["state"],
                    "artifact_id": task["attempts"][-1].get("artifact_id")}
        if not self._matches(task, attempt, "running"):
            return {"ok": False, "reason": "attempt no longer owns completion"}
        text = str(artifact or "").strip()
        if not text:
            return {"ok": False, "reason": "artifact write failed: empty result"}
        executor = None
        if execution_call is not None:
            history = task["attempts"][-1].get("execution_calls") or []
            candidates = history[-1]["candidates"] if history and history[-1]["call"] == execution_call else []
            if not candidates or candidates[-1].get("outcome") != "returned_result":
                return {"ok": False, "reason": "result executor was not recorded for this call"}
            executor = candidates[-1]

        from .. import lab_artifacts
        try:
            if self.store is not None:
                if writer is None:
                    return {"ok": False, "reason": "artifact write failed: writer unavailable"}
                result = writer.record(task_id=task["id"], attempt=attempt, text=text,
                                       provider=provider, model=model,
                                       connection=self.store._conn)
                if not result.get("ok"):
                    return {"ok": False, "reason": "artifact write failed: " +
                            str(result.get("reason") or "no result")[:180]}
                if result.get("dedup") and (not result.get("readable") or
                        result.get("sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()):
                    return {"ok": False, "reason": "artifact write failed: existing result differs or is unavailable"}
                art_id = result["id"]
            else:
                # The standalone in-memory roster has no file shelf.
                art_id = lab_artifacts.artifact_id(task["id"], attempt)
        except Exception as error:
            return {"ok": False, "reason": "artifact write failed: " + type(error).__name__}

        task["artifacts"].append({"id": art_id, "attempt": attempt, "label": text[:200],
                                  "at": _now(), "file_backed": self.store is not None})
        task["steps"] = steps
        state = "awaiting_review" if task["approval_required"] else "approved"
        self._update_attempt(task, state, artifact_id=art_id, provider=provider, model=model)
        if executor is not None:
            task["attempts"][-1]["result_executor"] = executor
        self._agent_after(task, "awaiting_review" if state == "awaiting_review" else "idle")
        return {"ok": True, "task": task, "state": state, "artifact_id": art_id}

    @_persisted
    def fail(self, task_id, error, *, attempt=None) -> dict:
        task = self._tasks.get(str(task_id))
        if not self._matches(task, attempt, "queued", "running"):
            return {"ok": False, "reason": "attempt no longer owns failure"}
        task["error"] = str(error or "unknown error")[:300]
        self._update_attempt(task, "failed", error=task["error"])
        self._agent_after(task, "error")
        return {"ok": True, "task": task, "state": "failed"}

    def mark_error(self, task_id, reason="model_unavailable", *, attempt=None) -> dict:
        return self.fail(task_id, reason, attempt=attempt)

    def exceeds_budget(self, task_id) -> bool:
        task = self.task(task_id)
        budget = self.agent(task.get("owner") or "").get("budget") or {}
        try:
            max_steps = int(budget.get("max_steps", 0))
        except Exception:
            max_steps = 0
        return max_steps > 0 and int(task.get("steps") or 0) >= max_steps

    @_persisted
    def pause_for_rest(self, task_id, character_id=None, *, attempt=None) -> dict:
        task = self._tasks.get(str(task_id))
        if task is None:
            return {"ok": False, "reason": "unknown task %s" % task_id}
        if attempt is None and task["state"] in ADMISSIBLE_STATES:
            task.update(state="paused", updated_at=_now())
        elif self._matches(task, attempt, "queued", "running"):
            self._update_attempt(task, "paused", reason=str(character_id or "rest"))
        else:
            return {"ok": False, "reason": "task is %s or its attempt changed" % task["state"]}
        task["paused_by"] = str(character_id or task.get("owner") or "")
        self._agent_after(task, "blocked")
        self._add_edge(task.get("owner") or "?", "world", "pause", "paused",
                       "paused; ownership retained")
        return {"ok": True, "task": task, "state": "paused", "note": "paused, not dropped"}

    @_persisted
    def resume(self, task_id) -> dict:
        task = self._tasks.get(str(task_id))
        if task is None or task["state"] != "paused":
            return {"ok": False, "reason": "task is not paused"}
        task.update(state="assigned", paused_by=None, updated_at=_now())
        self._add_edge(task.get("owner") or "?", "world", "resume", "active",
                       "ready for admission")
        return {"ok": True, "task": task}

    @_persisted
    def mark_running(self, task_id, *, attempt=None) -> dict:
        task = self._tasks.get(str(task_id))
        if not self._matches(task, attempt, "queued"):
            return {"ok": False, "reason": "attempt is no longer queued"}
        self._update_attempt(task, "running")
        agent = self._agents.get(task.get("owner"))
        if agent:
            agent.update(state="working", task_id=task["id"], updated_at=_now())
        return {"ok": True, "task": task, "attempt": attempt}

    @_persisted
    def consolidate(self, parent_id, child_id) -> dict:
        """Fold a finished subtask's result into its parent task.

        This is the "consolidate" half of delegation: the child's artifacts are
        appended to the parent (tagged with their source so provenance survives)
        and an edge records the merge. A child that never produced anything is
        refused rather than silently merged as success.
        """
        parent = self._tasks.get(str(parent_id))
        child = self._tasks.get(str(child_id))
        if parent is None or child is None:
            return {"ok": False, "reason": "unknown task in consolidation"}
        if child.get("state") not in ("approved", "awaiting_review", "failed"):
            return {"ok": False,
                    "reason": "subtask is %s; nothing to consolidate"
                              % child.get("state")}
        seen = {(row.get("source_task"), row.get("id"))
                for row in parent.get("artifacts", [])}
        added = 0
        for artifact in (child.get("artifacts") or []):
            entry = dict(artifact)
            entry["source_task"] = child["id"]
            key = (child["id"], entry.get("id"))
            if key in seen:
                continue
            parent.setdefault("artifacts", []).append(entry)
            seen.add(key)
            added += 1
        if not added:
            return {"ok": True, "dedup": True, "parent": parent, "artifacts_added": 0}
        parent["updated_at"] = _now()
        self._add_edge(str(child.get("owner") or "?"),
                       str(parent.get("owner") or "?"),
                       "consolidate", "done",
                       "%s -> %s (%d artifact(s))" % (child["id"], parent["id"],
                                                      added))
        return {"ok": True, "parent": _json_safe(parent),
                "artifacts_added": added}

    # -- edges / reporting --------------------------------------------------
    def _edge_endpoint(self, value):
        """Resolve an edge endpoint to a real agent id or the 'world'
        sentinel; None means 'no drawable relationship'.

        LAB-3R6: edges used to be written with the display label "FRIDAY"
        (the boss NAME, not its id `friday`), the "world" sentinel or "?"
        - while the AI Roster graph keys nodes by agent id, so up to 79
        persisted rows were un-drawable ("reference unknown agents"). New
        writes resolve here; `_load` runs the same resolver over legacy rows.
        """
        text = str(value or "").strip()
        if not text or text == "?":
            return None                      # no actor: nothing to draw
        if text == "world":
            return "world"
        if text in self._agents:
            return text
        lowered = text.lower()
        for agent_id, agent in self._agents.items():
            if str((agent or {}).get("name") or "").lower() == lowered:
                return agent_id              # display label -> real id
        return text

    def _add_edge(self, src, dst, kind, state, message=""):
        frm = self._edge_endpoint(src)
        to = self._edge_endpoint(dst)
        if frm is None or to is None:
            return                           # LAB-3R6: never write a row
        # that references no node ("?" owners, missing boss, ...)
        self._seq += 1
        self._edges.append({"from": str(frm), "to": str(to), "kind": str(kind),
                            "state": str(state), "at": _now(),
                            "message": str(message)[:240]})
        if len(self._edges) > 400:
            del self._edges[:-400]

    def edges(self) -> list:
        with self._lock:
            return deepcopy(self._edges)

    def snapshot(self) -> dict:
        with self._lock:
            return {"agents": self.agents(), "tasks": self.tasks(),
                    "edges": self.edges()}
