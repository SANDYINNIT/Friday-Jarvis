"""Tests for the task store (temp DB, no user data touched)."""

import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.getcwd())

from source.server import task_store as tasks


def temp_store(name=None):
    # A UNIQUE file per call: SQLite keeps the -wal/-shm handles open, so a
    # shared path would leak rows between tests (and cannot be deleted).
    name = name or f"task_store_test_{uuid.uuid4().hex[:8]}.db"
    path = os.path.join(tempfile.gettempdir(), "opencode", name)
    return tasks.TaskStore(db_path=path)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = temp_store()

    def test_add_requires_title(self):
        result = self.store.add("   ")
        self.assertFalse(result["ok"])

    def test_add_returns_task_with_id(self):
        result = self.store.add("Email the supplier")
        self.assertTrue(result["ok"], result)
        self.assertGreater(result["task"]["id"], 0)
        self.assertEqual(result["task"]["status"], "open")
        self.assertEqual(result["task"]["priority"], "normal")

    def test_priority_normalisation(self):
        self.assertEqual(tasks.normalize_priority("URGENT"), "high")
        self.assertEqual(tasks.normalize_priority("someday"), "low")
        self.assertEqual(tasks.normalize_priority(None), "normal")
        self.assertEqual(tasks.normalize_priority("weird"), "normal")

    def test_due_parsing_relative_words(self):
        now = datetime(2026, 9, 29, 15, 0)
        due, label, ok = tasks.parse_due("tomorrow", now=now)
        self.assertTrue(ok)
        self.assertEqual(due, "2026-09-30 09:00")
        self.assertIn("tomorrow", label)

    def test_due_parsing_in_hours(self):
        now = datetime(2026, 9, 29, 15, 0)
        due, label, ok = tasks.parse_due("in 2 hours", now=now)
        self.assertTrue(ok)
        self.assertEqual(due, "2026-09-29 17:00")

    def test_due_parsing_unparseable_is_kept_as_label(self):
        due, label, ok = tasks.parse_due("sometime maybe")
        self.assertFalse(ok)
        self.assertIsNone(due)
        self.assertEqual(label, "sometime maybe")

    def test_every_due_branch_returns_three_values(self):
        # Regression: the word branches once returned 2-tuples while add() and
        # the documented contract expect (due, label, ok).
        for phrase in ("today", "tonight", "this evening", "tomorrow", "next week",
                       "asap", "at 9am", "5pm", "friday", "in 2 hours", ""):
            parsed = tasks.parse_due(phrase)
            self.assertEqual(len(parsed), 3, f"{phrase!r} returned {len(parsed)} values")
            self.assertIsInstance(parsed[2], bool)

    def test_label_never_claims_a_date_before_the_stored_due(self):
        # Regression: a rolled-forward time used to keep a "today" label.
        now = datetime(2026, 9, 29, 15, 0)
        due, label, ok = tasks.parse_due("at 9am", now=now)
        self.assertTrue(ok)
        self.assertEqual(due, "2026-09-30 09:00")
        self.assertNotIn("today", label.lower())
        self.assertIn("tomorrow", label.lower())

    def test_label_matches_stored_date_across_the_day(self):
        now = datetime(2026, 9, 29, 15, 0)
        for phrase in ("today", "tonight", "tomorrow", "next week", "5pm", "friday"):
            due, label, _ok = tasks.parse_due(phrase, now=now)
            stored = datetime.strptime(due, "%Y-%m-%d %H:%M")
            if "today" in label.lower():
                self.assertEqual(stored.date(), now.date(), f"{phrase!r} -> {label}")
            elif "tomorrow" in label.lower():
                self.assertEqual(stored.date(), (now + timedelta(days=1)).date(), f"{phrase!r} -> {label}")

    def test_due_parsing_blank_is_ok(self):
        due, label, ok = tasks.parse_due("")
        self.assertTrue(ok)
        self.assertIsNone(due)

    def test_list_orders_dated_before_undated(self):
        self.store.add("undated task")
        self.store.add("dated task", due="in 3 hours")
        tasks_list = self.store.list_tasks()["tasks"]
        self.assertEqual(tasks_list[0]["title"], "dated task")
        self.assertEqual(tasks_list[-1]["title"], "undated task")

    def test_complete_moves_to_done_and_summary_updates(self):
        task_id = self.store.add("finish report")["task"]["id"]
        self.assertEqual(self.store.summary()["open"], 1)
        done = self.store.complete(task_id)
        self.assertEqual(done["task"]["status"], "done")
        summary = self.store.summary()
        self.assertEqual(summary["open"], 0)
        self.assertEqual(summary["done"], 1)

    def test_reopen_returns_to_open(self):
        task_id = self.store.add("temporary")["task"]["id"]
        self.store.complete(task_id)
        self.assertEqual(self.store.reopen(task_id)["task"]["status"], "open")

    def test_complete_unknown_id_reports(self):
        result = self.store.complete(4242)
        self.assertFalse(result["ok"])

    def test_complete_rejects_non_numeric_id(self):
        result = self.store.complete("not-an-id")
        self.assertFalse(result["ok"])

    def test_delete_removes_task(self):
        task_id = self.store.add("temporary")["task"]["id"]
        self.assertTrue(self.store.delete(task_id)["ok"])
        self.assertFalse(self.store.delete(task_id)["ok"])

    def test_overdue_is_flagged(self):
        self.store.add("overdue thing", due="in 1 minute")
        # Force the due date into the past.
        with self.store._connect() as conn:
            past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
            conn.execute("UPDATE tasks SET due_at=?", (past,))
        summary = self.store.summary()
        self.assertEqual(summary["overdue"], 1)
        self.assertTrue(self.store.list_tasks()["tasks"][0]["overdue"])

    def test_list_all_includes_done(self):
        task_id = self.store.add("finished")["task"]["id"]
        self.store.complete(task_id)
        self.assertEqual(self.store.list_tasks(status="open")["count"], 0)
        self.assertEqual(self.store.list_tasks(status="all")["count"], 1)

    def test_high_priority_counted(self):
        self.store.add("urgent thing", priority="high")
        self.assertEqual(self.store.summary()["high_priority"], 1)


if __name__ == "__main__":
    unittest.main()
    print("PASS: task store")
