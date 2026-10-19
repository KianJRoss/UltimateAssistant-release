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
                    verify INTEGER NOT NULL DEFAULT 0,
                    doer TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(assistant_tasks)")}
            for name, declaration in (
                ("kind", "TEXT NOT NULL DEFAULT 'commitment'"),
                ("estimated_minutes", "INTEGER"),
                ("related_task_id", "TEXT"),
                ("verify", "INTEGER NOT NULL DEFAULT 0"),
                ("doer", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE assistant_tasks ADD COLUMN {name} {declaration}")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS task_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('approved', 'changes_requested')),
                    note TEXT NOT NULL CHECK (length(trim(note)) > 0),
                    verifier TEXT NOT NULL CHECK (length(trim(verifier)) > 0),
                    doer TEXT NOT NULL CHECK (length(trim(doer)) > 0),
                    title TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS task_decisions_task_created "
                "ON task_decisions(task_id, created_at, id)"
            )
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
        deduplicate: bool = False, verify: bool = False,
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
                "(id,title,details,domain,kind,due_at,estimated_minutes,related_task_id,status,source,evidence,verify,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'open',?,?,?,?,?)",
                (task_id, title.strip(), details.strip(), domain, kind, due_at,
                 estimated_minutes, related_task_id, source, evidence[:2000], int(bool(verify)), now, now),
            )
        return {
            "id": task_id, "title": title.strip(), "details": details.strip(),
            "domain": domain, "kind": kind, "due_at": due_at,
            "estimated_minutes": estimated_minutes, "related_task_id": related_task_id,
            "status": "open", "source": source,
            "evidence": evidence[:2000], "verify": int(bool(verify)), "doer": None,
            "created_at": now, "updated_at": now,
            "already_exists": False,
        }

    def update_task(
        self, task_id: str, *, status: str | None = None,
        due_at: str | None = None, update_due: bool = False,
        estimated_minutes: int | None = None, update_estimate: bool = False,
        doer: str | None = None,
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        if status is None and not update_due and not update_estimate:
            return None
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM assistant_tasks WHERE id=?", (task_id,),
            ).fetchone()
            if current is None:
                return None

            effective_status = status
            recorded_doer: str | None = None
            if status == "awaiting_verification":
                raise ValueError("awaiting_verification can only be entered by completing verified work.")
            if current["status"] == "awaiting_verification" and status is not None:
                raise ValueError("A task awaiting verification must be approved or have changes requested.")
            if status == "done" and bool(current["verify"]):
                if current["status"] == "awaiting_verification":
                    raise ValueError("This task is already awaiting verification.")
                requested_doer = (doer or "").strip()
                if not requested_doer:
                    raise ValueError("A doer is required to complete a task that needs verification.")
                recorded_doer = (current["doer"] or "").strip() or requested_doer
                if recorded_doer != requested_doer:
                    raise ValueError("Verified rework must be completed by the original doer.")
                effective_status = "awaiting_verification"

            assignments = ["updated_at=?"]
            values: list[Any] = [now]
            if effective_status is not None:
                assignments.append("status=?")
                values.append(effective_status)
            if recorded_doer is not None:
                assignments.append("doer=?")
                values.append(recorded_doer)
            if update_due:
                assignments.append("due_at=?")
                values.append(due_at)
            if update_estimate:
                assignments.append("estimated_minutes=?")
                values.append(estimated_minutes)
            values.append(task_id)
            cursor = connection.execute(f"UPDATE assistant_tasks SET {', '.join(assignments)} WHERE id=?", values)
            if not cursor.rowcount:
                return None
            row = connection.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def verify_task(
        self, task_id: str, outcome: str, note: str, verifier: str,
    ) -> dict[str, Any]:
        if outcome not in {"approved", "changes_requested"}:
            raise ValueError("Outcome must be approved or changes_requested.")
        clean_note = note.strip()
        clean_verifier = verifier.strip()
        if not clean_note:
            raise ValueError("A verification note is required.")
        if not clean_verifier:
            raise ValueError("A verifier is required.")

        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM assistant_tasks WHERE id=?", (task_id,),
            ).fetchone()
            if task is None:
                raise ValueError("Task not found.")
            if task["status"] != "awaiting_verification":
                raise ValueError("Task is not awaiting verification.")
            recorded_doer = (task["doer"] or "").strip()
            if not recorded_doer:
                raise ValueError("Task has no recorded doer.")
            if clean_verifier == recorded_doer:
                raise ValueError("The doer cannot verify their own work.")

            next_status = "done" if outcome == "approved" else "in_progress"
            cursor = connection.execute(
                "UPDATE assistant_tasks SET status=?, updated_at=? "
                "WHERE id=? AND status='awaiting_verification'",
                (next_status, now, task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task is no longer awaiting verification.")
            connection.execute(
                "INSERT INTO task_decisions "
                "(task_id,outcome,note,verifier,doer,title,details,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (task_id, outcome, clean_note, clean_verifier, recorded_doer,
                 task["title"], task["details"], now),
            )
            updated = connection.execute(
                "SELECT * FROM assistant_tasks WHERE id=?", (task_id,),
            ).fetchone()
        return dict(updated)

    def list_task_decisions(
        self, limit: int = 50, outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        if outcome is not None and outcome not in {"approved", "changes_requested"}:
            raise ValueError("Outcome must be approved or changes_requested.")
        bounded_limit = max(1, min(limit, 200))
        condition = "" if outcome is None else "WHERE outcome=?"
        parameters: tuple[Any, ...] = (bounded_limit,) if outcome is None else (outcome, bounded_limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM task_decisions {condition} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]
