from __future__ import annotations

import sqlite3
import base64
import json
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

            connection.execute("""CREATE TABLE IF NOT EXISTS conversation_preferences (
                conversation_id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0,
                settled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")

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
            connection.execute("UPDATE conversation_preferences SET settled=0 WHERE conversation_id=?", (conversation_id,))
            return int(cursor.lastrowid)

    def append_spoken(self, conversation_id: str, content: str, delivery_key: str) -> dict:
        """Record an initiated spoken turn once, so the user's reply has its context."""
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS spoken_deliveries(
                delivery_key TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, message_id INTEGER NOT NULL)''')
            connection.execute('BEGIN IMMEDIATE')
            old = connection.execute('SELECT conversation_id,message_id FROM spoken_deliveries WHERE delivery_key=?',
                                     (delivery_key,)).fetchone()
            if old:
                if old['conversation_id'] != conversation_id:
                    raise ValueError('Spoken delivery belongs to a different conversation')
                return {'recorded':False,'message_id':old['message_id']}
            cursor = connection.execute('''INSERT INTO messages(conversation_id,role,content,model_content,created_at,agent)
                VALUES(?,'assistant',?,?,?,'tutor')''',
                (conversation_id,content,content,datetime.now(timezone.utc).isoformat()))
            connection.execute('INSERT INTO spoken_deliveries VALUES(?,?,?)',
                               (delivery_key,conversation_id,cursor.lastrowid))
            return {'recorded':True,'message_id':cursor.lastrowid}

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

    def message_page(self, conversation_id: str, *, before_id: int | None = None, limit: int = 100) -> dict:
        """Display-only history, newest page first, with chronological messages inside each page."""
        if before_id is not None and (type(before_id) is not int or before_id < 1):
            raise ValueError("Invalid message cursor")
        size = max(1, min(limit, 100))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, role, content, created_at, agent FROM messages WHERE conversation_id=? "
                + ("AND id<? " if before_id is not None else "")
                + "ORDER BY id DESC LIMIT ?",
                [conversation_id, *([before_id] if before_id is not None else []), size + 1],
            ).fetchall()
        messages = [dict(row) for row in reversed(rows[:size])]
        return {"conversation_id": conversation_id, "messages": messages,
                "next_before_id": messages[0]["id"] if len(rows) > size else None}

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

    def conversations(self, limit: int = 100, *, organized: bool = False) -> list[dict[str, Any]]:
        """Conversations oldest-first by last activity: id, started_at, last_at,
        message count, a title (the first user message) and any saved summary."""
        order = "pinned DESC, settled ASC, last_at DESC" if organized else "last_at DESC"
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"""SELECT m.conversation_id AS id, MIN(m.created_at) AS started_at, MAX(m.created_at) AS last_at,
                          COUNT(*) AS messages,
                          (SELECT content FROM messages f WHERE f.conversation_id = m.conversation_id
                             AND f.role = 'user' ORDER BY f.id LIMIT 1) AS first_message,
                          cm.summary AS summary, COALESCE(cp.pinned,0) AS pinned, COALESCE(cp.settled,0) AS settled
                   FROM messages m LEFT JOIN conversation_memory cm ON cm.conversation_id = m.conversation_id
                   LEFT JOIN conversation_preferences cp ON cp.conversation_id=m.conversation_id
                   GROUP BY m.conversation_id ORDER BY {order} LIMIT ?""",
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

    def history_page(self, *, limit: int = 50, cursor: str | None = None, settled: bool = False, query: str = "") -> dict:
        query = query.strip()
        if len(query) > 200:
            raise ValueError("Search is too long")
        anchor = None
        if cursor:
            try:
                if len(cursor) > 2048: raise ValueError()
                anchor = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
                if (anchor.get('v') != 1 or anchor.get('settled') is not settled or anchor.get('query') != query
                    or type(anchor.get('pin')) is not int or anchor['pin'] not in {0,1}
                    or not isinstance(anchor.get('last'),str) or len(anchor['last']) > 80
                    or not isinstance(anchor.get('id'),str) or len(anchor['id']) > 200): raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise ValueError("Invalid history cursor") from exc
        page_size = max(1,min(limit,100))
        predicate = ""
        params = [int(settled), query]
        if anchor:
            predicate = "AND (pinned < ? OR (pinned = ? AND (last_at < ? OR (last_at = ? AND id < ?))))"
            params += [anchor['pin'],anchor['pin'],anchor['last'],anchor['last'],anchor['id']]
        params.append(page_size+1)
        with self._lock, closing(self._connect()) as connection:
            rows=connection.execute(f"""WITH history AS (
                SELECT m.conversation_id AS id, MIN(m.created_at) AS started_at, MAX(m.created_at) AS last_at,
                    COUNT(*) AS messages, COALESCE(cp.pinned,0) AS pinned, COALESCE(cp.settled,0) AS settled,
                    (SELECT content FROM messages f WHERE f.conversation_id=m.conversation_id AND f.role='user' ORDER BY f.id LIMIT 1) AS first_message
                FROM messages m LEFT JOIN conversation_preferences cp ON cp.conversation_id=m.conversation_id
                GROUP BY m.conversation_id)
                SELECT * FROM history WHERE settled=? AND instr(lower(COALESCE(first_message,'')), lower(?))>0
                {predicate} ORDER BY pinned DESC,last_at DESC,id DESC LIMIT ?""", params).fetchall()
        items=[]
        for row in rows[:page_size]:
            item=dict(row);title=' '.join(str(item.pop('first_message') or '').split())
            item['title']=title if len(title)<=80 else title[:79]+'…'
            items.append(item)
        next_cursor=None
        if len(rows)>page_size:
            last=items[-1]
            payload={'v':1,'settled':settled,'query':query,'pin':last['pinned'],'last':last['last_at'],'id':last['id']}
            next_cursor=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        return {'conversations':items,'next_cursor':next_cursor}

    def organize(self, conversation_id: str, *, pinned: bool | None = None, settled: bool | None = None) -> dict | None:
        if pinned is None and settled is None:
            raise ValueError("Choose a pin or settle change")
        if pinned and settled:
            raise ValueError("A settled conversation cannot also be pinned")
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM messages WHERE conversation_id=? LIMIT 1", (conversation_id,)).fetchone():
                return None
            connection.execute("INSERT OR IGNORE INTO conversation_preferences VALUES (?,0,0,?)", (conversation_id,datetime.now(timezone.utc).isoformat()))
            row=connection.execute("SELECT pinned,settled FROM conversation_preferences WHERE conversation_id=?", (conversation_id,)).fetchone()
            next_pinned = bool(row['pinned']) if pinned is None else pinned
            next_settled = bool(row['settled']) if settled is None else settled
            if pinned: next_settled=False
            if settled: next_pinned=False
            connection.execute("UPDATE conversation_preferences SET pinned=?,settled=?,updated_at=? WHERE conversation_id=?", (int(next_pinned),int(next_settled),datetime.now(timezone.utc).isoformat(),conversation_id))
        return {'id':conversation_id,'pinned':next_pinned,'settled':next_settled}

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
