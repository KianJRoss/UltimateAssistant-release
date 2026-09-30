from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


class StudySessionStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS study_sessions (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    title TEXT NOT NULL,
                    planned_start TEXT,
                    planned_minutes INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'planned',
                    started_at TEXT,
                    completed_at TEXT,
                    actual_minutes INTEGER,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS study_sessions_status_start "
                "ON study_sessions(status, planned_start, created_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
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

    def list_sessions(self, *, include_completed: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        condition = "" if include_completed else "WHERE status != 'completed'"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM study_sessions {condition} "
                "ORDER BY CASE WHEN planned_start IS NULL THEN 1 ELSE 0 END, planned_start, created_at LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_session(
        self, subject: str, title: str, planned_minutes: int,
        *, planned_start: str | None = None, notes: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        session = {
            "id": uuid.uuid4().hex,
            "subject": subject.strip(),
            "title": title.strip(),
            "planned_start": planned_start,
            "planned_minutes": planned_minutes,
            "status": "planned",
            "started_at": None,
            "completed_at": None,
            "actual_minutes": None,
            "notes": notes.strip()[:2000],
            "created_at": now,
        }
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO study_sessions "
                "(id,subject,title,planned_start,planned_minutes,status,notes,created_at) "
                "VALUES (?,?,?,?,?,'planned',?,?)",
                (session["id"], session["subject"], session["title"], planned_start,
                 planned_minutes, session["notes"], now),
            )
        return session

    def start_session(self, session_id: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE study_sessions SET status='in_progress', started_at=? "
                "WHERE id=? AND status='planned'", (now, session_id),
            )
            if not cursor.rowcount:
                return None
            row = connection.execute("SELECT * FROM study_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def complete_session(self, session_id: str, *, actual_minutes: int | None = None, notes: str = "") -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        clean_notes = notes.strip()[:2000]
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE study_sessions SET status='completed', completed_at=?, "
                "actual_minutes=COALESCE(?, actual_minutes), notes=CASE WHEN ?='' THEN notes ELSE ? END "
                "WHERE id=? AND status IN ('planned','in_progress')",
                (now, actual_minutes, clean_notes, clean_notes, session_id),
            )
            if not cursor.rowcount:
                return None
            row = connection.execute("SELECT * FROM study_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None
