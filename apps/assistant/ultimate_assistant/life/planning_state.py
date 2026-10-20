"""Durable storage for explicit planning preferences and plan snapshots.

``PlanningStore`` uses ``planning.sqlite`` below an explicit ``home`` or, by
default, ``life_root()/planning``.  If
the store is created under life_root() when first needed, without
creating storage.  Importing this module does not touch the filesystem.

Preferences preserve the supplied JSON schedule (including offsets and extra
fields) after validation; this module does not infer, expand, or normalize
work windows.  Plans are detached JSON snapshots, ordered by SQLite insertion
sequence rather than caller timestamps or content.  A request ID is durably
idempotent: an identical retry returns the original sequence and UTC creation
time, while reuse for different content is rejected.

Only finite JSON values are supported.  This store does not generate plans,
interpret schedules, execute actions, or coordinate runtime/device APIs.
Focus minutes are limited to 1--1440 and break minutes to 0--1440.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .paths import life_root


_MAX_MINUTES = 1440


def _json_snapshot(value, name):
    def validate(item):
        if item is None or isinstance(item, (str, bool)):
            return
        if isinstance(item, int) and not isinstance(item, bool):
            return
        if isinstance(item, float):
            if math.isfinite(item):
                return
            raise ValueError(f"{name} must contain only finite JSON values")
        if isinstance(item, list):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError(f"{name} JSON object keys must be strings")
            for child in item.values():
                validate(child)
            return
        raise ValueError(f"{name} must contain only JSON values")

    try:
        validate(value)
    except RecursionError as exc:
        raise ValueError(f"{name} must be an acyclic JSON value") from exc
    try:
        text = json.dumps(
            value, ensure_ascii=True, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only finite JSON values") from exc
    return text, json.loads(text)


def _short_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    if len(value) > 512:
        raise ValueError(f"{name} is too long (maximum 512 characters)")
    return value


class PlanningStore:
    """SQLite-backed planning preferences and immutable plan snapshots."""

    def __init__(self, home=None):
        if home is None:
            home = life_root() / "planning"
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "planning.sqlite"
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS preferences(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    work_windows TEXT NOT NULL,
                    focus_minutes INTEGER NOT NULL,
                    break_minutes INTEGER NOT NULL,
                    timezone_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    request_id TEXT UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coordinator_decisions(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT UNIQUE,
                    request_id TEXT UNIQUE,
                    input_revision INTEGER NOT NULL,
                    inputs_fingerprint TEXT NOT NULL,
                    decision_payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coordinator_checkpoints(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint_id TEXT UNIQUE,
                    job_or_session_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    checkpoint_payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coordinator_prerequisites(
                    name TEXT PRIMARY KEY,
                    available INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt REAL,
                    last_error TEXT,
                    backoff_until REAL
                );
                CREATE TABLE IF NOT EXISTS planning_observations(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    observation_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(observation_key, revision)
                );
                """
            )

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _minutes(value, name, minimum):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if not minimum <= value <= _MAX_MINUTES:
            raise ValueError(f"{name} must be between {minimum} and {_MAX_MINUTES}")
        return value

    @staticmethod
    def _aware_iso(value, field):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"work window {field} must be an ISO 8601 string")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"work window {field} must be a valid ISO 8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"work window {field} must include a timezone offset")
        return parsed

    def save_preferences(
        self, work_windows, *, focus_minutes=25, break_minutes=5,
        timezone_name="America/Indianapolis",
    ):
        text, detached = _json_snapshot(work_windows, "work_windows")
        if not isinstance(detached, list):
            raise ValueError("work_windows must be a list")
        for window in detached:
            if not isinstance(window, dict):
                raise ValueError("each work window must be an object")
            start=self._aware_iso(window.get('start',window.get('starts_at')), 'start')
            end=self._aware_iso(window.get('end',window.get('ends_at')), 'end')
            for canonical,alias in [('start','starts_at'),('end','ends_at')]:
                if canonical in window and alias in window and self._aware_iso(window[canonical],canonical)!=self._aware_iso(window[alias],alias):
                    raise ValueError('Conflicting work-window aliases')
            if end <= start:
                raise ValueError("work window end must be strictly after start")
        focus_minutes = self._minutes(focus_minutes, "focus_minutes", 1)
        break_minutes = self._minutes(break_minutes, "break_minutes", 0)
        timezone_name = _short_text(timezone_name, "timezone_name")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            # America/Indianapolis is an IANA backward-compatibility link.  Some
            # deliberately slim system tzdata installations omit backward links
            # while retaining the canonical zone used by the required default.
            if timezone_name == "America/Indianapolis":
                try:
                    ZoneInfo("America/Indiana/Indianapolis")
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError(
                        "timezone_name must identify an available IANA timezone"
                    ) from exc
            else:
                raise ValueError(
                    "timezone_name must identify an available IANA timezone"
                ) from exc
        with self._db() as db:
            db.execute(
                """INSERT INTO preferences VALUES(1,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET
                   work_windows=excluded.work_windows,
                   focus_minutes=excluded.focus_minutes,
                   break_minutes=excluded.break_minutes,
                   timezone_name=excluded.timezone_name""",
                (text, focus_minutes, break_minutes, timezone_name),
            )
        return self.preferences()

    def preferences(self):
        with self._db() as db:
            row = db.execute("SELECT * FROM preferences WHERE singleton=1").fetchone()
        if row is None:
            return None
        return {
            "work_windows": json.loads(row["work_windows"]),
            "focus_minutes": row["focus_minutes"],
            "break_minutes": row["break_minutes"],
            "timezone_name": row["timezone_name"],
        }

    @staticmethod
    def _plan_record(row):
        if row is None:
            return None
        return {
            "sequence": row["sequence"],
            "plan": json.loads(row["plan"]),
            "input_fingerprint": row["input_fingerprint"],
            "request_id": row["request_id"],
            "created_at": row["created_at"],
        }

    def record_plan(self, plan, *, input_fingerprint, request_id=None):
        text, detached = _json_snapshot(plan, "plan")
        if not isinstance(detached, dict):
            raise ValueError("plan must be a JSON object")
        input_fingerprint = _short_text(input_fingerprint, "input_fingerprint")
        if request_id is not None:
            request_id = _short_text(request_id, "request_id")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_id is not None:
                old = db.execute(
                    "SELECT * FROM plans WHERE request_id=?", (request_id,)
                ).fetchone()
                if old is not None:
                    if old["plan"] != text or old["input_fingerprint"] != input_fingerprint:
                        raise ValueError("request_id already belongs to a different plan or fingerprint")
                    return self._plan_record(old)
            created_at = datetime.now(timezone.utc).isoformat()
            cursor = db.execute(
                """INSERT INTO plans(plan,input_fingerprint,request_id,created_at)
                   VALUES(?,?,?,?)""",
                (text, input_fingerprint, request_id, created_at),
            )
            row = db.execute(
                "SELECT * FROM plans WHERE sequence=?", (cursor.lastrowid,)
            ).fetchone()
            return self._plan_record(row)

    def latest_plan(self):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM plans ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        return self._plan_record(row)

    def latest_learning_evidence(self, activity_id=None):
        """Read the evidence snapshot retained with the latest plan.

        This is an advisory projection only; absence or any outcome never
        changes commitment completion.
        """
        record = self.latest_plan()
        evidence = ((record or {}).get("plan") or {}).get("learning_evidence")
        if not isinstance(evidence, dict):
            return None
        detached = _json_snapshot(evidence, "learning_evidence")[1]
        if activity_id is None:
            return detached
        activity_id = _short_text(activity_id, "activity_id")
        return {
            **{key: value for key, value in detached.items()
               if key not in {"activities", "planning_summary", "history"}},
            "activities": ({activity_id: detached.get("activities", {})[activity_id]}
                           if activity_id in detached.get("activities", {}) else {}),
            "planning_summary": ({activity_id: detached.get("planning_summary", {})[activity_id]}
                                 if activity_id in detached.get("planning_summary", {}) else {}),
            "history": [item for item in detached.get("history", [])
                        if item.get("activity_id") == activity_id],
        }

    def record_observation(self, observation_key, kind, payload):
        """Append a revision only when a durable planning observation changes."""
        observation_key = _short_text(observation_key, "observation_key")
        kind = _short_text(kind, "kind")
        text, detached = _json_snapshot(payload, "observation payload")
        if not isinstance(detached, dict):
            raise ValueError("observation payload must be a JSON object")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM planning_observations WHERE observation_key=? ORDER BY revision DESC LIMIT 1",
                (observation_key,),
            ).fetchone()
            if old is not None and old["kind"] == kind and old["payload"] == text:
                return self._observation_record(old)
            revision = (old["revision"] + 1) if old is not None else 1
            created_at = datetime.now(timezone.utc).isoformat()
            cursor = db.execute(
                "INSERT INTO planning_observations(observation_key,revision,kind,payload,created_at) VALUES(?,?,?,?,?)",
                (observation_key, revision, kind, text, created_at),
            )
            row = db.execute("SELECT * FROM planning_observations WHERE sequence=?", (cursor.lastrowid,)).fetchone()
        return self._observation_record(row)

    @staticmethod
    def _observation_record(row):
        return {
            "sequence": row["sequence"], "observation_key": row["observation_key"],
            "revision": row["revision"], "kind": row["kind"],
            "payload": json.loads(row["payload"]), "created_at": row["created_at"],
        }

    def list_observations(self, *, kind=None, observation_key=None):
        clauses, values = [], []
        if kind is not None:
            clauses.append("kind=?"); values.append(_short_text(kind, "kind"))
        if observation_key is not None:
            clauses.append("observation_key=?"); values.append(_short_text(observation_key, "observation_key"))
        query = "SELECT * FROM planning_observations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence"
        with self._db() as db:
            rows = db.execute(query, values).fetchall()
        return [self._observation_record(row) for row in rows]

    def record_decision(
        self,
        decision_id: str,
        decision_payload: dict,
        *,
        input_revision: int,
        inputs_fingerprint: str,
        request_id: str | None = None,
        status: str = "decided",
    ) -> dict:
        decision_id = _short_text(decision_id, "decision_id")
        inputs_fingerprint = _short_text(inputs_fingerprint, "inputs_fingerprint")
        if request_id is not None:
            request_id = _short_text(request_id, "request_id")
        text, detached = _json_snapshot(decision_payload, "decision_payload")
        if not isinstance(detached, dict):
            raise ValueError("decision_payload must be a JSON object")
        if isinstance(input_revision, bool) or not isinstance(input_revision, int) or input_revision < 0:
            raise ValueError("input_revision must be a non-negative integer")

        now_iso = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_id is not None:
                old = db.execute(
                    "SELECT * FROM coordinator_decisions WHERE request_id=?", (request_id,)
                ).fetchone()
                if old is not None:
                    if (
                        old["decision_payload"] != text
                        or old["inputs_fingerprint"] != inputs_fingerprint
                        or old["input_revision"] != input_revision
                    ):
                        raise ValueError("request_id already belongs to a different decision")
                    return {
                        "sequence": old["sequence"],
                        "decision_id": old["decision_id"],
                        "request_id": old["request_id"],
                        "input_revision": old["input_revision"],
                        "inputs_fingerprint": old["inputs_fingerprint"],
                        "decision_payload": json.loads(old["decision_payload"]),
                        "status": old["status"],
                        "created_at": old["created_at"],
                        "updated_at": old["updated_at"],
                    }

            cursor = db.execute(
                """INSERT INTO coordinator_decisions(
                    decision_id, request_id, input_revision, inputs_fingerprint,
                    decision_payload, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    input_revision=excluded.input_revision,
                    inputs_fingerprint=excluded.inputs_fingerprint,
                    decision_payload=excluded.decision_payload,
                    status=excluded.status,
                    updated_at=excluded.updated_at""",
                (
                    decision_id,
                    request_id,
                    input_revision,
                    inputs_fingerprint,
                    text,
                    status,
                    now_iso,
                    now_iso,
                ),
            )
            row = db.execute(
                "SELECT * FROM coordinator_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            return {
                "sequence": row["sequence"],
                "decision_id": row["decision_id"],
                "request_id": row["request_id"],
                "input_revision": row["input_revision"],
                "inputs_fingerprint": row["inputs_fingerprint"],
                "decision_payload": json.loads(row["decision_payload"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    def get_decision(self, decision_id: str) -> dict | None:
        decision_id = _short_text(decision_id, "decision_id")
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM coordinator_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "sequence": row["sequence"],
            "decision_id": row["decision_id"],
            "request_id": row["request_id"],
            "input_revision": row["input_revision"],
            "inputs_fingerprint": row["inputs_fingerprint"],
            "decision_payload": json.loads(row["decision_payload"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_decisions(self, limit: int = 50) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM coordinator_decisions ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "decision_id": row["decision_id"],
                "request_id": row["request_id"],
                "input_revision": row["input_revision"],
                "inputs_fingerprint": row["inputs_fingerprint"],
                "decision_payload": json.loads(row["decision_payload"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def record_checkpoint(
        self,
        checkpoint_id: str,
        job_or_session_id: str,
        stage: str,
        checkpoint_payload: dict,
    ) -> dict:
        checkpoint_id = _short_text(checkpoint_id, "checkpoint_id")
        job_or_session_id = _short_text(job_or_session_id, "job_or_session_id")
        stage = _short_text(stage, "stage")
        text, detached = _json_snapshot(checkpoint_payload, "checkpoint_payload")
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute(
                """INSERT INTO coordinator_checkpoints(
                    checkpoint_id, job_or_session_id, stage, checkpoint_payload, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    stage=excluded.stage,
                    checkpoint_payload=excluded.checkpoint_payload""",
                (checkpoint_id, job_or_session_id, stage, text, now_iso),
            )
            row = db.execute(
                "SELECT * FROM coordinator_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)
            ).fetchone()
            return {
                "sequence": row["sequence"],
                "checkpoint_id": row["checkpoint_id"],
                "job_or_session_id": row["job_or_session_id"],
                "stage": row["stage"],
                "checkpoint_payload": json.loads(row["checkpoint_payload"]),
                "created_at": row["created_at"],
            }

    def get_checkpoints(self, job_or_session_id: str) -> list[dict]:
        job_or_session_id = _short_text(job_or_session_id, "job_or_session_id")
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM coordinator_checkpoints WHERE job_or_session_id=? ORDER BY sequence ASC",
                (job_or_session_id,),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "checkpoint_id": row["checkpoint_id"],
                "job_or_session_id": row["job_or_session_id"],
                "stage": row["stage"],
                "checkpoint_payload": json.loads(row["checkpoint_payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_prerequisite_failure(self, name: str, error: str, *, backoff_seconds: float = 60.0, now_ts: float | None = None) -> dict:
        name = _short_text(name, "prerequisite name")
        now_val = time.time() if now_ts is None else now_ts
        with self._db() as db:
            row = db.execute("SELECT failure_count FROM coordinator_prerequisites WHERE name=?", (name,)).fetchone()
            count = (row["failure_count"] if row else 0) + 1
            backoff_mult = min(3600.0, backoff_seconds * (2 ** min(count - 1, 6)))
            backoff_until = now_val + backoff_mult
            db.execute(
                """INSERT INTO coordinator_prerequisites(name, available, failure_count, last_attempt, last_error, backoff_until)
                   VALUES (?, 0, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       available=0,
                       failure_count=?,
                       last_attempt=?,
                       last_error=?,
                       backoff_until=?""",
                (name, count, now_val, error, backoff_until, count, now_val, error, backoff_until),
            )
            return {"name": name, "available": False, "failure_count": count, "backoff_until": backoff_until, "error": error}

    def record_prerequisite_success(self, name: str) -> dict:
        name = _short_text(name, "prerequisite name")
        now_val = time.time()
        with self._db() as db:
            db.execute(
                """INSERT INTO coordinator_prerequisites(name, available, failure_count, last_attempt, last_error, backoff_until)
                   VALUES (?, 1, 0, ?, NULL, NULL)
                   ON CONFLICT(name) DO UPDATE SET
                       available=1,
                       failure_count=0,
                       last_attempt=?,
                       last_error=NULL,
                       backoff_until=NULL""",
                (name, now_val, now_val),
            )
            return {"name": name, "available": True, "failure_count": 0}

    def get_prerequisite_status(self, name: str, now_ts: float | None = None) -> dict:
        name = _short_text(name, "prerequisite name")
        now_val = time.time() if now_ts is None else now_ts
        with self._db() as db:
            row = db.execute("SELECT * FROM coordinator_prerequisites WHERE name=?", (name,)).fetchone()
        if not row:
            return {"name": name, "available": True, "can_attempt": True, "failure_count": 0}
        backoff_until = row["backoff_until"]
        can_attempt = backoff_until is None or now_val >= backoff_until
        return {
            "name": name,
            "available": bool(row["available"]),
            "can_attempt": can_attempt,
            "failure_count": row["failure_count"],
            "last_error": row["last_error"],
            "backoff_until": backoff_until,
        }
