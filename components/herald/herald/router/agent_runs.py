"""Durable, reconnectable execution records for long-running agent work."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from herald.router.secret_vault import SecretVault


DEFAULT_DB_PATH = Path.home() / ".herald" / "agent_runs.db"
DEFAULT_VAULT_ROOT = Path.home() / ".herald" / "agent_run_vault"
TERMINAL_STATUSES = {"completed", "failed", "interrupted"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AgentRun:
    id: str
    session_id: str
    status: str
    request_id: str | None
    state_ref: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str

    def public(self) -> dict[str, Any]:
        start = datetime.fromisoformat(self.started_at or self.created_at)
        end = datetime.fromisoformat(self.completed_at) if self.completed_at else datetime.now(UTC)
        return {
            "id": self.id, "session_id": self.session_id, "status": self.status,
            "request_id": self.request_id,
            "created_at": self.created_at, "started_at": self.started_at,
            "completed_at": self.completed_at, "updated_at": self.updated_at,
            "elapsed_seconds": max(0, round((end - start).total_seconds(), 1)),
        }


class AgentRunStore:
    """SQLite run metadata plus encrypted prompts, results, and progress events."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH,
                 vault: SecretVault | None = None) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.vault = vault or SecretVault(DEFAULT_VAULT_ROOT)
        self._lock = threading.RLock()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    status TEXT NOT NULL, request_id TEXT,
                    state_ref TEXT NOT NULL, created_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(session_id, request_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_session "
                "ON agent_runs(session_id, created_at DESC)"
            )
            # A service restart must never silently repeat filesystem/tool side
            # effects. Interrupted work remains inspectable and can be retried
            # deliberately with a new request ID.
            now = _now()
            rows = conn.execute(
                "SELECT id,state_ref FROM agent_runs WHERE status IN ('queued','running')"
            ).fetchall()
            for row in rows:
                state = self._read_ref(row["state_ref"])
                state.setdefault("events", []).append({
                    "at": now, "kind": "interrupted",
                    "message": "Herald restarted before this run completed.", "data": {},
                })
                reference = self._write(row["id"], state)
                conn.execute(
                    "UPDATE agent_runs SET status='interrupted',state_ref=?,"
                    "completed_at=?,updated_at=? WHERE id=?",
                    (reference, now, now, row["id"]),
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def create(self, session_id: str, prompt: str,
               *, request_id: str | None = None) -> tuple[AgentRun, bool]:
        with self._lock:
            if request_id:
                with closing(self._connect()) as conn:
                    row = conn.execute(
                        "SELECT * FROM agent_runs WHERE session_id=? AND request_id=?",
                        (session_id, request_id),
                    ).fetchone()
                if row:
                    return self._row(row), False
            run_id, now = uuid.uuid4().hex, _now()
            state = {
                "prompt": prompt, "content": None, "error": None, "response": None,
                "events": [{"at": now, "kind": "queued",
                            "message": "Task accepted and queued.", "data": {}}],
            }
            reference = self._write(run_id, state)
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO agent_runs "
                    "(id,session_id,status,request_id,state_ref,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (run_id, session_id, "queued", request_id, reference, now, now),
                )
            return self.require(run_id), True

    def get(self, run_id: str) -> AgentRun | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        return self._row(row) if row else None

    def require(self, run_id: str) -> AgentRun:
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def list(self, *, session_id: str | None = None, limit: int = 50) -> list[AgentRun]:
        query, args = "SELECT * FROM agent_runs", []
        if session_id:
            query += " WHERE session_id=?"
            args.append(session_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(min(max(int(limit), 1), 500))
        with closing(self._connect()) as conn:
            rows = conn.execute(query, args).fetchall()
        return [self._row(row) for row in rows]

    def delete_for_session(self, session_id: str) -> int:
        """Remove encrypted run artifacts when their owning session is deleted."""
        with self._lock:
            runs = self.list(session_id=session_id, limit=500)
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute(
                    "DELETE FROM agent_runs WHERE session_id=?", (session_id,),
                )
            for run in runs:
                self.vault.remove(run.state_ref.removeprefix("vault:"))
            return cursor.rowcount

    def inspect(self, run_id: str) -> dict[str, Any]:
        run = self.require(run_id)
        state = self._state(run)
        return {
            **run.public(), "prompt": state.get("prompt", ""),
            "content": state.get("content"), "error": state.get("error"),
            "events": state.get("events", []), "response": state.get("response"),
        }

    def mark_running(self, run_id: str) -> AgentRun:
        now = _now()
        return self._update(run_id, status="running", started_at=now, updated_at=now)

    def event(self, run_id: str, kind: str, message: str,
              data: dict[str, Any] | None = None) -> AgentRun:
        with self._lock:
            run = self.require(run_id)
            state = self._state(run)
            safe_data = {
                str(key): (str(value)[:1000] if not isinstance(value, (int, float, bool, type(None))) else value)
                for key, value in (data or {}).items()
            }
            events = list(state.get("events") or [])
            events.append({"at": _now(), "kind": str(kind)[:80],
                           "message": str(message)[:2000], "data": safe_data})
            state["events"] = events[-300:]
            reference = self._write(run_id, state)
            return self._update(run_id, state_ref=reference, updated_at=_now())

    def complete(self, run_id: str, response: dict[str, Any]) -> AgentRun:
        with self._lock:
            run = self.require(run_id)
            state = self._state(run)
            now = _now()
            state["content"] = str(response.get("content", ""))
            # Tool observations inside the recursive trace can be extremely
            # large. Progress events retain the useful audit trail without
            # duplicating entire file reads/command outputs in run storage.
            state["response"] = {
                key: value for key, value in response.items() if key != "trace"
            }
            if response.get("trace"):
                state["response"]["trace_steps"] = len(response["trace"])
            state["error"] = None
            state.setdefault("events", []).append({
                "at": now, "kind": "completed",
                "message": "Response saved. Claimed code changes may still require deployment.",
                "data": response.get("budget") or {},
            })
            reference = self._write(run_id, state)
            return self._update(
                run_id, status="completed", state_ref=reference,
                completed_at=now, updated_at=now,
            )

    def fail(self, run_id: str, error: str) -> AgentRun:
        with self._lock:
            run = self.require(run_id)
            state = self._state(run)
            now, detail = _now(), str(error)[:8000]
            state["error"] = detail
            state.setdefault("events", []).append({
                "at": now, "kind": "failed", "message": detail, "data": {},
            })
            reference = self._write(run_id, state)
            return self._update(
                run_id, status="failed", state_ref=reference,
                completed_at=now, updated_at=now,
            )

    def _state(self, run: AgentRun) -> dict[str, Any]:
        return self._read_ref(run.state_ref)

    def _read_ref(self, reference: str) -> dict[str, Any]:
        value = self.vault.get_json(reference.removeprefix("vault:"))
        if not isinstance(value, dict):
            raise RuntimeError("agent run artifact is invalid")
        return value

    def _write(self, run_id: str, state: dict[str, Any]) -> str:
        return self.vault.put_json(
            f"agent-runs/{run_id}/state", state,
            metadata={"kind": "agent-run", "run_id": run_id},
        )

    def _update(self, run_id: str, **values: Any) -> AgentRun:
        allowed = {"status", "state_ref", "started_at", "completed_at", "updated_at"}
        values = {key: value for key, value in values.items() if key in allowed}
        assignments = ",".join(f"{key}=?" for key in values)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"UPDATE agent_runs SET {assignments} WHERE id=?",
                (*values.values(), run_id),
            )
        return self.require(run_id)

    @staticmethod
    def _row(row: sqlite3.Row) -> AgentRun:
        return AgentRun(
            id=row["id"], session_id=row["session_id"], status=row["status"],
            request_id=row["request_id"], state_ref=row["state_ref"],
            created_at=row["created_at"], started_at=row["started_at"],
            completed_at=row["completed_at"], updated_at=row["updated_at"],
        )
