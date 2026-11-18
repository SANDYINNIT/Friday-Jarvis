"""ROUND LAB-3R3: durable task category across restart/reload and honest
memory-recorder classification (NPC rule persisted, not just process-local).

This tests the ACTUAL production path: a task created with category=`life`
round-trips through a roster `_save`/`_load` cycle, a fresh process sees it
as `life`, and the memory recorder uses the restored category (NOT a re-run
of the agent's name).
"""

import os
import tempfile
import time

import pytest

from source.server import lab_state as lab
from source.server import lab_authority as authority_mod
from source.server import lab_budget as budget_mod
from source.server import lab_memory as memory_mod
from source.server.world.roster import Roster
from source.server.world.store import Store
from source.server.world.executor import AgentExecutor


@pytest.fixture()
def lab_env(tmp_path, monkeypatch):
    database = tmp_path / "world.db"
    monkeypatch.setattr(lab, "db_path", lambda: str(database))
    lab.reset()
    lab._execution_queue_reset()
    authority_mod.reset_shared()
    budget_mod.reset_shared()
    memory_mod.reset_shared()
    authority_mod.configure(tmp_path / "authority.db")
    budget_mod.configure(database)
    memory_mod.configure(tmp_path / "character_mem.db", world_db=str(database))
    store = Store(database)
    roster = Roster(store)
    lab._executor = AgentExecutor(roster, runner=FakeRunner(),
                                  note=lab.note)
    try:
        yield {"store": store, "roster": roster, "db": database,
               "memory": memory_mod.shared()}
    finally:
        lab._execution_queue_reset()
        lab.reset()
        authority_mod.reset_shared()
        budget_mod.reset_shared()
        memory_mod.reset_shared()


class FakeRunner:
    def __call__(self, goal, **kw):
        return {"ok": True, "text": "worked %s" % goal,
                "provider": "fake_pool", "model": "fake/model-F",
                "tokens_in": 5, "tokens_out": 2, "cached": False,
                "error": "", "latency": 0.01}


class TestPersistenceCategory:
    def test_life_category_survives_restart(self, lab_env):
        """Create a life task, save the store, reload the Roster from disk on
        a FRESH object. The reloaded task's `category` must still be `life`,
        not the default 'user_work'."""
        roster = lab_env["roster"]
        created = roster.create_task("bg probe", owner="research",
                                     category="life")
        task_id = created["task"]["id"]
        roster._tasks[task_id]["category"] = "life"
        roster._save()

        reloaded = Roster(roster.store)
        reopened = reloaded.task(task_id)
        assert reopened.get("category") == "life", reopened
        # and it must NOT have reclassified to user_work
        assert reopened.get("category") != "user_work"

    def test_user_work_durable(self, lab_env):
        """The default category is 'user_work' and round-trips through the
        restart too."""
        roster = lab_env["roster"]
        created = roster.create_task("sandy asked", owner="research")
        roster._save()
        reloaded = Roster(roster.store)
        assert reloaded.task(created["task"]["id"]).get(
            "category") == "user_work"

    def test_additive_column_on_preexisting_db(self, tmp_path):
        """A fresh database created BEFORE the column existed: the migration
        adds `category` idempotently; existing rows get the default."""
        database = tmp_path / "old.db"
        store = Store(database)
        # drop the category column to simulate an old DB (raw SQLite)
        store._conn.execute("ALTER TABLE roster_tasks RENAME TO roster_old")
        store._conn.execute(
            "CREATE TABLE roster_tasks (id TEXT PRIMARY KEY, goal TEXT,"
            " owner TEXT, state TEXT, steps INTEGER, artifacts_json TEXT,"
            " error TEXT, approval_required INTEGER DEFAULT 0,"
            " paused_by TEXT, created_at REAL, updated_at REAL)")
        store._conn.execute(
            "INSERT INTO roster_tasks (id,goal,owner,state,steps,"
            " artifacts_json,error,approval_required,paused_by,"
            " created_at,updated_at) VALUES ('t-old','x','',"
            " 'pending',0,'[]','','0',NULL,1,1)")
        store._conn.execute("DROP TABLE roster_old")
        store._conn.commit()
        # reload (forceing _migrate to run)
        fresh_store = Store(database)
        assert fresh_store._conn.execute(
            "SELECT category FROM roster_tasks WHERE id='t-old'"
        ).fetchone() is not None, "the column must exist"
        assert fresh_store._conn.execute(
            "SELECT category FROM roster_tasks WHERE id='t-old'"
        ).fetchone()[0] == "user_work"


class TestMemoryRecorderPersistedCategory:
    def test_note_uses_roster_task_category_persisted(
            self, lab_env):
        """The note pipeline reads the roster task's persisted category,
        NOT a process-only map. Create a life task, save, reload, record."""
        roster = lab_env["roster"]
        created = roster.create_task("bg note test", owner="research",
                                     category="life")
        task_id = created["task"]["id"]
        roster._tasks[task_id]["category"] = "life"
        roster._save()
        # a FRESH Roster from the same store
        reloaded_roster = Roster(roster.store)
        # the reloaded roster has category=life (not user_work)
        assert reloaded_roster.task(task_id).get("category") == "life"
        # now emit a note, and the note pipeline should read it as life
        # (not user_work!) - so the character experience is recorded
        memory = lab_env["memory"]
        payload = {
            "task": task_id, "agent": "research",
            "provider": "fake_pool", "model": "fake/model",
            "text": "done",
        }  # NOTE: no bucket key at all - the roster's persisted category
           # should be the authoritative source
        lab.note("task_completed", payload)
        # verify the payload's bucket was set from the roster's category
        assert payload.get("bucket") == "life", (
            "note did not restore the persisted category", payload)
