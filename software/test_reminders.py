import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from source.server.reminders import ReminderStore, ReminderWorker, parse_reminder, validate_recurrence


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = ReminderStore(Path(self.tempdir.name) / "reminders.db")
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_persistence_and_list(self):
        reminder = self.store.create("ship release", self.now)
        reopened = ReminderStore(self.store.db_path).list()
        self.assertEqual(reopened[0]["id"], reminder["id"])
        self.assertEqual(reopened[0]["status"], "pending")

    def test_due_firing_and_no_duplicate(self):
        self.store.create("one", self.now - timedelta(seconds=1))
        messages = []
        worker = ReminderWorker(self.store, notification_hook=messages.append)
        self.assertEqual(worker.poll_once(now=self.now), 1)
        self.assertEqual(worker.poll_once(now=self.now), 0)
        self.assertEqual(messages, ["Reminder: one"])
        self.assertEqual(self.store.list(status="fired")[0]["status"], "fired")

    def test_concurrent_workers_claim_once(self):
        self.store.create("once", self.now - timedelta(seconds=1))
        messages = []
        workers = [ReminderWorker(self.store, notification_hook=messages.append) for _ in range(2)]
        threads = [threading.Thread(target=worker.poll_once, kwargs={"now": self.now}) for worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(messages, ["Reminder: once"])

    def test_cancellation(self):
        reminder = self.store.create("later", self.now + timedelta(hours=1))
        self.assertTrue(self.store.cancel(reminder["id"]))
        self.assertFalse(self.store.cancel(reminder["id"]))
        self.assertEqual(self.store.list(status="cancelled")[0]["title"], "later")

    def test_recurrence_validation_and_fire(self):
        self.assertEqual(validate_recurrence("interval:60"), "interval:60")
        with self.assertRaises(ValueError):
            validate_recurrence("every five minutes")
        reminder = self.store.create("check", self.now - timedelta(seconds=1), recurrence="interval:60")
        worker = ReminderWorker(self.store)
        self.assertEqual(worker.poll_once(now=self.now), 1)
        row = self.store.list()[0]
        self.assertEqual(row["id"], reminder["id"])
        self.assertEqual(row["status"], "pending")
        self.assertGreater(datetime.fromisoformat(row["due_at"]), self.now)

    def test_quiet_defers_then_delivers(self):
        self.store.create("quiet", self.now - timedelta(seconds=1))
        quiet = [False]
        messages = []
        worker = ReminderWorker(self.store, notification_hook=messages.append, quiet_predicate=lambda: quiet[0])
        worker.poll_once(now=self.now)
        self.assertEqual(messages, [])
        quiet[0] = True
        worker.poll_once(now=self.now)
        self.assertEqual(messages, ["Reminder: quiet"])

    def test_parser_is_deterministic(self):
        due, title = parse_reminder("remind me in 10 minutes to stretch", now=self.now)
        self.assertEqual(title, "stretch")
        self.assertEqual(due, self.now + timedelta(minutes=10))
        due, title = parse_reminder("set a timer for 2 hours", now=self.now)
        self.assertEqual((due, title), (self.now + timedelta(hours=2), "Timer"))
        self.assertIsNone(parse_reminder("remind me sometime tomorrow", now=self.now))

    def test_worker_polling_is_bounded_and_stoppable(self):
        worker = ReminderWorker(self.store, poll_interval=0.05)
        worker.start()
        self.assertTrue(worker._thread.is_alive())
        self.assertTrue(worker.stop(timeout=1))
        self.assertFalse(worker._thread.is_alive())
        self.assertTrue(worker.stop(timeout=1))


if __name__ == "__main__":
    unittest.main()
