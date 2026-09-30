from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


class TaskStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS assistant_tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT 'personal',
                    kind TEXT NOT NULL DEFAULT 'commitment',
                    due_at TEXT,
                    estimated_minutes INTEGER,
                    related_task_id TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    source TEXT NOT NULL DEFAULT 'user',
                    evidence TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(assistant_tasks)")}
            for name, declaration in (
                ("kind", "TEXT NOT NULL DEFAULT 'commitment'"),
                ("estimated_minutes", "INTEGER"),
                ("related_task_id", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE assistant_tasks ADD COLUMN {name} {declaration}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS assistant_tasks_status_due "
                "ON assistant_tasks(status, due_at, created_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def list_tasks(self, *, include_done: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        condition = "" if include_done else "WHERE status != 'done'"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM assistant_tasks {condition} "
                "ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, created_at LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_task(
        self, title: str, *, details: str = "", domain: str = "personal",
        due_at: str | None = None, kind: str = "commitment",
        estimated_minutes: int | None = None, related_task_id: str | None = None,
        source: str = "user", evidence: str = "",
        deduplicate: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        task_id = uuid.uuid4().hex
        with self._lock, self._connect() as connection:
            if deduplicate:
                row = connection.execute(
                    "SELECT * FROM assistant_tasks WHERE domain=? AND lower(title)=lower(?) "
                    "AND ifnull(due_at,'')=ifnull(?,'') LIMIT 1",
                    (domain, title.strip(), due_at),
                ).fetchone()
                if row:
                    task = dict(row)
                    task["already_exists"] = True
                    return task
            connection.execute(
                "INSERT INTO assistant_tasks "
                "(id,title,details,domain,kind,due_at,estimated_minutes,related_task_id,status,source,evidence,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'open',?,?,?,?)",
                (task_id, title.strip(), details.strip(), domain, kind, due_at,
                 estimated_minutes, related_task_id, source, evidence[:2000], now, now),
            )
        return {
            "id": task_id, "title": title.strip(), "details": details.strip(),
            "domain": domain, "kind": kind, "due_at": due_at,
            "estimated_minutes": estimated_minutes, "related_task_id": related_task_id,
            "status": "open", "source": source,
            "evidence": evidence[:2000], "created_at": now, "updated_at": now,
            "already_exists": False,
        }

    def update_task(
        self, task_id: str, *, status: str | None = None,
        due_at: str | None = None, update_due: bool = False,
        estimated_minutes: int | None = None, update_estimate: bool = False,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        assignments = ["updated_at=?"]
        values: list[Any] = [now]
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if update_due:
            assignments.append("due_at=?")
            values.append(due_at)
        if update_estimate:
            assignments.append("estimated_minutes=?")
            values.append(estimated_minutes)
        if len(assignments) == 1:
            return None
        values.append(task_id)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(f"UPDATE assistant_tasks SET {', '.join(assignments)} WHERE id=?", values)
            if not cursor.rowcount:
                return None
            row = connection.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None
