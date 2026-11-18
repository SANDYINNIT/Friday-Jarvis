"""Bounded contention regression tests for the world.db SQLITE WRITE WEDGE.

ROOT CAUSE these tests lock in (measured 2026-11-06 on this machine):

`Roster._save` rewrites agents/tasks inside ONE `BEGIN IMMEDIATE` transaction
but wrote the `edges` rows AFTER that transaction closed, one autocommit
statement per row. Every save therefore rewrites EVERY persisted edge as its
own transaction:

* per-row cost measured 6.41 ms autocommit vs 0.06 ms inside a transaction
  (~100x: journal write + fsync + a fresh write-lock acquisition per row);
* one assign/review cycle measured 0.63 s at 28 edges and 2.10 s at 102
  edges - linear in edge count (the in-memory cap is 400);
* under the production multi-connection topology (lab_authority + lab_budget
  singletons on the SAME world.db) single edge INSERTs stalled 5.5 s at the
  busy timeout, one HUD cycle took 27.9 s, and only 7 of 60 cycles finished
  inside a 20 s window.

The second connection of the blocking pair is lab_authority's legacy-mode
connection (isolation_level unset): its first busy failure leaves an OPEN
transaction (`in_transaction=True` after the failed INSERT - verified
separately), whose next SELECT then holds SHARED and blocks every other
connection's COMMIT until that stale transaction finally commits. The fix
(edges inside the existing transaction) cuts lock acquisitions per save from
1+N to 1, so the collision that seeds that stale transaction essentially
stops happening, and restores the atomicity `Roster._save` documents.

Tests (bounded, explicit temp fixture paths, never production state):

1. test_failed_save_rolls_back_the_whole_rewrite - deterministic atomicity:
   an edges INSERT failing mid-rewrite must leave the PREVIOUS agents, tasks
   and edges on disk untouched (the guarantee `_save` documents).
2. test_hud_loops_finish_under_multi_connection_contention - the bounded
   contention repro: HUD-shaped assign/review cycles racing lab_authority
   and lab_budget singletons plus a deterministic lock-holder connection.
   Fails pre-fix on the cycle deadline; also bounds any single statement
   stall. Records PRAGMA journal_mode/locking_mode and the slowest SQL.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time

# Explicit fixture path BEFORE any source.server import (project convention).
_FIXTURE_DIR = tempfile.mkdtemp(prefix="friday-store-contention-")
os.environ.setdefault("FRIDAY_WORLD_DB", os.path.join(_FIXTURE_DIR, "world.db"))
sys.path.insert(0, os.getcwd())

import pytest

from source.server.world.store import Store
from source.server.world.roster import Roster
from source.server import lab_authority, lab_budget


@pytest.fixture()
def fixture_db(tmp_path, monkeypatch):
    path = str(tmp_path / "world.db")
    monkeypatch.setenv("FRIDAY_WORLD_DB", path)
    yield path
    lab_authority.reset_shared()
    lab_budget.reset_shared()


def _seed(db_path, edges_target=20):
    """Small roster + enough edges that the per-row churn is exercised."""
    store = Store(db_path)
    roster = Roster(store)
    for i in range(3):
        roster.add_agent("agent%d" % i, "Agent%d" % i, "worker")
    made = 0
    while len(roster._edges) < edges_target and made < edges_target:
        made += 1
        created = roster.create_task("seed %d" % made, owner="agent0")
        if not created.get("ok"):
            break
        roster.assign(created["task"]["id"], "agent0")
        roster.review(created["task"]["id"], "approve")
    return store, roster


def _dump(store):
    """On-disk roster state, comparable across a failed save."""
    conn = store._conn
    tasks = sorted((row["id"], row["goal"], row["state"])
                   for row in conn.execute(
                       "SELECT id,goal,state FROM roster_tasks"))
    edges = sorted((row["src"], row["dst"], row["kind"], row["message"])
                   for row in conn.execute(
                       "SELECT src,dst,kind,message FROM edges"))
    agents = sorted((row["id"], row["state"])
                    for row in conn.execute("SELECT id,state FROM agents"))
    return {"tasks": tasks, "edges": edges, "agents": agents}


def test_failed_save_rolls_back_the_whole_rewrite(fixture_db):
    """A failed rewrite must not destroy existing roster data.

    The poison trigger aborts the FIRST edges row of the rewrite. Before the
    fix the agents/tasks rewrite had already committed outside the loop's
    reach (edges sat after the transaction), so the old tasks were replaced
    and the old edges were deleted with nothing to re-insert them - exactly
    the half-saved damage `_save`'s docstring promises cannot happen.
    """
    store, roster = _seed(fixture_db, edges_target=6)
    before = _dump(store)
    assert before["tasks"] and before["edges"], "fixture must have state"

    store._conn.execute(
        "CREATE TRIGGER poison_edges BEFORE INSERT ON edges "
        "WHEN NEW.message = 'POISON' BEGIN "
        "SELECT RAISE(ABORT, 'injected edges failure'); END")

    # In-memory mutation + a poisoned edge FIRST in the rewrite order.
    first_id = next(iter(roster._tasks))
    roster._tasks[first_id]["goal"] = "MUTATED-GOAL"
    roster._edges.insert(0, {"from": "agent0", "to": "FRIDAY",
                             "kind": "review", "state": "done",
                             "at": time.time(), "message": "POISON"})

    result = roster._save()
    assert result["ok"] is False, result
    assert "injected edges failure" in str(result.get("reason", "")), result

    after = _dump(store)
    assert after == before, (
        "failed _save changed on-disk roster state: tasks %s -> %s, "
        "edges %d -> %d"
        % ([t for t in before["tasks"] if t not in after["tasks"]],
           [t for t in after["tasks"] if t not in before["tasks"]],
           len(before["edges"]), len(after["edges"])))
    store.close()


def test_hud_loops_finish_under_multi_connection_contention(fixture_db,
                                                            monkeypatch):
    """Bounded contention repro: HUD cycles vs the production topology.

    Rivals on the SAME world.db, exactly as production runs them:
    lab_authority.configure + lab_budget.configure singletons (separate
    connections) doing small writes, plus a deterministic lock-holder
    connection modelling candidate (b) - a second connection holding the
    write lock. The HUD thread runs assign/review cycles (each a full
    roster._save rewrite) until CYCLES_REQUIRED finish or DEADLINE passes.

    Pre-fix the per-row autocommit edge loop cannot finish in time; post-fix
    each save takes the write lock once and the run completes comfortably.
    """
    store, roster = _seed(fixture_db, edges_target=20)
    assert len(roster._edges) >= 20

    journal = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    locking = store._conn.execute("PRAGMA locking_mode").fetchone()[0]
    assert journal in ("delete", "truncate", "persist"), journal
    assert locking == "normal", locking

    # -- wall-time instrumentation around _exec_tx (the observed wedge site)
    samples = []
    samples_lock = threading.Lock()
    orig_exec_tx = Store._exec_tx

    def timed_exec_tx(self, sql, params=()):
        t0 = time.perf_counter()
        try:
            return orig_exec_tx(self, sql, params)
        finally:
            dt = time.perf_counter() - t0
            if dt >= 0.05:
                with samples_lock:
                    samples.append((dt, sql.strip()[:90]))

    monkeypatch.setattr(Store, "_exec_tx", timed_exec_tx)

    # -- SQL attribution for whatever was in flight around a stall
    trace = []

    def _trace(sql):
        trace.append(sql.strip()[:90])
        del trace[:-60]

    store._conn.set_trace_callback(_trace)

    stop = threading.Event()
    rival_errors = []
    holder_errors = []

    def authority_writer():
        auth = lab_authority.configure(fixture_db)
        i = 0
        while not stop.is_set():
            i += 1
            try:
                auth.bump("contention", "e%d" % i)
            except Exception as error:
                rival_errors.append("%s: %s" % (type(error).__name__, error))
            time.sleep(0.005)

    def budget_writer():
        bud = lab_budget.configure(fixture_db)
        i = 0
        while not stop.is_set():
            i += 1
            try:
                bud.record("life", task_id="t%d" % i,
                           attempt="contention-%d" % i,
                           category="admission", ok=True,
                           provider="p", model="m")
            except Exception as error:
                rival_errors.append("%s: %s" % (type(error).__name__, error))
            time.sleep(0.005)

    def lock_holder():
        """Deterministic write-lock pressure from a second connection."""
        conn = sqlite3.connect(fixture_db, timeout=5.0, isolation_level=None)
        i = 0
        try:
            while not stop.is_set():
                i += 1
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        ("holder", str(i)))
                    time.sleep(0.04)
                    conn.execute("COMMIT")
                except Exception as error:
                    holder_errors.append("%s: %s" % (type(error).__name__,
                                                     error))
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                time.sleep(0.002)
        finally:
            conn.close()

    threads = [threading.Thread(target=authority_writer, daemon=True),
               threading.Thread(target=budget_writer, daemon=True),
               threading.Thread(target=lock_holder, daemon=True)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)

    cycles_required = 25
    deadline_seconds = 18.0
    completed = 0
    t_start = time.perf_counter()
    deadline = t_start + deadline_seconds
    try:
        while completed < cycles_required and time.perf_counter() < deadline:
            created = roster.create_task("hud %d" % completed,
                                         owner="agent0")
            assert created.get("ok"), created
            assigned = roster.assign(created["task"]["id"], "agent0")
            assert assigned.get("ok"), assigned
            reviewed = roster.review(created["task"]["id"], "approve")
            assert reviewed.get("ok"), reviewed
            completed += 1
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5.0)
        store._conn.set_trace_callback(None)
    wall = time.perf_counter() - t_start

    with samples_lock:
        slow = sorted(samples, reverse=True)
    slowest = "%.2fs %s" % slow[0] if slow else "none"
    diag = ("journal=%s locking=%s edges=%d cycles=%d/%d in %.1fs | "
            "slowest statements: %s | rival lock errors=%d "
            "(sample %s) | holder errors=%d | recent SQL: %s"
            % (journal, locking, len(roster._edges), completed,
               cycles_required, wall,
               ["%.2fs %s" % (d, s) for d, s in slow[:5]],
               len(rival_errors), rival_errors[:2], len(holder_errors),
               trace[-3:]))
    assert completed >= cycles_required, (
        "SQLITE WRITE WEDGE: only %d/%d HUD assign/review cycles finished "
        "in %.1fs (deadline %.0fs) - %s"
        % (completed, cycles_required, wall, deadline_seconds, diag))
    assert not slow or slow[0][0] < 2.5, (
        "a single statement stalled %.2fs inside conn.execute (lock wait at "
        "store._exec_tx) - %s" % (slow[0][0], diag))
    store.close()
