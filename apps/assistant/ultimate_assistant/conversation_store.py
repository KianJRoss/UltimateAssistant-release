from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


class ConversationStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    model_content TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS messages_by_conversation "
                "ON messages(conversation_id, id)"
            )
            connection.execute("""CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                user_message_id INTEGER NOT NULL, elapsed_seconds REAL NOT NULL,
                label TEXT NOT NULL, created_at TEXT NOT NULL)""")
            message_columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
            if "model_content" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN model_content TEXT")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS conversation_memory (
                    conversation_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    through_message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def append(self, conversation_id: str, role: str, content: str, *, model_content: str | None = None) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported conversation role.")
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO messages(conversation_id, role, content, model_content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, role, content, model_content or content, datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.lastrowid)

    def history(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT id, role, content, model_content, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        result = [dict(row) for row in rows]
        with self._lock, closing(self._connect()) as connection, connection:
            activity = connection.execute("SELECT user_message_id, elapsed_seconds, label FROM activity WHERE conversation_id = ? ORDER BY id", (conversation_id,)).fetchall()
        for item in result:
            item["activity"] = [dict(event) for event in activity if event["user_message_id"] == item["id"]]
        return result

    def record_activity(self, conversation_id: str, user_message_id: int, elapsed: float, label: str) -> dict:
        event = {"elapsed_seconds": round(elapsed, 2), "label": label[:200]}
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("INSERT INTO activity(conversation_id,user_message_id,elapsed_seconds,label,created_at) VALUES(?,?,?,?,?)",
                               (conversation_id, user_message_id, event["elapsed_seconds"], event["label"], datetime.now(timezone.utc).isoformat()))
        return event

    def memory(self, conversation_id: str) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT summary, through_message_id, updated_at FROM conversation_memory "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else {"summary": "", "through_message_id": 0, "updated_at": None}

    def compaction_batch(self, conversation_id: str, *, retain_recent: int = 20) -> list[dict[str, Any]]:
        state = self.memory(conversation_id)
        with self._lock, closing(self._connect()) as connection, connection:
            cutoff = connection.execute(
                "SELECT id FROM messages WHERE conversation_id = ? "
                "ORDER BY id DESC LIMIT 1 OFFSET ?",
                (conversation_id, retain_recent),
            ).fetchone()
            if not cutoff:
                return []
            rows = connection.execute(
                "SELECT id, role, content, model_content, created_at FROM messages "
                "WHERE conversation_id = ? AND id > ? AND id <= ? ORDER BY id",
                (conversation_id, state["through_message_id"], cutoff["id"]),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        total_chars = 0
        for row in rows:
            item = dict(row)
            if item.get("model_content"):
                item["content"] = item["model_content"]
            item.pop("model_content", None)
            if messages and (len(messages) >= 50 or total_chars + len(item["content"]) > 30000):
                break
            messages.append(item)
            total_chars += len(item["content"])
        if len(messages) < 8 and total_chars < 6000:
            return []
        return messages

    def save_memory(self, conversation_id: str, summary: str, through_message_id: int) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO conversation_memory(conversation_id, summary, through_message_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET summary = excluded.summary,
                     through_message_id = excluded.through_message_id, updated_at = excluded.updated_at""",
                (conversation_id, summary, through_message_id, datetime.now(timezone.utc).isoformat()),
            )

    def clear(self, conversation_id: str) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            connection.execute("DELETE FROM activity WHERE conversation_id = ?", (conversation_id,))
            connection.execute("DELETE FROM conversation_memory WHERE conversation_id = ?", (conversation_id,))
