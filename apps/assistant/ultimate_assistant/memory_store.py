from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

import httpx

from .settings import settings

MEMORY_CATEGORIES = {"rule", "preference", "context", "episode"}


def configured_embedding_model() -> str:
    if os.environ.get("ULTIMATE_ASSISTANT_EMBEDDING_MODEL"):
        return os.environ["ULTIMATE_ASSISTANT_EMBEDDING_MODEL"].strip()
    try:
        preferences = json.loads(settings.user_settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        preferences = {}
    return str(preferences.get("memory_embedding_model") or "").strip()


def ollama_embedding(text: str) -> list[float] | None:
    model = configured_embedding_model()
    if not model:
        return None
    endpoint = os.environ.get("ULTIMATE_ASSISTANT_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    response = httpx.post(
        f"{endpoint}/api/embed", json={"model": model, "input": text[:6000]}, timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    vectors = payload.get("embeddings")
    vector = vectors[0] if isinstance(vectors, list) and vectors else payload.get("embedding")
    if not isinstance(vector, list) or not vector:
        return None
    return [float(value) for value in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


class MemoryStore:
    def __init__(self, database: Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 3,
                    source_conversation_id TEXT,
                    source_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    embedding TEXT
                )"""
            )
            connection.execute("CREATE INDEX IF NOT EXISTS memories_active ON memories(status, importance DESC)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def remember(
        self, content: str, *, category: str = "context", title: str = "",
        importance: int = 3, source_conversation_id: str | None = None,
        source_message_id: int | None = None,
        embedder=ollama_embedding,
    ) -> dict[str, Any]:
        content = " ".join(content.split()).strip()
        title = " ".join(title.split()).strip() or content[:80]
        if not content or len(content) > 4000:
            raise ValueError("Memory content must be between 1 and 4000 characters.")
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"Category must be one of: {', '.join(sorted(MEMORY_CATEGORIES))}.")
        if not 1 <= importance <= 5:
            raise ValueError("Importance must be between 1 and 5.")
        vector = None
        try:
            vector = embedder(f"{title}\n{content}") if embedder else None
        except Exception:
            vector = None
        now = datetime.now(timezone.utc).isoformat()
        memory_id = str(uuid.uuid4())
        with self._lock, closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT id FROM memories WHERE status = 'active' AND category = ? AND lower(content) = lower(?)",
                (category, content),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE memories SET title = ?, importance = ?, updated_at = ?, "
                    "source_conversation_id = COALESCE(?, source_conversation_id), "
                    "source_message_id = COALESCE(?, source_message_id), "
                    "embedding = COALESCE(?, embedding) WHERE id = ?",
                    (title, importance, now, source_conversation_id, source_message_id,
                     json.dumps(vector) if vector else None, existing["id"]),
                )
                memory_id = existing["id"]
            else:
                connection.execute(
                    "INSERT INTO memories(id, category, title, content, importance, "
                    "source_conversation_id, source_message_id, created_at, updated_at, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (memory_id, category, title, content, importance, source_conversation_id,
                     source_message_id, now, now, json.dumps(vector) if vector else None),
                )
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return dict(row)

    def list_memories(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM memories WHERE status = 'active' "
                "ORDER BY importance DESC, updated_at DESC LIMIT ?", (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def search(self, query: str, *, limit: int = 8, embedder=ollama_embedding) -> list[dict[str, Any]]:
        terms = {term.casefold() for term in re.findall(r"[\w'-]{2,}", query)}
        if not terms:
            return []
        memories = self.list_memories(limit=500)
        if not memories:
            return []
        try:
            query_vector = embedder(query) if embedder and any(memory.get("embedding") for memory in memories) else None
        except Exception:
            query_vector = None
        ranked: list[tuple[float, dict[str, Any]]] = []
        now = datetime.now(timezone.utc)
        for memory in memories:
            haystack = f"{memory['title']} {memory['content']}".casefold()
            overlap = sum(term in haystack for term in terms) / max(1, len(terms))
            vector_score = 0.0
            if query_vector and memory.get("embedding"):
                try:
                    vector_score = max(0.0, _cosine(query_vector, json.loads(memory["embedding"])))
                except (TypeError, ValueError, json.JSONDecodeError):
                    vector_score = 0.0
            importance_score = float(memory["importance"]) / 5.0
            try:
                age_days = max(0.0, (now - datetime.fromisoformat(memory["updated_at"])).total_seconds() / 86400)
            except (TypeError, ValueError):
                age_days = 365.0
            recency_score = 1.0 / (1.0 + age_days / 180.0)
            if query_vector:
                score = 0.62 * vector_score + 0.20 * overlap + 0.13 * importance_score + 0.05 * recency_score
                threshold = 0.28
            else:
                score = 0.68 * overlap + 0.24 * importance_score + 0.08 * recency_score
                threshold = 0.36
            if memory["category"] == "rule" and (overlap or vector_score >= 0.25):
                score = max(score, 0.95)
            if score >= threshold:
                ranked.append((score, memory))
        ranked.sort(key=lambda item: (item[0], item[1]["importance"], item[1]["updated_at"]), reverse=True)
        selected = [memory for _, memory in ranked[:max(1, min(limit, 30))]]
        if selected:
            accessed = datetime.now(timezone.utc).isoformat()
            with self._lock, closing(self._connect()) as connection, connection:
                connection.executemany(
                    "UPDATE memories SET last_accessed_at = ? WHERE id = ?",
                    [(accessed, item["id"]) for item in selected],
                )
        return selected

    def delete(self, memory_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0

    def update(
        self, memory_id: str, content: str, *, category: str, title: str,
        importance: int, embedder=ollama_embedding,
    ) -> dict[str, Any] | None:
        content = " ".join(content.split()).strip()
        title = " ".join(title.split()).strip() or content[:80]
        if not content or len(content) > 4000:
            raise ValueError("Memory content must be between 1 and 4000 characters.")
        if category not in MEMORY_CATEGORIES:
            raise ValueError(f"Category must be one of: {', '.join(sorted(MEMORY_CATEGORIES))}.")
        if not 1 <= importance <= 5:
            raise ValueError("Importance must be between 1 and 5.")
        try:
            vector = embedder(f"{title}\n{content}") if embedder else None
        except Exception:
            vector = None
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE memories SET category = ?, title = ?, content = ?, importance = ?, "
                "updated_at = ?, embedding = ? WHERE id = ?",
                (category, title, content, importance, now, json.dumps(vector) if vector else None, memory_id),
            )
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return dict(row) if row else None
