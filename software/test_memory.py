"""Focused tests for the explicit bounded memory sidecar."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from source.server.memory import (MemoryStore, build_memory_context,
                                extract_automatic_fact,
                                _CREDENTIAL_SUBJECT_RE)


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
        legacy.write_text(
            "=== USER NOTES AND LEARNINGS ===\n- Keep this line\n",
            encoding="utf-8")
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
        self.assertNotEqual(updated["id"], record["id"])
        self.assertEqual(self.store.recall("old", top_n=5), [])
        self.assertEqual(self.store.recall("new content", top_n=5)[0]["id"], updated["id"])

    def test_update_missing_or_deleted_memory_returns_none(self):
        record = self.store.remember("gone", "value")
        self.store.forget(memory_id=record["id"])
        self.assertIsNone(self.store.update(record["id"], key="x", content="y"))
        self.assertIsNone(self.store.update(999999, key="x", content="y"))
        with self.assertRaises(ValueError):
            self.store.update(record["id"])


class ActivityJournalTests(unittest.TestCase):
    # 2026-10-01: the activity journal is queryable but OUT of default recall.
    # Measured live: 38 of 48 rows were FRIDAY's own spoken output and they won
    # every recall slot for a query like "spotify" (three stale transcript lines,
    # zero durable facts). expires_at was NULL on every row, so nothing could
    # ever age out either.

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.directory.name) / "memory.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_activity_rows_hidden_from_default_recall(self):
        self.store.remember(key="Owner", content="the user is the owner, made you")
        self.store.remember(
            key="activity:10_09_1",
            content="[10:09] FRIDAY: Spotify is already open",
            topic="activity",
            source="activity",
        )
        found = self.store.recall("spotify")
        self.assertEqual([r["content"] for r in found if r["topic"] == "activity"], [])

    def test_activity_rows_returned_when_explicitly_requested(self):
        self.store.remember(
            key="activity:10_09_1",
            content="[10:09] FRIDAY: Spotify is already open",
            topic="activity",
            source="activity",
        )
        found = self.store.recall("spotify", include_activity=True)
        self.assertTrue(any(r["topic"] == "activity" for r in found))

    def test_build_memory_context_passes_include_activity_through(self):
        self.store.remember(
            key="activity:10_09_1",
            content="[10:09] FRIDAY: Spotify is already open",
            topic="activity",
            source="activity",
        )
        self.assertEqual(build_memory_context("spotify", self.store), "")
        self.assertIn(
            "Spotify is already open",
            build_memory_context("spotify", self.store, include_activity=True),
        )

    def test_prune_topic_soft_deletes_oldest_beyond_keep(self):
        for index in range(12):
            self.store.remember(
                key="activity:%02d" % index,
                content="action %d" % index,
                topic="activity",
                source="activity",
            )
        self.assertEqual(len(self.store.list_active(limit=100)), 12)
        self.assertEqual(self.store.prune_topic("activity", keep=5), 7)
        active = self.store.list_active(limit=100)
        self.assertEqual(len(active), 5)
        self.assertEqual(
            sorted(row["key"] for row in active),
            ["activity:07", "activity:08", "activity:09", "activity:10", "activity:11"],
        )

    def test_prune_topic_leaves_other_topics_alone(self):
        self.store.remember(key="Owner", content="the user is the owner")
        for index in range(6):
            self.store.remember(
                key="activity:%02d" % index,
                content="action %d" % index,
                topic="activity",
                source="activity",
            )
        self.store.prune_topic("activity", keep=2)
        self.assertTrue(
            any(row["key"] == "Owner" for row in self.store.list_active(limit=100))
        )


class SelectiveMemoryTests(unittest.TestCase):
    # Sir asked that FRIDAY store LESS junk: no questions, no commands, no
    # sentence fragments, no transient pronoun facts, no secrets.
    # These exercise extract_automatic_fact() - the layer that actually judges
    # a user statement. `remember()` is the explicit-write layer and does not
    # judge content (only secrets are stripped from it).

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.directory.name) / "memory.db")

    def tearDown(self):
        self.directory.cleanup()

    def _keys(self):
        return {row["key"] for row in self.store.list_active(limit=50)}

    def _blob(self):
        return " ".join(row["content"] for row in self.store.list_active(limit=50))

    def test_questions_seen_in_the_live_store_are_rejected(self):
        # These three were permanent rows in the live DB before this rule.
        for statement in (
            "Who are you",
            "Then why'd you say that the user is listening to spotify",
            "Please try again right away",
        ):
            self.assertIsNone(extract_automatic_fact(statement), statement)

    def test_short_fragment_is_rejected(self):
        self.assertIsNone(extract_automatic_fact("no"))

    def test_transient_pronoun_facts_are_rejected(self):
        for statement in (
            "Today is Tuesday",
            "It is raining outside",
            "There are three monitors",
            "This is a test",
        ):
            self.assertIsNone(extract_automatic_fact(statement), statement)

    def test_secret_shapes_are_rejected_by_the_extractor(self):
        for secret in (
            "AKIATESTKEY123456789 is my key",
            "AIzaTESTKEY_FOR_PUBLIC_FIXTURE_000000000000",
            "wk-abc123.ws-def456 is my modal token",
        ):
            self.assertIsNone(extract_automatic_fact(secret), secret)

    def test_secret_shapes_never_reach_disk_in_plaintext(self):
        # `remember()` with an explicit key bypasses the extractor, so the
        # stripper is the only defence there. This test is what caught AIza...
        # being persisted verbatim.
        secrets = (
            "AKIATESTKEY123456789",
            "AIzaTESTKEY_FOR_PUBLIC_FIXTURE_000000000000",
            "wk-abc123.ws-def456",
            "123456789:TESTKEY_FOR_PUBLIC_FIXTURE_000000000000",
            "sk-or-v1-" + "a" * 40,
        )
        for index, secret in enumerate(secrets):
            self.store.remember(key="cred%d" % index, content="my key is %s ok" % secret)
        blob = self._blob()
        for secret in secrets:
            self.assertNotIn(secret, blob)

    def test_genuine_facts_are_still_learned(self):
        for statement in (
            "the user is the owner, made you",
            "Minecraft is java",
            "My favourite editor is notepad",
        ):
            self.assertIsNotNone(extract_automatic_fact(statement), statement)

    def test_confidence_ranks_but_never_invents_a_match(self):
        self.store.remember(key="Coffee", content="the user drinks coffee in the morning")
        self.store.remember(key="Cars", content="the user drives a blue car", confidence=0.99)
        found = self.store.recall("coffee")
        self.assertEqual([row["key"] for row in found], ["Coffee"])


class CredentialSubjectTests(unittest.TestCase):
    # 2026-10-02 audit (#4). With FRIDAY_AUTO_MEMORY=1 the extractor was
    # LEARNING real credentials: 'my bank pin is 4821', 'my recovery phrase
    # is ...', 'my locker combination is 12-34-56', 'my door code is ...'.
    # The fix is a SUBJECT gate (see _CREDENTIAL_SUBJECT_RE), not value
    # detection - trying to spot secret VALUES would over-redact ordinary text
    # and still miss anything short or memorable.

    # Synthetic / public example shapes only. Nothing here is a real credential.
    MUST_BLOCK = (
        "my wifi passphrase is correcthorsebattery",
        "my bank pin is 4821",
        "my door code is 9912",
        "my recovery phrase is apple banana cherry",
        "my seed phrase is alpha beta",
        "my locker combination is 12-34-56",
        "my wifi password is hunter2",
        "my steam password is letmein123",
        "my two-factor code is 918273",
        "my security answer is blue moon",
    )
    MUST_KEEP = (
        "the user is the owner",
        "my favourite editor is notepad",
        "Minecraft is written in java",
        "my favourite colour is green",
        "my bike is a blue canyon",
    )

    def test_credential_subjects_are_never_learned(self):
        for statement in self.MUST_BLOCK:
            self.assertIsNone(extract_automatic_fact(statement), statement)

    def test_ordinary_facts_are_still_learned(self):
        for statement in self.MUST_KEEP:
            self.assertIsNotNone(extract_automatic_fact(statement), statement)

    def test_pin_as_a_verb_is_not_mistaken_for_a_credential(self):
        # "pin" alone is a verb in ordinary speech; the pattern requires it to
        # look like a credential subject so it is not over-matched.
        self.assertFalse(_CREDENTIAL_SUBJECT_RE.search("I pinned a note to the board"))
        self.assertTrue(_CREDENTIAL_SUBJECT_RE.search("my pin is 4821"))
        self.assertTrue(_CREDENTIAL_SUBJECT_RE.search("my pin code is 4821"))

    def test_credential_subject_is_case_insensitive(self):
        for statement in ("My Bank PIN is 4821", "MY RECOVERY PHRASE IS a b c"):
            self.assertIsNone(extract_automatic_fact(statement), statement)

    def test_lessons_md_refuses_credential_lines(self):
        # lessons.md is injected into her prompt EVERY turn, so a credential said
        # once would be re-read forever.
        import tempfile as _tf
        from source.server import self_improve as _si
        with _tf.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(_si, "lessons_path", lambda: str(root / "lessons.md")):
                self.assertFalse(
                    _si.append_lesson_note("the user said my bank pin is 4821"))
                self.assertTrue(
                    _si.append_lesson_note("screenshot goes to source/server/screenshots"))
            written = (root / "lessons.md").read_text(encoding="utf-8")
        self.assertNotIn("4821", written)
        self.assertIn("screenshot", written)

    def test_outcomes_md_stays_a_verbatim_transcript(self):
        # Deliberate design decision: outcomes.md records what was said and is
        # NOT injected into her prompt, so it is not filtered by subject.
        import tempfile as _tf
        from source.server import self_improve as _si
        with _tf.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(_si, "outcomes_path", lambda: str(root / "outcomes.md")):
                self.assertTrue(
                    _si.append_outcome("sir mentioned his pin is 4821", tag="asked"))
            written = (root / "outcomes.md").read_text(encoding="utf-8")
        self.assertIn("4821", written)


if __name__ == "__main__":
    unittest.main()