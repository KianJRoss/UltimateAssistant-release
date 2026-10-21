"""Durable cross-device continuation and capability-aware handoff.

Provides SQLite persistence for continuation context (current task, workspace
binding ref, next step, timestamp) with idempotent writes and revisions.
A continuation can be fenced to the device that owns it (validate_device_access).
No live device calls or mode changes.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

_MAX_ID_LEN = 256
_MAX_REF_LEN = 1024
_MAX_TEXT_LEN = 4096
_MAX_CONTEXT_BYTES = 512 * 1024  # 512 KiB per payload

_NONPORTABLE_KEYS = frozenset((
    "pid", "pids", "process", "processes", "process_handle", "process_handles",
    "handle", "handles", "job", "jobs", "lease_token", "lease_tokens"
))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty ISO 8601 string")
    s = value.strip()
    if s.endswith(("Z", "z")):
        candidate = s[:-1] + "+00:00"
    else:
        candidate = s
    try:
        dt = datetime.fromisoformat(candidate)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a valid ISO 8601 datetime") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_bounded_text(value, name: str, max_len: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} cannot be empty or whitespace only")
    if len(value) > max_len:
        raise ValueError(f"{name} exceeds maximum length of {max_len}")
    return value


def _canonical_json(value) -> str:
    text = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > _MAX_CONTEXT_BYTES:
        raise ValueError(f"Payload exceeds maximum size of {_MAX_CONTEXT_BYTES} bytes")
    return text


def _reject_nonportable(value, location="context"):
    if isinstance(value, dict):
        for key, child in value.items():
            key_cf = str(key).casefold()
            tokens = set(re.split(r"[_\W]+", key_cf))
            if (key_cf in _NONPORTABLE_KEYS or
                    (key_cf != "current_job" and any(tok in _NONPORTABLE_KEYS for tok in tokens if tok))):
                raise ValueError(f"{location}.{key} is device-local and cannot be handed off")
            _reject_nonportable(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nonportable(child, f"{location}[{index}]")


class HandoffContextStore:
    """Durable SQLite storage for cross-device continuation context."""

    def __init__(self, home=None):
        if home is None:
            from . import phone_root
            home = phone_root() / "handoff"
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / "handoff.sqlite"
        self._initialized = False

    def _ensure_initialized(self):
        if not self._initialized:
            self.home.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS contexts(
                        context_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        current_task TEXT NOT NULL,
                        workspace_binding_ref TEXT NOT NULL,
                        next_step TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        owning_device TEXT,
                        PRIMARY KEY(context_id, revision)
                    );
                    CREATE TABLE IF NOT EXISTS idempotency_requests(
                        request_id TEXT PRIMARY KEY,
                        fingerprint TEXT NOT NULL,
                        context_id TEXT NOT NULL,
                        revision INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS active_context(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        context_id TEXT NOT NULL,
                        revision INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_contexts_id_rev ON contexts(context_id, revision);
                """)
                try:
                    db.execute("ALTER TABLE contexts ADD COLUMN owning_device TEXT")
                except sqlite3.OperationalError:
                    pass
            self._initialized = True

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record_continuation(
        self,
        current_task: dict | str,
        workspace_binding_ref: str,
        next_step: dict | str,
        *,
        request_id: str,
        context_id: str | None = None,
        expected_revision: int | None = None,
        timestamp: str | None = None,
        metadata: dict | None = None,
        source_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        conversation_id: str | None = None,
        activity_id: str | None = None,
        current_job: dict | str | None = None,
        checkpoint: dict | str | None = None,
        presentation_id: str | None = None,
        continuation_state: str | None = None,
        user_id: str | None = None,
        owning_device: str | None = None,
        active_device: str | None = None,
    ) -> dict:
        """Idempotently persist continuation context with monotonic revisions."""
        self._ensure_initialized()

        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)
        cid = _validate_bounded_text(context_id or uuid.uuid4().hex, "context_id", _MAX_ID_LEN)
        workspace_ref = _validate_bounded_text(workspace_binding_ref, "workspace_binding_ref", _MAX_REF_LEN)

        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
                raise ValueError("expected_revision must be a nonnegative integer or None")

        if timestamp is not None:
            ts = _parse_iso(timestamp, "timestamp")
        else:
            ts = _now_iso()

        task_val = current_task if isinstance(current_task, (dict, list, str, int, float, bool)) else str(current_task)
        step_val = next_step if isinstance(next_step, (dict, list, str, int, float, bool)) else str(next_step)
        meta_val = metadata if isinstance(metadata, dict) else {}

        if owning_device is None and isinstance(meta_val, dict):
            owning_device = meta_val.get("owning_device") or meta_val.get("origin_device")
        if owning_device is None and isinstance(current_job, dict):
            owning_device = current_job.get("owning_device")
        if owning_device is not None:
            owning_device = _validate_bounded_text(owning_device, "owning_device", _MAX_ID_LEN)
        if active_device is not None:
            active_device = _validate_bounded_text(active_device, "active_device", _MAX_ID_LEN)

        def portable_ids(values, name):
            if values is None:
                return []
            if not isinstance(values, list):
                raise ValueError(f"{name} must be a list")
            result = []
            for value in values:
                item = _validate_bounded_text(value, name, _MAX_REF_LEN)
                if Path(item).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", item) or item.startswith("file:"):
                    raise ValueError(f"{name} cannot contain device-local paths")
                result.append(item)
            return result
        continuity = {
            "source_ids": portable_ids(source_ids, "source_ids"),
            "artifact_ids": portable_ids(artifact_ids, "artifact_ids"),
            "conversation_id": conversation_id, "activity_id": activity_id,
            "current_job": current_job, "checkpoint": checkpoint,
            "presentation_id": presentation_id, "continuation_state": continuation_state,
            "user_id": user_id,
        }
        if owning_device:
            continuity["owning_device"] = owning_device
        if active_device:
            continuity["active_device"] = active_device

        continuity = {key: value for key, value in continuity.items()
                      if value not in (None, [])}
        for key in ("conversation_id", "activity_id", "presentation_id",
                    "continuation_state", "user_id", "owning_device", "active_device"):
            if key in continuity:
                continuity[key] = _validate_bounded_text(continuity[key], key, _MAX_ID_LEN)
        _reject_nonportable(task_val, "current_task")
        _reject_nonportable(step_val, "next_step")
        _reject_nonportable(meta_val, "metadata")
        _reject_nonportable(continuity, "continuity")

        task_canon = _canonical_json(task_val)
        step_canon = _canonical_json(step_val)
        meta_canon = _canonical_json(meta_val)

        fingerprint_obj = {
            "context_id": cid,
            "current_task": task_val,
            "workspace_binding_ref": workspace_ref,
            "next_step": step_val,
            "expected_revision": expected_revision,
            "metadata": meta_val,
            "continuity": continuity,
            "owning_device": owning_device,
        }
        fingerprint = _canonical_json(fingerprint_obj)

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")

            # Check idempotency retry
            retry = db.execute(
                "SELECT * FROM idempotency_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if retry is not None:
                if retry["fingerprint"] != fingerprint:
                    raise ValueError("request_id already belongs to a different continuation request")
                row = db.execute(
                    "SELECT * FROM contexts WHERE context_id=? AND revision=?",
                    (retry["context_id"], retry["revision"]),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Inconsistent idempotency state: referenced context revision missing")
                return json.loads(row["payload"])

            # Determine revision
            row = db.execute(
                "SELECT MAX(revision) as max_rev FROM contexts WHERE context_id=?", (cid,)
            ).fetchone()
            curr_rev = row["max_rev"] if row and row["max_rev"] is not None else 0

            if curr_rev:
                prior_row = db.execute(
                    "SELECT payload FROM contexts WHERE context_id=? AND revision=?",
                    (cid, curr_rev),
                ).fetchone()
                prior_payload = json.loads(prior_row["payload"])
                prior_continuity = prior_payload.get("continuity") or {}
                prior_user = prior_continuity.get("user_id")
                if prior_user is not None and continuity.get("user_id") != prior_user:
                    raise ValueError("continuation context belongs to another user")

                prior_owner = prior_payload.get("owning_device") or prior_continuity.get("owning_device")

                # Same-job continuation fencing (evaluated before context owner check when job identity is specified)
                prior_job = prior_continuity.get("current_job")
                if prior_job and current_job:
                    prior_jid = (prior_job.get("id") or prior_job.get("job_id")) if isinstance(prior_job, dict) else str(prior_job)
                    curr_jid = (current_job.get("id") or current_job.get("job_id")) if isinstance(current_job, dict) else str(current_job)
                    if prior_jid and curr_jid and prior_jid == curr_jid:
                        prior_job_owner = (prior_job.get("owning_device") if isinstance(prior_job, dict) else None) or prior_owner
                        curr_job_owner = (current_job.get("owning_device") if isinstance(current_job, dict) else None) or owning_device
                        if prior_job_owner and curr_job_owner and prior_job_owner != curr_job_owner:
                            raise ValueError(f"job '{curr_jid}' is fenced to owning device '{prior_job_owner}'")

                if prior_owner is not None:
                    if owning_device is not None and owning_device != prior_owner:
                        raise ValueError(f"continuation context is fenced to owning device '{prior_owner}'")
                    if owning_device is None:
                        owning_device = prior_owner
                        continuity["owning_device"] = prior_owner

            if expected_revision is not None and expected_revision != curr_rev:
                raise ValueError(
                    f"Revision conflict: expected revision {expected_revision} but current is {curr_rev}"
                )

            next_rev = curr_rev + 1

            record = {
                "context_id": cid,
                "revision": next_rev,
                "current_task": task_val,
                "workspace_binding_ref": workspace_ref,
                "next_step": step_val,
                "timestamp": ts,
                "metadata": meta_val,
                "continuity": continuity,
                "request_id": request_id,
                "owning_device": owning_device,
                "active_device": active_device,
            }
            payload_str = _canonical_json(record)

            db.execute(
                """INSERT INTO contexts(context_id, revision, current_task, workspace_binding_ref, next_step, timestamp, payload, owning_device)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (cid, next_rev, task_canon, workspace_ref, step_canon, ts, payload_str, owning_device),
            )
            db.execute(
                """INSERT INTO idempotency_requests(request_id, fingerprint, context_id, revision)
                   VALUES(?,?,?,?)""",
                (request_id, fingerprint, cid, next_rev),
            )
            db.execute(
                """INSERT INTO active_context(singleton, context_id, revision)
                   VALUES(1, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET context_id=excluded.context_id, revision=excluded.revision""",
                (cid, next_rev),
            )

            return copy.deepcopy(record)

    def get_latest_context(self, context_id: str | None = None) -> dict | None:
        """Retrieve the latest revision of a specific context or the globally active one."""
        if not self.path.is_file():
            return None
        with self._db() as db:
            if context_id is not None:
                row = db.execute(
                    "SELECT payload FROM contexts WHERE context_id=? ORDER BY revision DESC LIMIT 1",
                    (context_id,),
                ).fetchone()
            else:
                active = db.execute(
                    "SELECT context_id, revision FROM active_context WHERE singleton=1"
                ).fetchone()
                if not active:
                    row = db.execute(
                        "SELECT payload FROM contexts ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                else:
                    row = db.execute(
                        "SELECT payload FROM contexts WHERE context_id=? AND revision=?",
                        (active["context_id"], active["revision"]),
                    ).fetchone()
            if row is None:
                return None
            return json.loads(row["payload"])

    def context_history(self, context_id: str) -> list[dict]:
        """Return all revisions of a continuation context in chronological order."""
        if not self.path.is_file():
            return []
        context_id = _validate_bounded_text(context_id, "context_id", _MAX_ID_LEN)
        with self._db() as db:
            rows = db.execute(
                "SELECT payload FROM contexts WHERE context_id=? ORDER BY revision ASC",
                (context_id,),
            ).fetchall()
            return [json.loads(r["payload"]) for r in rows]

    def read_continuation_snapshot(self) -> dict:
        """Read-only view without creating tables or directories if nonexistent."""
        if not self.path.is_file():
            return {"state": "not_connected", "active_context": None, "total_records": 0}
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                active = db.execute("SELECT context_id, revision FROM active_context WHERE singleton=1").fetchone()
                active_record = None
                if active:
                    row = db.execute(
                        "SELECT payload FROM contexts WHERE context_id=? AND revision=?",
                        (active["context_id"], active["revision"]),
                    ).fetchone()
                    if row:
                        active_record = json.loads(row["payload"])
                count = db.execute("SELECT COUNT(*) as cnt FROM contexts").fetchone()["cnt"]
                return {
                    "state": "ready",
                    "active_context": active_record,
                    "total_records": count,
                }
        except (sqlite3.Error, OSError, ValueError, KeyError):
            return {"state": "unavailable", "active_context": None, "total_records": 0}

    def validate_device_access(self, context_id: str, device_id: str) -> dict:
        """Validate whether device_id is permitted to continue context_id."""
        ctx = self.get_latest_context(context_id)
        if not ctx:
            return {"allowed": False, "reason": "context_not_found"}
        owner = ctx.get("owning_device") or (ctx.get("continuity") or {}).get("owning_device")
        if owner and owner != device_id:
            return {
                "allowed": False,
                "reason": f"continuation context is fenced to owning device '{owner}'",
                "owning_device": owner,
                "requesting_device": device_id,
            }
        return {"allowed": True, "context": ctx, "owning_device": owner}

