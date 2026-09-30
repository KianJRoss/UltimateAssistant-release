"""Persistent, resumable flow-run metadata with encrypted checkpoints."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from herald.router.secret_vault import SecretVault


DEFAULT_DB_PATH = Path.home() / ".herald" / "flow_runs.db"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class FlowRun:
    id: str
    name: str
    mode: str
    status: str
    project: str | None
    part: str | None
    current_stage: int
    total_stages: int
    definition_ref: str
    checkpoint_ref: str | None
    result_ref: str | None
    error: str | None
    created_at: str
    updated_at: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "mode": self.mode,
            "status": self.status, "project": self.project, "part": self.part,
            "current_stage": self.current_stage, "total_stages": self.total_stages,
            "has_checkpoint": bool(self.checkpoint_ref), "has_result": bool(self.result_ref),
            "error": self.error, "created_at": self.created_at, "updated_at": self.updated_at,
        }


class FlowRunStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, vault: SecretVault | None = None) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.vault = vault or SecretVault()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS flow_runs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, project TEXT, part TEXT,
                    current_stage INTEGER NOT NULL DEFAULT 0, total_stages INTEGER NOT NULL,
                    definition_ref TEXT NOT NULL, checkpoint_ref TEXT, result_ref TEXT,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            # A run left status="running" from before an unclean
            # shutdown/restart has no process actually driving it anymore --
            # nothing previously flagged that, so a caller polling it
            # afterward saw a plain "running" with no hint it's stuck.
            # Matches AgentRunStore's equivalent reconciliation on init.
            # checkpoint_ref is untouched, so /flow-runs/{id}/resume can
            # still pick an "interrupted" run back up.
            conn.execute(
                "UPDATE flow_runs SET status='interrupted', updated_at=? WHERE status='running'",
                (_now(),),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def create(self, spec: dict[str, Any], input_text: str, *,
               project: str | None = None, part: str | None = None) -> FlowRun:
        run_id, now = uuid.uuid4().hex, _now()
        definition_name = f"flows/{run_id}/definition"
        definition_ref = self.vault.put_json(
            definition_name,
            {"spec": spec, "input": input_text, "project": project, "part": part},
            metadata={"kind": "flow-definition", "run_id": run_id},
        )
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO flow_runs "
                "(id,name,mode,status,project,part,total_stages,definition_ref,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, str(spec.get("name") or "flow"), str(spec.get("mode") or "efficiency"),
                 "created", project, part, len(spec.get("flow") or spec.get("stages") or []),
                 definition_ref, now, now),
            )
        return self.get(run_id)

    def get(self, run_id: str) -> FlowRun | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM flow_runs WHERE id=?", (run_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, limit: int = 50) -> list[FlowRun]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM flow_runs ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def definition(self, run_id: str) -> dict[str, Any]:
        run = self._require(run_id)
        return self.vault.get_json(run.definition_ref.removeprefix("vault:"))

    def checkpoint(self, run_id: str) -> dict[str, Any] | None:
        run = self._require(run_id)
        if not run.checkpoint_ref:
            return None
        return self.vault.get_json(run.checkpoint_ref.removeprefix("vault:"))

    def save_checkpoint(self, run_id: str, state: dict[str, Any]) -> FlowRun:
        name = f"flows/{run_id}/checkpoint"
        reference = self.vault.put_json(
            name, state, metadata={"kind": "flow-checkpoint", "run_id": run_id},
        )
        return self._update(
            run_id, status="running", current_stage=int(state.get("next_stage") or 0),
            checkpoint_ref=reference, error=None,
        )

    def complete(self, run_id: str, result: dict[str, Any]) -> FlowRun:
        reference = self.vault.put_json(
            f"flows/{run_id}/result", result,
            metadata={"kind": "flow-result", "run_id": run_id},
        )
        run = self._require(run_id)
        return self._update(
            run_id, status="completed", current_stage=run.total_stages,
            result_ref=reference, error=None,
        )

    def result(self, run_id: str) -> dict[str, Any] | None:
        run = self._require(run_id)
        if not run.result_ref:
            return None
        return self.vault.get_json(run.result_ref.removeprefix("vault:"))

    def fail(self, run_id: str, error: str) -> FlowRun:
        return self._update(run_id, status="failed", error=error[:500])

    def mark_running(self, run_id: str) -> FlowRun:
        return self._update(run_id, status="running", error=None)

    def _update(self, run_id: str, **values: Any) -> FlowRun:
        allowed = {"status", "current_stage", "checkpoint_ref", "result_ref", "error"}
        values = {key: value for key, value in values.items() if key in allowed}
        values["updated_at"] = _now()
        assignments = ",".join(f"{key}=?" for key in values)
        with closing(self._connect()) as conn, conn:
            conn.execute(f"UPDATE flow_runs SET {assignments} WHERE id=?", (*values.values(), run_id))
        return self._require(run_id)

    def _require(self, run_id: str) -> FlowRun:
        run = self.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    @staticmethod
    def _row(row: sqlite3.Row) -> FlowRun:
        return FlowRun(
            id=row["id"], name=row["name"], mode=row["mode"], status=row["status"],
            project=row["project"], part=row["part"], current_stage=row["current_stage"],
            total_stages=row["total_stages"], definition_ref=row["definition_ref"],
            checkpoint_ref=row["checkpoint_ref"], result_ref=row["result_ref"],
            error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"],
        )
