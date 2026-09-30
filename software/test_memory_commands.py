"""Headless tests for explicit voice memory commands."""

import tempfile
import unittest
from pathlib import Path

from source.server.memory import (
    MAX_VOICE_MEMORY_CONTENT_CHARS,
    MemoryStore,
    format_memory_response,
    extract_and_remember_automatic,
    extract_automatic_fact,
    parse_memory_command,
)


class MemoryCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.directory.name) / "memory.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_remember_is_explicit_and_persistent(self):
        command = parse_memory_command("remember that my favorite color is blue")
        self.assertEqual(command["action"], "remember")
        response = format_memory_response(command, self.store)
        record = self.store.recall("favorite color", top_n=1)[0]
        self.assertEqual(record["key"], "favorite color")
        self.assertEqual(record["topic"], "favorite color")
        self.assertEqual(record["scope"], "persistent")
        self.assertEqual(record["source"], "voice")
        self.assertIn("blue", response)

    def test_called_fact_recall_and_forget(self):
        command = parse_memory_command("remember my Minecraft world is called Cedar Valley")
        format_memory_response(command, self.store)
        recall = format_memory_response(
            parse_memory_command("what do you remember about my Minecraft world?"), self.store
        )
        self.assertIn("Cedar Valley", recall)
        forget = format_memory_response(
            parse_memory_command("forget that my Minecraft world is called Cedar Valley"), self.store
        )
        self.assertIn("1", forget)
        self.assertEqual(self.store.recall("minecraft world"), [])

    def test_empty_and_oversized_commands_are_not_stored(self):
        self.assertEqual(parse_memory_command("remember that")["action"], "malformed")
        self.assertEqual(parse_memory_command("what do you know about")["action"], "malformed")
        oversized = "remember my note is " + ("x" * (MAX_VOICE_MEMORY_CONTENT_CHARS + 1))
        self.assertEqual(parse_memory_command(oversized)["action"], "malformed")
        self.assertEqual(self.store.list_active(), [])

    def test_normal_questions_are_not_memory_commands(self):
        self.assertIsNone(parse_memory_command("what is my favorite color?"))
        self.assertIsNone(parse_memory_command("please remember to open Minecraft"))

    def test_missing_memory_is_truthful(self):
        response = format_memory_response(
            parse_memory_command("what do you remember about my favorite color"), self.store
        )
        self.assertIn("don’t remember", response)

    def test_automatic_learning_accepts_safe_first_person_facts(self):
        for text in ("my favorite color is blue", "my Minecraft world is called Cedar Valley",
                     "I live in Toronto", "I prefer dark mode"):
            record = extract_and_remember_automatic(text, self.store)
            self.assertEqual(record["source"], "automatic")
            self.assertEqual(record["scope"], "persistent")
        self.assertEqual(self.store.recall("favorite color", top_n=1)[0]["content"],
                         "my favorite color is blue")

    def test_automatic_learning_rejects_questions_commands_and_secrets(self):
        rejected = (
            "what is my favorite color?",
            "please remember to open Minecraft",
            "my password is hunter2",
            "my API key is sk-abcdefghijklmnopqrstuvwxyz",
        )
        for text in rejected:
            self.assertIsNone(extract_automatic_fact(text))
        self.assertEqual(self.store.list_active(), [])

    def test_automatic_learning_is_bounded(self):
        oversized = "my favorite color is " + ("x" * 257)
        self.assertIsNone(extract_automatic_fact(oversized))
        self.assertIsNone(extract_automatic_fact("x" * 1025))

    def test_automatic_learning_updates_duplicate_topic(self):
        first = extract_and_remember_automatic("I prefer dark mode", self.store)
        second = extract_and_remember_automatic("I prefer light mode", self.store)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_active()), 1)
        self.assertEqual(self.store.list_active()[0]["content"], "I prefer light mode")


if __name__ == "__main__":
    unittest.main()
