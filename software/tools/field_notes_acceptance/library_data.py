"""Offline fixtures shared by the library bridge and browser acceptance."""

import json
import os
from types import SimpleNamespace

from source.server import lab_memory, status_ui
from source.server.memory import MemoryStore
from source.server.world import Store, sim


class LibraryData:
    def __init__(self, area, runtime, bridge):
        self.runtime = runtime
        self.bridge = bridge
        self.canonical = area / "canonical.db"
        self.lab_db = area / "experiences.db"
        self.world_db = area / "world.db"
        self.notes = area / "project-notes.md"
        os.environ["FRIDAY_MEMORY_DB"] = str(self.canonical)
        status_ui._project_notes_path = lambda: self.notes
        self.notes.write_text("# Studio notes\nPrimary reference for the shelf. <b>Literal source markup</b>", encoding="utf-8")
        world = sim.new_world(seed=18)
        self.owner = world.characters[0].id
        self.other_owner = world.characters[1].id
        self.world = Store(self.world_db)
        self.world.save(world)
        self.world.append_event(1, self.owner, "rest", "Journal fixture at the resting station")
        self.lab = lab_memory.configure(self.lab_db, self.world_db)
        self.experience = self.lab.record_experience(self.owner, 1, "rest", "Morning observation", source_event_id="evt-1")
        self.lab.record_experience("legacy-unmapped", 2, "legacy", "Unresolved field note")
        canonical_sidecar = lab_memory.LabMemory(self.canonical)
        original = canonical_sidecar.remember("Studio hours", "Opens at nine", source="user")
        canonical_sidecar.create_summary(original["id"], "Previous studio hours")
        current = self.bridge.update_memory("mem-1", "Studio hours", "Opens at ten")
        assert current["ok"], current
        canonical_sidecar.create_summary(current["id"], "Current studio hours")
        self.bridge.add_memory("Reference shelf", "The archive is upstairs.")
        self.archives = area / "transcripts"
        self.archives.mkdir()
        self.runtime.conversation_history_path = self.archives
        self.runtime.conversation_filename = "Active.json"
        self.runtime.messages = [{"role": "user", "content": "Live buffer stays separate"}]
        self.write_archive("Studio_visit.json", [
            {"id": "speaker-message-1", "role": "user", "type": "message",
             "timestamp": "2026-11-08T14:00:00+01:00", "source": "fixture text", "content": "What time does the studio open?"},
            {"message_id": "speaker-message-2", "role": "assistant", "type": "message",
             "content": "At ten. <img src=x onerror='window.archiveRan=true'>"},
            {"role": "computer", "type": "image", "content": "ATTACHMENT_PAYLOAD"},
        ])
        self.write_archive("Active.json", self.runtime.messages)
        (self.archives / "Broken.json").write_text("[", encoding="utf-8")
        self.write_archive("Long_session.json", [{"role": "user", "content": str(i)} for i in range(205)])

    def write_archive(self, name, messages):
        (self.archives / name).write_text(json.dumps(messages), encoding="utf-8")

    def control(self, command):
        action = command["action"]
        if action == "library:change":
            return self.bridge.character_correct(self.owner, "exp-1", "Changed elsewhere", "Fixture interleaving")
        if action == "library:break-notes":
            self.notes = self.notes.parent
        if action == "library:empty-archive":
            empty = self.archives.parent / "empty"
            empty.mkdir(exist_ok=True)
            self.runtime.conversation_history_path = empty
        if action == "library:broken-archive":
            self.runtime.conversation_history_path = self.notes.parent / "not-a-folder.txt"
            self.runtime.conversation_history_path.write_text("Fixture", encoding="utf-8")
        if action == "library:restore-archive":
            self.runtime.conversation_history_path = self.archives
        if action == "library:fail-reader":
            self.runtime.library_error = bool(command["value"])
        if action == "library:state":
            return {"active_filename": self.runtime.conversation_filename,
                    "active_messages": self.runtime.messages, "owner": self.owner}
        return {}


def create_library_fixture(area):
    runtime = SimpleNamespace()
    bridge = status_ui.WebviewBridge(status_ui.StatusBus(), runtime)
    return LibraryData(area, runtime, bridge)
