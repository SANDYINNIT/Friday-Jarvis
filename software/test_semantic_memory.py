"""Headless tests for the optional semantic/graph memory layer."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from source.server.semantic_memory import (
    MAX_TOP_N,
    SemanticMemoryStore,
)


class SemanticMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SemanticMemoryStore(Path(self.directory.name) / "memory.db")

    def tearDown(self):
        self.directory.cleanup()

    def test_graph_relations_and_provenance(self):
        fact = self.store.remember_fact("home", "I live in Toronto", source="explicit-test")
        relation = self.store.link_facts("user", "lives_in", "Toronto", source_memory_id=fact["id"], confidence=0.8)
        self.assertEqual(relation["source_memory_id"], fact["id"])
        self.assertEqual(self.store.related_facts("Toronto")[0]["predicate"], "lives_in")

    def test_mocked_embedding_storage_and_cosine_search(self):
        with patch.dict("os.environ", {"FRIDAY_EMBEDDING_MODEL": "test-embed", "FRIDAY_EMBEDDING_ENDPOINT": "http://embed"}):
            store = SemanticMemoryStore(self.store.db_path)
            response = MagicMock()
            response.json.return_value = {"embedding": [1.0, 0.0]}
            response.raise_for_status.return_value = None
            with patch("source.server.semantic_memory.requests.post", return_value=response) as post:
                fact = store.remember_fact("coffee", "I prefer coffee", source="test")
                self.assertEqual(store.rebuild_embeddings(), 1)
                result = store.recall_semantic("tea preference", top_n=1)
                self.assertEqual(result[0]["id"], fact["id"])
                self.assertGreaterEqual(result[0]["score"], 0.0)
                self.assertGreaterEqual(post.call_count, 2)

    def test_fallback_makes_no_network_call_without_configuration(self):
        self.store.remember_fact("project", "The project uses SQLite", source="test")
        with patch("source.server.semantic_memory.requests.post") as post:
            result = self.store.recall_semantic("SQLite", top_n=1)
        self.assertEqual(len(result), 1)
        post.assert_not_called()

    def test_bounds_redaction_and_deletion(self):
        fact = self.store.remember_fact("secret", "password: hunter2 and safe note", source="test")
        self.assertNotIn("hunter2", fact["content"])
        self.assertLessEqual(len(self.store.recall_semantic("safe", top_n=999, max_chars=999999)), MAX_TOP_N)
        self.assertEqual(self.store.forget_fact(memory_id=fact["id"]), 1)
        self.assertEqual(self.store.recall_semantic("safe"), [])

    def test_invalid_embedding_is_ignored(self):
        with patch.dict("os.environ", {"FRIDAY_EMBEDDING_MODEL": "test", "FRIDAY_EMBEDDING_ENDPOINT": "http://embed"}):
            store = SemanticMemoryStore(self.store.db_path)
            response = MagicMock()
            response.json.return_value = {"embedding": [float("nan")]}
            response.raise_for_status.return_value = None
            with patch("source.server.semantic_memory.requests.post", return_value=response):
                fact = store.remember_fact("note", "plain", embed=True)
            self.assertEqual(store.recall_semantic("plain")[0]["id"], fact["id"])


if __name__ == "__main__":
    unittest.main()
