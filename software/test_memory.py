"""Focused tests for the explicit bounded memory sidecar."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from source.server.memory import MemoryStore, build_memory_context


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "memory.db"
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_schema_has_required_columns(self):
        connection = sqlite3.connect(self.db_path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
        finally:
            connection.close()
        self.assertTrue(
            {
                "id", "scope", "key", "content", "source", "confidence",
                "created_at", "updated_at", "expires_at", "superseded_by", "deleted",
            }.issubset(columns)
        )

    def test_recall_is_keyword_based_and_bounded(self):
        for index in range(5):
            self.store.remember(f"project-{index}", f"project note {index}", source="test")
        results = self.store.recall("project", top_n=2)
        self.assertEqual(len(results), 2)
        self.assertTrue(all("project" in row["content"] for row in results))

    def test_remember_updates_deduplicated_key(self):
        first = self.store.remember("preference", "dark mode", source="test")
        second = self.store.remember("preference", "light mode", source="test")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.recall("light", top_n=5)[0]["content"], "light mode")

    def test_expiry_and_deletion_are_excluded(self):
        self.store.remember("expired", "old", expires_at="2000-01-01T00:00:00+00:00")
        active = self.store.remember("active", "keep")
        self.assertEqual(self.store.recall("old"), [])
        self.store.forget(memory_id=active["id"])
        self.assertEqual(self.store.recall("keep"), [])

    def test_list_active_is_bounded_and_excludes_deleted(self):
        for index in range(4):
            self.store.remember(f"item-{index}", f"value {index}")
        deleted = self.store.remember("deleted", "not shown")
        self.store.forget(memory_id=deleted["id"])
        records = self.store.list_active(limit=2)
        self.assertEqual(len(records), 2)
        self.assertNotIn(deleted["id"], {record["id"] for record in records})

    def test_import_does_not_modify_legacy_file(self):
        legacy = Path(self.directory.name) / "pc_memory.md"
        legacy.write_text("=== USER NOTES AND LEARNINGS ===\n- Keep this line\n", encoding="utf-8")
        before = legacy.read_bytes()
        self.assertEqual(self.store.import_from_pc_memory(legacy), 1)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(len(self.store.recall("keep", top_n=5)), 1)

    def test_prompt_context_has_a_hard_bound(self):
        for index in range(10):
            self.store.remember(f"topic-{index}", "a very long remembered value " * 20)
        context = build_memory_context("topic", self.store, top_n=10, max_chars=300)
        self.assertLessEqual(len(context), 300)
        self.assertNotIn("topic-9", context)

    def test_update_edits_key_and_content_by_id(self):
        record = self.store.remember("old-key", "old content", source="test")
        updated = self.store.update(record["id"], key="new-key", content="new content")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["key"], "new-key")
        self.assertEqual(updated["content"], "new content")
        self.assertEqual(updated["id"], record["id"])
        self.assertEqual(self.store.recall("old", top_n=5), [])
        self.assertEqual(self.store.recall("new content", top_n=5)[0]["id"], record["id"])

    def test_update_missing_or_deleted_memory_returns_none(self):
        record = self.store.remember("gone", "value")
        self.store.forget(memory_id=record["id"])
        self.assertIsNone(self.store.update(record["id"], key="x", content="y"))
        self.assertIsNone(self.store.update(999999, key="x", content="y"))
        with self.assertRaises(ValueError):
            self.store.update(record["id"])


if __name__ == "__main__":
    unittest.main()
