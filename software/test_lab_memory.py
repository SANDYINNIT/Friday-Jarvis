"""Tests for source.server.lab_memory — five domains + their controls.

All databases live under pytest's ``tmp_path``.  Nothing here may open or
write FRIDAY's live state under ``~/.friday``; one test proves that by
repointing ``Path.home`` at a scratch directory and asserting it stays empty.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from source.server.lab_memory import (
    ASSISTANT_ACTOR,
    ASSISTANT_COLUMNS,
    LabMemory,
    migrate_assistant_columns,
)
from source.server.memory import MemoryStore
from source.server.world.store import Store


# -- helpers ---------------------------------------------------------------

OLD_MEMORY_COLUMNS = (
    "id", "scope", "kind", "key", "topic", "content", "source",
    "confidence", "created_at", "updated_at", "expires_at",
    "superseded_by", "deleted",
)


def _dump_table(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _raw(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fts_match_hits(db_path, token):
    """Raw FTS MATCH — proves physical index membership, bypassing the API."""
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?",
                (token,),
            ).fetchall()
        ]
    except sqlite3.Error as error:
        return [f"<fts unavailable: {error}>"]
    finally:
        conn.close()


class _SqlProxy:
    """Delegating sqlite3 connection that runs a handler on every statement.

    ``sqlite3.Connection`` is an immutable built-in type, so class-level
    monkeypatching is impossible; wrapping the connection factory is the
    honest way to intercept SQL.  Special methods used by ``with`` are
    explicitly forwarded (``__getattr__`` does not cover implicit lookups).
    """

    def __init__(self, real, handler=None):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_handler", handler)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        if name in ("_real", "_handler"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_real"), name, value)

    def __enter__(self):
        object.__getattribute__(self, "_real").__enter__()
        return self

    def __exit__(self, *exc):
        return object.__getattribute__(self, "_real").__exit__(*exc)

    def _run(self, sql):
        handler = object.__getattribute__(self, "_handler")
        if handler is not None:
            handler(sql)

    def execute(self, sql, *args, **kwargs):
        self._run(sql)
        return object.__getattribute__(self, "_real").execute(
            sql, *args, **kwargs
        )

    def executescript(self, script):
        self._run(script)
        return object.__getattribute__(self, "_real").executescript(script)

    def executemany(self, sql, *args, **kwargs):
        self._run(sql)
        return object.__getattribute__(self, "_real").executemany(
            sql, *args, **kwargs
        )


# -- fixtures --------------------------------------------------------------

@pytest.fixture()
def memory_db(tmp_path):
    return tmp_path / "lab_memory.db"


@pytest.fixture()
def world_db(tmp_path):
    path = tmp_path / "world.db"
    store = Store(path)
    yield path
    store.close()


@pytest.fixture()
def lab(memory_db):
    store = LabMemory(memory_db)
    store.init_schema()
    return store


@pytest.fixture()
def lab_with_world(memory_db, world_db):
    store = LabMemory(memory_db, world_db)
    store.init_schema()
    return store


# -- migration -------------------------------------------------------------

def test_migration_is_additive_and_idempotent(memory_db):
    legacy_store = MemoryStore(memory_db)
    legacy = legacy_store.remember(
        key="legacyfact", topic="", content="legacy content row",
        source="test",
    )
    before = _dump_table(
        memory_db,
        "SELECT " + ", ".join(OLD_MEMORY_COLUMNS)
        + " FROM memories WHERE id = ?",
        (legacy["id"],),
    )
    assert before, "legacy row must exist before migration"

    store = LabMemory(memory_db)
    first = store.init_schema()
    assert sorted(first["added"]) == sorted(ASSISTANT_COLUMNS)
    assert first["failed"] == []
    assert first["missing"] == []
    assert first["index"]["fts5"] is True  # this build has FTS5

    second = store.init_schema()
    assert second["added"] == []           # running twice adds nothing
    third = LabMemory(memory_db).init_schema()
    assert third["added"] == []            # a fresh handle adds nothing
    assert third["failed"] == []

    after = _dump_table(
        memory_db,
        "SELECT " + ", ".join(OLD_MEMORY_COLUMNS)
        + " FROM memories WHERE id = ?",
        (legacy["id"],),
    )
    assert after == before, "no existing row may be modified by the migration"

    full = _raw(memory_db, "SELECT * FROM memories WHERE id = ?",
                (legacy["id"],))[0]
    assert full["pinned"] == 0             # column default on a legacy row
    assert full["version"] == 1
    assert full["epistemic"] is None       # legacy rows read as observed
    assert full["source_event_id"] is None
    assert full["derived_from"] is None


def test_migration_failure_is_recorded_and_degrades(memory_db, monkeypatch):
    MemoryStore(memory_db).remember(
        key="preexisting", content="row written before migration",
        source="test",
    )
    original_connect = sqlite3.connect

    def blocked_connect(*args, **kwargs):
        def handler(sql):
            if str(sql).strip().upper().startswith(
                "ALTER TABLE MEMORIES ADD COLUMN"
            ):
                raise sqlite3.OperationalError("database is locked")
        return _SqlProxy(original_connect(*args, **kwargs), handler)

    monkeypatch.setattr(sqlite3, "connect", blocked_connect)
    store = LabMemory(memory_db)
    report = store.init_schema()
    assert report["added"] == []
    assert len(report["failed"]) == len(ASSISTANT_COLUMNS)
    assert all(entry["error"] == "database is locked"
               for entry in report["failed"])
    assert set(report["missing"]) == set(ASSISTANT_COLUMNS)

    # graceful degradation: the fact still writes, nothing raises
    row = store.remember(key="duringlock", content="still writes fine",
                         source="test")
    assert row["content"] == "still writes fine"
    pin_result = store.pin(row["id"])
    assert pin_result["ok"] is False
    assert pin_result["reason"] == "migration_failed"
    why = store.why_remember(row["id"])
    assert why["source"] == "test"
    assert why["epistemic"] is None  # column missing -> honest None, no raise
    assert store.migration_report()["missing"] == sorted(ASSISTANT_COLUMNS)


# -- provenance ------------------------------------------------------------

def test_remember_validates_epistemic_and_reports_provenance(lab):
    with pytest.raises(ValueError):
        lab.remember(key="bad", content="nope", epistemic="guess")
    with pytest.raises(ValueError):
        lab.remember(key="bad", content="nope", epistemic="")
    with pytest.raises(ValueError):
        lab.remember(key="bad", content="nope", epistemic="HEARSAY")

    row = lab.remember(
        key="ownername", content="owner is the user", epistemic="told",
        source="voice", source_event_id="evt-9",
    )
    assert row["epistemic"] == "told"
    assert row["source_event_id"] == "evt-9"
    assert row["version"] == 1
    assert row["pinned"] == 0

    why = lab.why_remember(row["id"])
    assert set(why) == {"source", "epistemic", "source_event_id",
                        "created_at", "version"}
    assert why["source"] == "voice"
    assert why["epistemic"] == "told"
    assert why["source_event_id"] == "evt-9"
    assert why["version"] == 1
    assert why["created_at"]

    with pytest.raises(ValueError):
        lab.why_remember(999999)

    # provenance can also be an interpretation, recorded verbatim
    interp = lab.remember(key="hunch", content="the hunch says gamma",
                          epistemic="interpretation", source="brain")
    assert lab.why_remember(interp["id"])["epistemic"] == "interpretation"


# -- pin + fade ------------------------------------------------------------

def test_pin_survives_fade_and_fade_never_hard_deletes(memory_db):
    lab = LabMemory(memory_db)
    lab.init_schema()
    pinned = lab.remember(key="pinnedfact",
                          content="pinnedcanary alpha content",
                          confidence=0.1)
    assert lab.pin(pinned["id"])["ok"] is True
    unpinned = lab.remember(key="unpinnedfact",
                            content="unpinnedcanary alpha content",
                            confidence=0.1)
    strong = lab.remember(key="strongfact",
                          content="strongcanary alpha content",
                          confidence=0.95)

    result = lab.fade_relevance(max_confidence=0.5, min_age_days=0)
    assert result["ok"] is True
    assert unpinned["id"] in result["faded"]
    assert pinned["id"] not in result["faded"]
    assert strong["id"] not in result["faded"]

    # soft-retire only: every row still exists with deleted = 0
    rows = {row["id"]: row for row in _raw(
        memory_db,
        "SELECT id, deleted, pinned, expires_at FROM memories",
    )}
    assert rows[unpinned["id"]]["deleted"] == 0
    assert rows[unpinned["id"]]["expires_at"] is not None
    assert rows[pinned["id"]]["deleted"] == 0
    assert rows[pinned["id"]]["pinned"] == 1
    assert rows[pinned["id"]]["expires_at"] is None  # pinned never faded
    assert rows[strong["id"]]["expires_at"] is None

    # the faded row is gone from recall; the pinned one survives
    assert lab.recall("unpinnedcanary") == []
    assert lab.recall("pinnedcanary") != []
    assert lab.recall("strongcanary") != []


# -- correct ---------------------------------------------------------------

def test_correct_bumps_version_and_blocks_resurrection(lab, memory_db):
    old = lab.remember(
        key="zephyrgate", content="zephyrgate oldpass value",
        epistemic="told", source="voice",
    )
    assert old["version"] == 1
    assert lab.create_summary(old["id"],
                              "summary over zephyrgate oldpass value")["ok"]

    out = lab.correct(old["id"], "zephyrgate newpass value",
                      reason="rotated after audit")
    assert out["ok"] is True
    new_id = out["new_memory_id"]
    assert new_id != old["id"]
    assert out["version"] == 2
    assert out["reason"] == "rotated after audit"
    assert out["record"]["derived_from"] == str(old["id"])

    old_row = _raw(memory_db, "SELECT * FROM memories WHERE id = ?",
                   (old["id"],))[0]
    assert old_row["superseded_by"] == new_id
    assert old_row["version"] == 1          # historical version is retained
    assert lab.why_remember(new_id)["version"] == 2

    # recall serves the corrected row only
    contents = [hit["content"] for hit in lab.recall("zephyrgate", top_n=10)]
    assert "zephyrgate newpass value" in contents
    assert "zephyrgate oldpass value" not in contents
    assert lab.recall("oldpass", top_n=10) == []

    # index: old content physically gone, new content findable
    assert lab.index_search("oldpass", top_n=10) == []
    assert lab.index_search("newpass", top_n=10) != []
    if lab.index_status()["engine"] == "fts5":
        assert _fts_match_hits(memory_db, "oldpass") == []

    # dependents: stale, never fresh again for the dead source
    fresh = lab.list_summaries(old["id"])
    assert fresh == []
    stale = lab.list_summaries(old["id"], include_stale=True)
    assert stale and stale[0]["stale"] == 1
    regen = lab.regenerate_summary(old["id"])
    assert regen["ok"] is False
    assert regen["reason"] == "no_live_source"
    assert lab.list_summaries(old["id"], include_stale=True)[0]["stale"] == 1

    # a dependent on the NEW row regenerates from live content only
    assert lab.create_summary(new_id, "placeholder")["ok"]
    regenerated = lab.regenerate_summary(new_id)
    assert regenerated["ok"] is True
    assert regenerated["summary"] == "zephyrgate newpass value"

    # dashboard and recap carry the correction, never the corrected-away text
    assert "zephyrgate newpass value" in json.dumps(
        lab.dashboard_projection())
    assert "zephyrgate oldpass value" not in json.dumps(
        lab.dashboard_projection())
    recap_text = json.dumps(lab.journal_recap(_today()))
    assert "zephyrgate newpass value" in recap_text
    assert "zephyrgate oldpass value" not in recap_text

    corrections = _raw(
        memory_db,
        "SELECT reason FROM memory_corrections WHERE memory_id = ?",
        (old["id"],),
    )
    assert corrections and corrections[0]["reason"] == "rotated after audit"


# -- forget / unforget -----------------------------------------------------

def test_forget_blocks_all_four_read_paths(lab, memory_db):
    row = lab.remember(
        key="kryptoniumfact",
        content="the kryptonium marker hides here",
        epistemic="observed", source="test",
    )
    today = _today()

    # all four paths find it BEFORE the forget
    assert lab.recall("kryptonium") != []
    assert "kryptonium" in json.dumps(lab.dashboard_projection())
    assert any("kryptonium" in fact["content"]
               for fact in lab.journal_recap(today)["facts"])
    assert lab.index_search("kryptonium") != []
    engine = lab.index_status()["engine"]
    if engine == "fts5":
        assert _fts_match_hits(memory_db, "kryptonium") == [row["id"]]

    assert lab.create_summary(row["id"], "dependent over kryptonium")["ok"]
    assert lab.forget(row["id"])["ok"] is True

    # all four paths miss it AFTER the forget
    assert lab.recall("kryptonium") == []
    assert "kryptonium" not in json.dumps(lab.dashboard_projection())
    assert all("kryptonium" not in fact["content"]
               for fact in lab.journal_recap(today)["facts"])
    assert lab.index_search("kryptonium") == []
    if engine == "fts5":
        assert _fts_match_hits(memory_db, "kryptonium") == []
    # ... and the dependent is invalidated
    assert lab.list_summaries(row["id"]) == []
    assert lab.list_summaries(row["id"], include_stale=True)[0]["stale"] == 1

    # soft delete: the row is still there, just marked
    soft = _raw(memory_db, "SELECT deleted FROM memories WHERE id = ?",
                (row["id"],))
    assert soft[0]["deleted"] == 1


def test_unforget_is_allowed_only_when_no_dependent_was_rebuilt(
    lab, memory_db
):
    row = lab.remember(key="reversibility",
                       content="reversibility content marker",
                       source="test")
    assert lab.create_summary(row["id"], "summary text")["ok"]
    assert lab.forget(row["id"])["ok"]

    # every dependent is stale -> provably not rebuilt -> reversible
    restored = lab.unforget(row["id"])
    assert restored["ok"] is True
    assert lab.recall("reversibility") != []
    assert lab.list_summaries(row["id"]) == []      # stale until regenerated
    assert lab.regenerate_summary(row["id"])["ok"] is True
    assert lab.list_summaries(row["id"])[0]["stale"] == 0

    # forget again, then simulate an out-of-band rebuild while dead
    assert lab.forget(row["id"])["ok"]
    conn = sqlite3.connect(str(memory_db))
    with conn:
        conn.execute(
            "UPDATE memory_summaries SET stale = 0 WHERE memory_id = ?",
            (row["id"],),
        )
    conn.close()
    refused = lab.unforget(row["id"])
    assert refused["ok"] is False
    assert refused["reason"] == "dependent_rebuilt"
    assert _raw(memory_db, "SELECT deleted FROM memories WHERE id = ?",
                (row["id"],))[0]["deleted"] == 1


# -- permission model ------------------------------------------------------

def test_permission_filtering_at_retrieval_and_index_level(lab):
    observed = lab.remember(key="observedfact",
                            content="observedalpha signal", epistemic="observed")
    told = lab.remember(key="toldfact",
                        content="toldbeta signal", epistemic="told")
    interpreted = lab.remember(key="interpfact",
                               content="interpretgamma signal",
                               epistemic="interpretation")

    def ids(results):
        return {row["id"] for row in results}

    for surface in (lab.recall, lab.index_search):
        # the owner sees observed/told even with no permission at all
        assert ids(surface("signal", actor=ASSISTANT_ACTOR,
                           permission="none")) == {observed["id"], told["id"]}
        # a stranger with no permission sees nothing from assistant-private
        assert surface("signal", actor="stranger", permission="none") == []
        # read unlocks observed/told but never an interpretation
        assert ids(surface("signal", actor="stranger",
                           permission="read")) == {observed["id"], told["id"]}
        # interpretation requires interpret permission, even for the owner
        assert ids(surface("interpretgamma", actor=ASSISTANT_ACTOR,
                           permission="read")) == set()
        assert ids(surface("interpretgamma", actor=ASSISTANT_ACTOR,
                           permission="interpret")) == {interpreted["id"]}
        # admin sees everything
        assert ids(surface("signal", actor="stranger",
                           permission="admin")) == {observed["id"], told["id"],
                                                    interpreted["id"]}


def test_world_domain_is_world_public(lab_with_world, world_db):
    lab = lab_with_world
    world = Store(world_db)
    assert world.append_event(3, "nova", "say", "worldpublic event text")
    world.close()
    for actor, permission in (("nobody", "none"),
                              (ASSISTANT_ACTOR, "none")):
        hits = lab.recall("worldpublic", domain="world",
                          actor=actor, permission=permission)
        assert hits, (actor, permission)
        assert hits[0]["text"] == "worldpublic event text"
        assert hits[0]["domain"] == "world"


# -- FTS5 honesty ----------------------------------------------------------

def _independent_fts5_probe():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe_fts USING fts5(a)")
        return True, None
    except sqlite3.Error as error:
        return False, str(error)
    finally:
        conn.close()


def test_fts5_availability_is_reported_truthfully(memory_db):
    available, error = _independent_fts5_probe()
    store = LabMemory(memory_db)
    store.init_schema()
    status = store.index_status()
    assert status["fts5"] is available
    if available:
        assert status["engine"] == "fts5"
        assert status["error"] is None
        assert status["table"] == "memories_fts"
        assert "FTS5" in status["detail"] or "fts5" in status["detail"]
    else:
        assert status["engine"] == "keyword"
        assert status["error"] == error
    # whatever the engine, the index actually answers
    row = store.remember(key="indexcheck", content="indexcheck needle",
                         source="test")
    assert store.index_search("indexcheck") != []
    assert store.recall("indexcheck") != []
    assert store.forget(row["id"])["ok"]
    assert store.index_search("indexcheck") == []


def test_keyword_fallback_works_when_fts5_is_disabled(memory_db):
    store = LabMemory(memory_db, prefer_fts5=False)
    report = store.init_schema()
    assert report["index"]["engine"] == "keyword"
    status = store.index_status()
    assert status["fts5"] is False
    assert status["engine"] == "keyword"
    assert status["error"] is None
    assert "prefer_fts5=False" in status["detail"]

    row = store.remember(key="fallbackfact",
                         content="fallback needle content", source="test")
    assert {hit["id"] for hit in store.index_search("fallback")} == {row["id"]}
    assert store.recall("fallback") != []
    assert store.forget(row["id"])["ok"]
    assert store.index_search("fallback") == []
    assert store.recall("fallback") == []


def test_non_fts_name_collision_is_reported_truthfully(memory_db):
    conn = sqlite3.connect(str(memory_db))
    with conn:
        conn.execute("CREATE TABLE memories_fts(a)")  # squats the name
    conn.close()
    store = LabMemory(memory_db)
    store.init_schema()
    status = store.index_status()
    assert status["fts5"] is False
    assert status["engine"] == "keyword"
    assert "not an FTS5 table" in status["error"]
    # the fallback keeps every control working
    row = store.remember(key="squatfact", content="squat needle content",
                         source="test")
    assert store.recall("squat") != []
    assert store.forget(row["id"])["ok"]
    assert store.recall("squat") == []


# -- checkpoints -----------------------------------------------------------

def test_checkpoint_restore_never_touches_domain1_or_config(
    memory_db, world_db
):
    world = Store(world_db)
    assert world.append_event(1, "nova", "act", "event one")
    world.close()
    conn = sqlite3.connect(str(world_db))
    with conn:
        conn.execute(
            "INSERT INTO characters (id, name, persona, x, y, room, state, "
            "needs_json, goal, task_ref, blocked_reason, memory_json, dwell) "
            "VALUES ('nova', 'Nova', '', 4, 5, 'lab', 'idle', '{}', 'work', "
            "'', '', '[]', 2)"
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('seed', '777')")
        conn.execute("INSERT INTO meta (key, value) "
                     "VALUES ('provider_config', 'local-only')")
    conn.close()

    lab = LabMemory(memory_db, world_db)
    lab.init_schema()
    lab.record_experience("nova", 1, "note", "checkpoint marker alpha")
    lab.record_experience("nova", 2, "note", "checkpoint marker beta")
    fact_one = lab.remember(key="assistantfact",
                            content="assistant must stay byte identical",
                            source="test")
    lab.remember(key="anotherfact", content="second assistant row",
                 source="test")

    checkpoint = lab.character_checkpoint("nova")
    assert checkpoint["ok"] is True
    assert checkpoint["experience_count"] == 2
    assert checkpoint["has_world_state"] is True

    # post-checkpoint drift: a new experience and a moved character
    lab.record_experience("nova", 3, "note", "post checkpoint drift row")
    conn = sqlite3.connect(str(world_db))
    with conn:
        conn.execute("UPDATE characters SET x = 99, y = 98, "
                     "room = 'elsewhere' WHERE id = 'nova'")
    conn.close()

    memories_before = _dump_table(
        memory_db, "SELECT * FROM memories ORDER BY id"
    )
    meta_before = _dump_table(
        world_db, "SELECT * FROM meta ORDER BY key"
    )

    result = lab.restore_character_checkpoint("nova",
                                              checkpoint["checkpoint_id"])
    assert result["ok"] is True
    assert result["world_state_restored"] is True
    assert result["experiences_restored"] == 2
    assert result["experiences_retired"] == 1

    # domain 2 reverted: drift row soft-retired, snapshot rows live
    experience_state = {
        row["text"]: row["deleted"]
        for row in _raw(
            memory_db,
            "SELECT text, deleted FROM character_experiences "
            "WHERE char_id = 'nova'",
        )
    }
    assert experience_state["post checkpoint drift row"] == 1
    assert experience_state["checkpoint marker alpha"] == 0
    assert experience_state["checkpoint marker beta"] == 0

    # world decor/position restored
    moved = _dump_table(
        world_db, "SELECT x, y, room FROM characters WHERE id = 'nova'"
    )
    assert moved == [(4, 5, "lab")]

    # domain 1 and config byte-identical after the restore
    memories_after = _dump_table(
        memory_db, "SELECT * FROM memories ORDER BY id"
    )
    meta_after = _dump_table(world_db, "SELECT * FROM meta ORDER BY key")
    assert memories_after == memories_before
    assert meta_after == meta_before
    assert _raw(memory_db, "SELECT content FROM memories WHERE id = ?",
                (fact_one["id"],))[0]["content"] == \
        "assistant must stay byte identical"

    # unknown / mismatched checkpoints are refused without side effects
    assert lab.restore_character_checkpoint(
        "nova", "does-not-exist")["reason"] == "unknown_checkpoint"
    assert lab.restore_character_checkpoint(
        "other", checkpoint["checkpoint_id"]
    )["reason"] == "checkpoint_char_mismatch"
    assert _dump_table(memory_db,
                       "SELECT * FROM memories ORDER BY id") == memories_before
    assert _dump_table(world_db,
                       "SELECT * FROM meta ORDER BY key") == meta_before


# -- dashboard is read-only ------------------------------------------------

def test_dashboard_projection_is_read_only(lab, monkeypatch):
    lab.remember(key="dashfact", content="dashboard read-only row",
                 source="test")
    lab.record_experience("nova", 1, "note", "dashboard experience row")

    statements = []
    original_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        def handler(sql):
            text = " ".join(str(sql).split())
            statements.append(text)
            head = text.upper().split(" ", 1)[0] if text else ""
            if head not in ("SELECT", "PRAGMA", "WITH"):
                raise AssertionError(
                    f"dashboard_projection issued a write: {text[:160]}"
                )
        return _SqlProxy(original_connect(*args, **kwargs), handler)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    result = lab.dashboard_projection()

    assert statements, "dashboard should have issued read SQL"
    for sql in statements:
        assert sql.upper().startswith(("SELECT", "PRAGMA", "WITH")), sql

    assert set(result) >= {"assistant_facts", "character_experiences",
                           "artifacts", "world_events", "generated_at"}
    assert any(fact["content"] == "dashboard read-only row"
               for fact in result["assistant_facts"])
    json.dumps(result)  # JSON-serialisable plain dicts

    # the projection surface exposes no write function of its own
    dashboard_names = [name for name in dir(lab) if "dashboard" in name]
    assert dashboard_names == ["dashboard_projection"]


# -- JSON safety -----------------------------------------------------------

def test_every_public_return_is_json_safe(lab, world_db, memory_db):
    world = Store(world_db)
    world.append_event(2, "nova", "act", "json safety event")
    world.close()
    store = LabMemory(memory_db, world_db)
    returns = []

    returns.append(store.init_schema())
    returns.append(store.migration_report())
    row = store.remember(key="jsonfact", content="json safety fact",
                         source="test")
    returns.append(row)
    returns.append(store.why_remember(row["id"]))
    returns.append(store.pin(row["id"]))
    returns.append(store.unpin(row["id"]))
    returns.append(store.create_summary(row["id"], "json summary"))
    returns.append(store.list_summaries(row["id"]))
    returns.append(store.list_summaries(row["id"], include_stale=True))
    returns.append(store.regenerate_summary(row["id"]))
    returns.append(store.record_experience("nova", 1, "note",
                                           "json experience"))
    checkpoint = store.character_checkpoint("nova")
    returns.append(checkpoint)
    returns.append(store.restore_character_checkpoint("nova",
                                                      checkpoint["checkpoint_id"]))
    corrected = store.correct(row["id"], "json safety fact v2",
                              reason="json reason")
    returns.append(corrected)
    returns.append(store.list_summaries(row["id"], include_stale=True))
    returns.append(store.grant_artifact("art-json", task_id="t1",
                                        char_id="nova", label="json artifact",
                                        path="report.txt"))
    returns.append(store.revoke_artifact("art-json"))
    returns.append(store.list_artifacts())
    returns.append(store.recall("json"))
    returns.append(store.index_search("json"))
    returns.append(store.index_status())
    returns.append(store.rebuild_index())
    returns.append(store.fade_relevance(min_age_days=0))
    returns.append(store.dashboard_projection())
    returns.append(store.scrapbook("nova"))
    returns.append(store.journal_recap(_today()))
    target = corrected["new_memory_id"]
    returns.append(store.forget(target))
    returns.append(store.unforget(target))

    assert returns, "the workout should have produced return values"
    for value in returns:
        json.dumps(value)  # raises if any Path/bytes/Row leaked through


# -- never touch ~/.friday -------------------------------------------------

def test_never_touches_friday_state_dir(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # explicit refusal to build lab state inside ~/.friday
    with pytest.raises(ValueError):
        LabMemory(fake_home / ".friday" / "lab.db")
    with pytest.raises(ValueError):
        LabMemory(tmp_path / "ok.db",
                  world_db=fake_home / ".friday" / "world.db")

    memory_db = tmp_path / "lab.db"
    world_path = tmp_path / "world.db"
    world = Store(world_path)
    world.append_event(1, "nova", "act", "home isolation event")
    world.close()

    store = LabMemory(memory_db, world_path)
    store.init_schema()
    row = store.remember(key="homefact", content="home isolation fact",
                         source="test")
    store.create_summary(row["id"], "home isolation summary")
    store.pin(row["id"])
    store.correct(row["id"], "home isolation fact v2", reason="r")
    store.record_experience("nova", 1, "note", "home isolation experience")
    store.grant_artifact("art-home", task_id="t1", char_id="nova",
                         label="home isolation artifact", path="report.txt")
    checkpoint = store.character_checkpoint("nova")
    store.restore_character_checkpoint("nova", checkpoint["checkpoint_id"])
    store.dashboard_projection()
    store.scrapbook("nova")
    store.journal_recap(_today())
    store.recall("home")
    store.index_search("home")
    store.fade_relevance(min_age_days=0)
    store.rebuild_index()
    store.list_artifacts()
    store.forget(row["id"])
    store.unforget(row["id"])
    store.migration_report()

    assert list(fake_home.rglob("*")) == [], (
        "nothing may be written under (the fake) ~/.friday"
    )


# -- no fabricated text ----------------------------------------------------

def test_scrapbook_and_journal_recap_never_fabricate(lab_with_world, world_db):
    lab = lab_with_world
    world = Store(world_db)
    assert world.append_event(5, "nova", "say", "fabrication event canary")
    world.close()
    conn = sqlite3.connect(str(world_db))
    with conn:
        conn.execute(
            "INSERT INTO checkpoints (tick, created_at, snapshot_json) "
            "VALUES (5, ?, '{}')",
            (time.time(),),
        )
    conn.close()

    lab.record_experience("nova", 5, "note", "fabrication exp canary alpha")
    lab.record_experience("nova", 6, "note", "fabrication exp canary beta")
    lab.remember(key="fabricationfact",
                 content="fabrication fact canary gamma", source="test")

    scrap = lab.scrapbook("nova")
    experience_texts = {row["text"] for row in scrap["experiences"]}
    assert experience_texts == {"fabrication exp canary alpha",
                                "fabrication exp canary beta"}
    assert {row["text"] for row in scrap["events"]} == {
        "fabrication event canary"
    }

    recap = lab.journal_recap(_today())
    fact_contents = {row["content"] for row in recap["facts"]}
    assert fact_contents == {"fabrication fact canary gamma"}
    assert {row["text"] for row in recap["experiences"]} <= experience_texts
    assert {row["text"] for row in recap["events"]} == {
        "fabrication event canary"
    }

    # an empty day yields empty structured sections, never invented prose
    empty = lab.journal_recap("1999-01-01")
    assert empty == {"date": "1999-01-01", "facts": [],
                     "experiences": [], "events": []}


# -- bounds and validation -------------------------------------------------

def test_recap_scrapbook_and_lists_are_bounded(lab):
    for index in range(30):
        lab.remember(key=f"boundedfact{index}",
                     content=f"bounded row number {index}", source="test")
    recap = lab.journal_recap(_today(), limit=5)
    assert len(recap["facts"]) == 5

    for index in range(60):
        lab.record_experience("nova", index, "note",
                              f"bounded experience {index}")
    scrap = lab.scrapbook("nova", limit=10)
    assert len(scrap["experiences"]) == 10
    dashboard = lab.dashboard_projection(limit=7)
    assert len(dashboard["assistant_facts"]) <= 7

    with pytest.raises(ValueError):
        lab.journal_recap("not-a-date")
    with pytest.raises(ValueError):
        lab.journal_recap("2026-13-40")
    with pytest.raises(ValueError):
        lab.recall("anything", domain="bogus")
    with pytest.raises(ValueError):
        lab.recall("anything", permission="bogus")

