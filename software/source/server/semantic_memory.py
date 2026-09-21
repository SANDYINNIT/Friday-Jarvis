"""Optional graph and embedding memory over the existing SQLite MemoryStore.

This module is deliberately not imported by the voice path.  Callers opt in by
constructing :class:`SemanticMemoryStore` and explicitly requesting embedding
work.  Missing Ollama configuration, network errors, and malformed responses
fall back to the deterministic keyword store.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import sqlite3

import requests

from .memory import MemoryStore


MAX_FACT_CHARS = 2048
MAX_FIELD_CHARS = 256
MAX_QUERY_CHARS = 512
MAX_TOP_N = 50
MAX_RESULT_CHARS = 12000
MAX_VECTOR_DIMENSIONS = 4096
DEFAULT_TIMEOUT_SECONDS = 10

_SECRET_RE = re.compile(
    r"(?i)(?:password|passwd|passcode|api[_ -]?key|access[_ -]?key|secret|token|"
    r"credential|bearer|private key|authorization)\s*[:=]?\s*\S+|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|eyJ[A-Za-z0-9_-]{20,})\b|-----BEGIN[^\n]*"
)


def _timestamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _bounded(value, limit, name):
    value = str(value or "").strip()
    if not value or len(value) > limit:
        raise ValueError(f"{name} is required and must be at most {limit} characters")
    return value


def redact_secrets(value):
    """Replace obvious credential-shaped values before persistence or HTTP."""
    return _SECRET_RE.sub("[REDACTED]", str(value or ""))


def _safe_text(value, limit, name):
    return _bounded(redact_secrets(value), limit, name)


def _hash_content(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _cosine(left, right):
    if len(left) != len(right) or not left:
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else None


class SemanticMemoryStore:
    """Graph/embedding APIs backed by the same database as ``MemoryStore``."""

    def __init__(self, db_path=None, *, embedding_model=None, embedding_endpoint=None, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.memory = MemoryStore(db_path)
        self.db_path = self.memory.db_path
        self.embedding_model = str(embedding_model or os.environ.get("FRIDAY_EMBEDDING_MODEL", "")).strip()
        self.embedding_endpoint = str(embedding_endpoint or os.environ.get("FRIDAY_EMBEDDING_ENDPOINT", "")).strip()
        self.timeout = max(1, min(int(timeout), 30))
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=1.0)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    source_memory_id INTEGER,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1))
                );
                CREATE INDEX IF NOT EXISTS memory_relations_lookup_idx
                    ON memory_relations (deleted, subject, object, predicate);
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    memory_id INTEGER PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_embeddings_hash_idx
                    ON memory_embeddings (content_hash, model_name);
                """
            )

    def _configured(self):
        return bool(self.embedding_model and self.embedding_endpoint)

    def _embedding(self, text):
        if not self._configured():
            return None
        payload = {"model": self.embedding_model, "prompt": text}
        try:
            response = requests.post(self.embedding_endpoint, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            vector = data.get("embedding")
            if vector is None and data.get("embeddings"):
                vector = data["embeddings"][0]
            if not isinstance(vector, list) or not 0 < len(vector) <= MAX_VECTOR_DIMENSIONS:
                return None
            vector = [float(item) for item in vector]
            if not all(math.isfinite(item) for item in vector):
                return None
            return vector
        except Exception:
            return None

    def remember_fact(self, key, content, *, topic="", scope="persistent", source="semantic", confidence=1.0, expires_at=None, embed=False):
        """Store one provenance-bearing fact; embedding is opt-in per call."""
        key = _safe_text(key, MAX_FIELD_CHARS, "key")
        content = _safe_text(content, MAX_FACT_CHARS, "content")
        topic = redact_secrets(str(topic or "").strip())[:MAX_FIELD_CHARS]
        record = self.memory.remember(
            key=key, content=content, topic=topic, scope=scope,
            source=_safe_text(source, MAX_FIELD_CHARS, "source"),
            confidence=max(0.0, min(float(confidence), 1.0)), expires_at=expires_at,
        )
        if embed:
            self._store_embedding(record["id"], content, self._embedding(content))
        return record

    def _store_embedding(self, memory_id, content, vector):
        if vector is None:
            return False
        now = _timestamp()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO memory_embeddings
                   (memory_id, model_name, vector_json, content_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(memory_id) DO UPDATE SET model_name=excluded.model_name,
                   vector_json=excluded.vector_json, content_hash=excluded.content_hash,
                   updated_at=excluded.updated_at""",
                (int(memory_id), self.embedding_model, json.dumps(vector, separators=(",", ":")),
                 _hash_content(content), now, now),
            )
        return True

    def link_facts(self, subject, predicate, object_, *, source_memory_id=None, confidence=1.0):
        subject = _safe_text(subject, MAX_FIELD_CHARS, "subject")
        predicate = _safe_text(predicate, MAX_FIELD_CHARS, "predicate")
        object_ = _safe_text(object_, MAX_FIELD_CHARS, "object")
        if source_memory_id is not None:
            source_memory_id = int(source_memory_id)
            if source_memory_id <= 0:
                raise ValueError("source_memory_id must be positive")
        now = _timestamp()
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO memory_relations
                   (subject, predicate, object, source_memory_id, confidence, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (subject, predicate, object_, source_memory_id, max(0.0, min(float(confidence), 1.0)), now, now),
            )
            row = connection.execute("SELECT * FROM memory_relations WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def related_facts(self, subject_or_object, *, predicate=None, top_n=10):
        try:
            value = _safe_text(subject_or_object, MAX_FIELD_CHARS, "subject_or_object")
            limit = max(0, min(int(top_n), MAX_TOP_N))
            if not limit:
                return []
            args = [value, value]
            clause = "deleted = 0 AND (subject = ? OR object = ?)"
            if predicate is not None:
                clause += " AND predicate = ?"
                args.append(_safe_text(predicate, MAX_FIELD_CHARS, "predicate"))
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM memory_relations WHERE {clause} ORDER BY updated_at DESC, id DESC LIMIT ?",
                    [*args, limit],
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []

    def recall_semantic(self, query, *, top_n=10, max_chars=MAX_RESULT_CHARS):
        """Combine optional cosine ranking with the existing keyword fallback."""
        try:
            query = _safe_text(query, MAX_QUERY_CHARS, "query")
            limit = max(0, min(int(top_n), MAX_TOP_N))
            char_limit = max(0, min(int(max_chars), MAX_RESULT_CHARS))
            if not query or not limit or not char_limit:
                return []
            keyword_rows = self.memory.recall(query, top_n=MAX_TOP_N)
            query_vector = self._embedding(query)
            candidates = {}
            for rank, row in enumerate(keyword_rows):
                candidates[row["id"]] = (max(0.0, 1.0 - rank / max(1, len(keyword_rows))), row)
            if query_vector is not None:
                with self._connection() as connection:
                    rows = connection.execute(
                        """SELECT m.*, e.vector_json FROM memories m JOIN memory_embeddings e ON e.memory_id = m.id
                           WHERE m.deleted = 0 AND (m.expires_at IS NULL OR m.expires_at > ?)""",
                        (_timestamp(),),
                    ).fetchall()
                for row in rows:
                    try:
                        vector = json.loads(row["vector_json"])
                        score = _cosine(query_vector, vector)
                    except Exception:
                        score = None
                    if score is not None:
                        semantic_score = max(0.0, min(1.0, (score + 1.0) / 2.0))
                        keyword_score = candidates.get(row["id"], (0.0,))[0]
                        candidates[row["id"]] = (0.75 * semantic_score + 0.25 * keyword_score, dict(row))
            ranked = sorted(candidates.values(), key=lambda item: (-item[0], item[1].get("updated_at", ""), item[1]["id"]))
            results = []
            used = 0
            for score, row in ranked[:limit]:
                result = dict(row)
                result.pop("vector_json", None)
                result["score"] = round(float(score), 6)
                size = len(json.dumps(result, ensure_ascii=True))
                if used + size > char_limit:
                    break
                results.append(result)
                used += size
            return results
        except Exception:
            return []

    def forget_fact(self, *, memory_id=None, key=None, topic=None, scope=None):
        try:
            relation_ids = []
            with self._connection() as connection:
                clauses = []
                values = []
                if memory_id is not None:
                    clauses.append("id = ?")
                    values.append(int(memory_id))
                if key is not None:
                    clauses.append("key = ?")
                    values.append(str(key).strip())
                if topic is not None:
                    clauses.append("topic = ?")
                    values.append(str(topic).strip())
                if scope is not None:
                    clauses.append("scope = ?")
                    values.append(str(scope))
                if clauses:
                    relation_ids = [row["id"] for row in connection.execute(
                        "SELECT id FROM memories WHERE " + " AND ".join(clauses), values
                    ).fetchall()]
            count = self.memory.forget(memory_id=memory_id, key=key, topic=topic, scope=scope)
            if relation_ids:
                with self._connection() as connection:
                    connection.execute(
                        "UPDATE memory_relations SET deleted = 1, updated_at = ? WHERE source_memory_id IN ("
                        + ",".join("?" for _ in relation_ids) + ")",
                        [_timestamp(), *relation_ids],
                    )
            return count
        except Exception:
            return 0

    def rebuild_embeddings(self, *, top_n=MAX_TOP_N):
        """Explicitly create/update embeddings; never runs during ordinary recall."""
        if not self._configured():
            return 0
        try:
            limit = max(0, min(int(top_n), 5000))
            with self._connection() as connection:
                rows = connection.execute(
                    """SELECT m.id, m.content, e.content_hash, e.model_name FROM memories m
                       LEFT JOIN memory_embeddings e ON e.memory_id = m.id
                       WHERE m.deleted = 0 AND (m.expires_at IS NULL OR m.expires_at > ?)
                       ORDER BY m.id LIMIT ?""", (_timestamp(), limit),
                ).fetchall()
            updated = 0
            for row in rows:
                content_hash = _hash_content(row["content"])
                if row["content_hash"] == content_hash and row["model_name"] == self.embedding_model:
                    continue
                if self._store_embedding(row["id"], row["content"], self._embedding(row["content"])):
                    updated += 1
            return updated
        except Exception:
            return 0


def semantic_memory_enabled():
    """Return whether both opt-in embedding settings are present."""
    return bool(os.environ.get("FRIDAY_EMBEDDING_MODEL", "").strip() and os.environ.get("FRIDAY_EMBEDDING_ENDPOINT", "").strip())


def remember_fact(*args, db_path=None, **kwargs):
    return SemanticMemoryStore(db_path).remember_fact(*args, **kwargs)


def link_facts(*args, db_path=None, **kwargs):
    return SemanticMemoryStore(db_path).link_facts(*args, **kwargs)


def recall_semantic(query, *, top_n=10, max_chars=MAX_RESULT_CHARS, db_path=None):
    return SemanticMemoryStore(db_path).recall_semantic(query, top_n=top_n, max_chars=max_chars)


def related_facts(subject_or_object, *, predicate=None, top_n=10, db_path=None):
    return SemanticMemoryStore(db_path).related_facts(subject_or_object, predicate=predicate, top_n=top_n)


def forget_fact(*, db_path=None, **kwargs):
    return SemanticMemoryStore(db_path).forget_fact(**kwargs)


def rebuild_embeddings(*, top_n=MAX_TOP_N, db_path=None):
    return SemanticMemoryStore(db_path).rebuild_embeddings(top_n=top_n)
