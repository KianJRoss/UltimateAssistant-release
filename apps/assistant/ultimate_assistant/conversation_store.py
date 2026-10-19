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
            if "activity_id" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN activity_id TEXT")
            if "model_content" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN model_content TEXT")
            if "agent" not in message_columns:  # which agent wrote an assistant message
                connection.execute("ALTER TABLE messages ADD COLUMN agent TEXT")
            # Another program's thread (an A2A context, a Responses API response
            # chain) -> the conversation it is in. Follows objective switches.
            connection.execute(
                """CREATE TABLE IF NOT EXISTS protocol_threads (
                    key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    body TEXT, updated_at TEXT NOT NULL)"""
            )
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

    def append(self, conversation_id: str, role: str, content: str, *, model_content: str | None = None,
               agent: str | None = None) -> int:
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported conversation role.")
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO messages(conversation_id, role, content, model_content, created_at, agent) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, role, content, model_content or content, datetime.now(timezone.utc).isoformat(),
                 agent if role == "assistant" else None),
            )
            return int(cursor.lastrowid)

    def last_agent(self, conversation_id: str) -> str | None:
        """The agent that wrote this conversation's latest reply."""
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT agent FROM messages WHERE conversation_id = ? AND role = 'assistant' AND agent IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()
        return row["agent"] if row else None

    def latest_conversation(self) -> str | None:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT conversation_id FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        return row["conversation_id"] if row else None

    def history(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT id, role, content, model_content, created_at, activity_id, agent FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        result = [dict(row) for row in rows]
        with self._lock, closing(self._connect()) as connection, connection:
            activity = connection.execute("SELECT user_message_id, elapsed_seconds, label FROM activity WHERE conversation_id = ? ORDER BY id", (conversation_id,)).fetchall()
        for item in result:
            item["activity"] = [dict(event) for event in activity if event["user_message_id"] == item["id"]]
        return result

    def bind_activity(self, conversation_id: str, message_id: int, activity_id: str) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("UPDATE messages SET activity_id=? WHERE id=? AND conversation_id=? AND role='user'",
                               (activity_id, message_id, conversation_id))

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
                "SELECT id, role, content, model_content, created_at, activity_id, agent FROM messages "
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

    def conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        """Conversations oldest-first by last activity: id, started_at, last_at,
        message count, a title (the first user message) and any saved summary."""
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """SELECT m.conversation_id AS id, MIN(m.created_at) AS started_at, MAX(m.created_at) AS last_at,
                          COUNT(*) AS messages,
                          (SELECT content FROM messages f WHERE f.conversation_id = m.conversation_id
                             AND f.role = 'user' ORDER BY f.id LIMIT 1) AS first_message,
                          cm.summary AS summary
                   FROM messages m LEFT JOIN conversation_memory cm ON cm.conversation_id = m.conversation_id
                   GROUP BY m.conversation_id ORDER BY last_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            title = " ".join(str(item.pop("first_message") or "").split())
            item["title"] = title if len(title) <= 80 else title[:79] + "…"
            item["summary"] = item.get("summary") or ""
            result.append(item)
        return result

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        return next((c for c in self.conversations(limit=1000) if c["id"] == conversation_id), None)

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Messages in any conversation containing `query` (case-insensitive), newest first."""
        needle = f"%{query.strip()}%"
        if len(query.strip()) < 2:
            return []
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT id, conversation_id, role, content, created_at FROM messages "
                "WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (needle, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def thread(self, key: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT conversation_id, body, updated_at FROM protocol_threads WHERE key = ?",
                                     (key,)).fetchone()
        return dict(row) if row else None

    def bind_thread(self, key: str, conversation_id: str, body: str | None = None) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO protocol_threads(key, conversation_id, body, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET conversation_id = excluded.conversation_id,
                     body = COALESCE(excluded.body, protocol_threads.body), updated_at = excluded.updated_at""",
                (key, conversation_id, body, datetime.now(timezone.utc).isoformat()),
            )

    def clear(self, conversation_id: str) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            connection.execute("DELETE FROM activity WHERE conversation_id = ?", (conversation_id,))
            connection.execute("DELETE FROM conversation_memory WHERE conversation_id = ?", (conversation_id,))
