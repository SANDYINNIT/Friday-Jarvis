"""Real Field Notes bridge paths over dedicated synthetic files."""

import sqlite3

import pytest

from source.server import lab_memory, status_ui
from source.server.memory import MemoryStore
from tools.field_notes_acceptance.library_data import create_library_fixture


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_MEMORY_DB", str(tmp_path / "unset.db"))
    monkeypatch.setattr(status_ui, "_project_notes_path", status_ui._project_notes_path)
    for attr in ("_shared", "_shared_memory_db", "_shared_world_db"):
        monkeypatch.setattr(lab_memory, attr, None)
    return create_library_fixture(tmp_path)


def test_memory_projection_preserves_domains_correction_and_reopen(data):
    bridge = data.bridge
    first = bridge.get_memory_library()
    retired = first["canonical"]["history"]["rows"][0]
    assert retired["ref"] == "mem-1" and retired["superseded_by"] == 2
    assert first["events"]["rows"][0]["ref"] == "evt-1"
    assert any(row["ref"] == "exp-1" and row["owner_resolved"]
               for row in first["experiences"]["current"]["rows"])
    assert any(row["ref"] == "exp-2" and not row["owner_resolved"]
               for row in first["experiences"]["current"]["rows"])
    for wrong in ("mem-1", "evt-1", "1", "exp-0"):
        assert not bridge.character_pin(data.owner, wrong, True)["ok"]
        assert not bridge.character_forget(data.owner, wrong)["ok"]
    for wrong in ("exp-1", "evt-1"):
        assert not bridge.delete_memory(wrong)["ok"]
        assert not bridge.update_memory(wrong, "Bad key", "Bad content")["ok"]
    assert not bridge.character_correct(data.other_owner, "exp-1", "Bad text", "Bad owner")["ok"]
    assert bridge.get_memory_library() == first
    assert not bridge.add_memory("Studio hours", "Resurrect original")["ok"]
    next_fact = bridge.update_memory("mem-2", "Opening time", "Opens at eleven", ["Studio hours", "Opens at ten"])
    assert next_fact["ok"]
    assert not bridge.update_memory("mem-2", "Bad", "Stale")["ok"]
    assert bridge.delete_memory(next_fact["ref"])["ok"]
    assert not bridge.add_memory("Opening time", "Resurrect deleted")["ok"]
    reopen = MemoryStore(data.canonical)
    assert not reopen.recall("Opens at")
    assert [row["key"] for row in reopen.list_active()] == ["Reference shelf"]
    history = {row["ref"]: row for row in bridge.get_memory_library()["canonical"]["history"]["rows"]}
    assert history["mem-2"]["previous_id"] == 1 and history[next_fact["ref"]]["deleted"]


def test_experience_actions_reject_stale_and_keep_other_records(data):
    bridge = data.bridge
    assert bridge.character_correct(data.owner, "exp-1", "Confirmed observation",
                                    "Timing", "Morning observation")["ok"]
    assert not bridge.character_correct(data.owner, "exp-1", "Stale content",
                                        "Timing", "Morning observation")["ok"]
    assert bridge.character_pin(data.owner, "exp-1", True)["ok"]
    assert lab_memory.LabMemory(data.lab_db, data.world_db).scrapbook(data.owner)["experiences"][0]["pinned"] == 1
    assert bridge.character_forget(data.owner, "exp-1")["ok"]
    assert not bridge.character_pin(data.owner, "exp-1", False)["ok"]
    lab_memory.configure(data.lab_db, data.world_db)
    reopened = bridge.get_memory_library()
    assert reopened["experiences"]["history"]["rows"][0]["ref"] == "exp-1"
    assert [row["ref"] for row in reopened["experiences"]["current"]["rows"]] == ["exp-2"]
    assert reopened["events"]["rows"][0]["ref"] == "evt-1"


def test_knowledge_provenance_and_source_failure(data):
    library = data.bridge.get_knowledge_library()
    assert library["notes"]["state"] == "ready" and "Primary reference" in library["notes"]["text"]
    assert {row["key"] for row in library["canonical"]["facts"]["rows"]} == {"Studio hours", "Reference shelf"}
    summaries = {row["summary"]: row for row in library["canonical"]["summaries"]["rows"]}
    assert summaries["Previous studio hours"]["current"] == 0
    assert summaries["Current studio hours"]["current"] == 1
    assert summaries["Current studio hours"]["memory_source"] == "user"
    data.notes = data.notes.parent
    assert data.bridge.get_knowledge_library()["notes"]["state"] == "error"


def test_reader_failure_does_not_create_or_migrate_missing_stores(tmp_path, monkeypatch):
    missing = tmp_path / "never-created.db"
    monkeypatch.setenv("FRIDAY_MEMORY_DB", str(missing))
    monkeypatch.setattr(lab_memory, "_shared", None)
    monkeypatch.setattr(lab_memory, "_shared_memory_db", None)
    monkeypatch.setattr(status_ui, "_project_notes_path", lambda: tmp_path / "never-created.md")
    bridge = status_ui.WebviewBridge(status_ui.StatusBus(), None)
    assert bridge.get_memory_library()["canonical"]["state"] == "unavailable"
    assert bridge.get_knowledge_library()["notes"]["state"] == "unavailable"
    assert not missing.exists()
    with sqlite3.connect(missing) as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER)")
    assert bridge.get_memory_library()["canonical"]["state"] == "error"
    with sqlite3.connect(missing) as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [("unrelated",)]


def test_archive_read_only_ids_limits_and_malformed_sources(data):
    bridge = data.bridge
    active = data.runtime.messages.copy()
    listing = bridge.get_conversation_library()
    assert {row["id"] for row in listing["rows"]} == {"Studio_visit.json", "Broken.json", "Long_session.json"}
    for unsafe in ("../Studio_visit.json", "nested\\Studio_visit.json", "Studio_visit.json:stream",
                   "Z:/example/file.json", "Active.json", "active.json"):
        assert bridge.get_conversation_transcript(unsafe)["state"] != "ready"
    result = bridge.get_conversation_transcript("Studio_visit.json")
    assert [row["id"] for row in result["rows"]] == ["speaker-message-1", "speaker-message-2", None]
    assert [row["position"] for row in result["rows"]] == [1, 2, 3]
    assert result["rows"][0]["timestamp"] == "2026-11-08T14:00:00+01:00"
    assert result["rows"][2]["text"] is None
    assert bridge.get_conversation_transcript("Broken.json")["state"] == "error"
    long = bridge.get_conversation_transcript("Long_session.json")
    assert long["truncated"] and len(long["rows"]) == 200
    assert data.runtime.conversation_filename == "Active.json" and data.runtime.messages == active
    assert bridge.get_conversation_library() == listing
    data.control({"action": "library:empty-archive"})
    assert bridge.get_conversation_library() == {"state": "ready", "rows": [], "truncated": False}
    data.control({"action": "library:broken-archive"})
    assert bridge.get_conversation_library()["state"] == "error"
