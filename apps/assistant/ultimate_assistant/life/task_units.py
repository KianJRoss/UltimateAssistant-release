"""Immutable, explicitly supplied task-unit definitions."""
from __future__ import annotations

import copy
import json
import math
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .paths import life_root


MAX_ID = 512
MAX_LABEL = 16384
MAX_PROVENANCE = 16384
MAX_UNITS = 500
MAX_DEFINITION_BYTES = 1024 * 1024
_RECORD_KEYS = {
    "activity_id", "revision", "units", "provenance", "request_id", "created_at"
}


def _text(value, name, maximum, *, allow_empty=False):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > maximum:
        raise ValueError(f"{name} is too long (maximum {maximum} characters)")
    return value


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("revision must be a positive integer")
    return value


def _validate_units(value):
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_UNITS:
        raise ValueError(f"units must contain from 1 to {MAX_UNITS} items")
    result = []
    seen = set()
    for unit in value:
        if not isinstance(unit, dict) or set(unit) != {"id", "label", "estimated_minutes"}:
            raise ValueError("each unit must contain exactly id, label, and estimated_minutes")
        identifier = _text(unit["id"], "unit id", MAX_ID)
        label = _text(unit["label"], "unit label", MAX_LABEL)
        if identifier in seen:
            raise ValueError("unit ids must be unique")
        seen.add(identifier)
        minutes = unit["estimated_minutes"]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
            raise ValueError("estimated_minutes must be a positive finite number")
        try:
            minutes = float(minutes)
        except OverflowError as exc:
            raise ValueError("estimated_minutes must be a positive finite number") from exc
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError("estimated_minutes must be a positive finite number")
        result.append({"id": identifier, "label": label, "estimated_minutes": minutes})
    if not math.isfinite(sum(unit["estimated_minutes"] for unit in result)):
        raise ValueError("aggregate estimated_minutes must be finite")
    _bounded_json(result)
    return result


def _validate_record(value):
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise ValueError("invalid task-unit record")
    record = {
        "activity_id": _text(value["activity_id"], "activity_id", MAX_ID),
        "revision": _revision(value["revision"]),
        "units": _validate_units(value["units"]),
        "provenance": _text(value["provenance"], "provenance", MAX_PROVENANCE,
                            allow_empty=True),
        "request_id": _text(value["request_id"], "request_id", MAX_ID),
        "created_at": _text(value["created_at"], "created_at", 128),
    }
    try:
        instant = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an aware ISO timestamp") from exc
    if instant.utcoffset() is None:
        raise ValueError("created_at must be an aware ISO timestamp")
    _bounded_json(record)
    return record


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _bounded_json(value):
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > MAX_DEFINITION_BYTES:
        raise ValueError("serialized task-unit definition exceeds 1 MiB")
    return encoded


def _indexed_record(row):
    if len(row["payload"].encode("utf-8")) > MAX_DEFINITION_BYTES:
        raise ValueError("serialized task-unit definition exceeds 1 MiB")
    record = _validate_record(json.loads(row["payload"]))
    if (record["activity_id"] != row["activity_id"] or
            record["revision"] != row["revision"] or
            record["request_id"] != row["indexed_request_id"]):
        raise ValueError("stored task-unit index does not match its payload")
    return record


class TaskUnitStore:
    """SQLite store retaining every revision of a task's unit definition."""

    def __init__(self, home=None):
        if home is None:
            home = life_root() / "task-units"
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "task-units.sqlite"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS definitions(
                    activity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(activity_id, revision)
                );
                CREATE TABLE IF NOT EXISTS definition_requests(
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    activity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def define(self, activity_id, units, *, request_id, expected_revision=None,
               provenance=""):
        activity_id = _text(activity_id, "activity_id", MAX_ID)
        request_id = _text(request_id, "request_id", MAX_ID)
        provenance = _text(provenance, "provenance", MAX_PROVENANCE, allow_empty=True)
        units = _validate_units(units)
        if (expected_revision is not None and
                (isinstance(expected_revision, bool) or
                 not isinstance(expected_revision, int) or expected_revision < 0)):
            raise ValueError("expected_revision must be a nonnegative integer or null")
        fingerprint = _bounded_json({"activity_id": activity_id, "units": units,
                             "expected_revision": expected_revision,
                             "provenance": provenance})
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            retry = db.execute(
                "SELECT * FROM definition_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if retry is not None:
                if retry["fingerprint"] != fingerprint:
                    raise ValueError("request_id already belongs to a different definition request")
                row = db.execute(
                    "SELECT d.activity_id,d.revision,d.payload,r.request_id AS indexed_request_id "
                    "FROM definitions d LEFT JOIN definition_requests r "
                    "ON r.activity_id=d.activity_id AND r.revision=d.revision "
                    "WHERE d.activity_id=? AND d.revision=?",
                    (retry["activity_id"], retry["revision"]),
                ).fetchone()
                if row is None:
                    raise ValueError("stored task-unit request is inconsistent")
                record = _indexed_record(row)
                if (record["activity_id"] != activity_id or record["request_id"] != request_id or
                        record["units"] != units or record["provenance"] != provenance):
                    raise ValueError("stored task-unit replay does not match its request")
                return record

            row = db.execute(
                "SELECT MAX(revision) AS revision FROM definitions WHERE activity_id=?",
                (activity_id,),
            ).fetchone()
            current = row["revision"]
            if current is None:
                if expected_revision not in (None, 0):
                    raise ValueError("expected_revision does not match current revision")
                revision = 1
            else:
                if expected_revision != current:
                    raise ValueError("expected_revision does not match current revision")
                revision = current + 1
            record = {"activity_id": activity_id, "revision": revision,
                      "units": units, "provenance": provenance,
                      "request_id": request_id,
                      "created_at": datetime.now(timezone.utc).isoformat()}
            db.execute("INSERT INTO definitions VALUES(?,?,?)",
                       (activity_id, revision, _bounded_json(record)))
            db.execute("INSERT INTO definition_requests VALUES(?,?,?,?)",
                       (request_id, fingerprint, activity_id, revision))
            return copy.deepcopy(record)

    def history(self, activity_id):
        activity_id = _text(activity_id, "activity_id", MAX_ID)
        with self._db() as db:
            rows = db.execute(
                "SELECT d.activity_id,d.revision,d.payload,r.request_id AS indexed_request_id "
                "FROM definitions d LEFT JOIN definition_requests r "
                "ON r.activity_id=d.activity_id AND r.revision=d.revision "
                "WHERE d.activity_id=? ORDER BY d.revision",
                (activity_id,),
            ).fetchall()
        return [_indexed_record(row) for row in rows]

    def snapshot(self, activity_id=None):
        if activity_id is not None:
            activity_id = _text(activity_id, "activity_id", MAX_ID)
        with self._db() as db:
            rows = db.execute("""
                SELECT d.activity_id,d.revision,d.payload,r.request_id AS indexed_request_id FROM definitions d
                JOIN (SELECT activity_id, MAX(revision) revision FROM definitions
                      GROUP BY activity_id) latest
                  ON latest.activity_id=d.activity_id AND latest.revision=d.revision
                LEFT JOIN definition_requests r ON r.activity_id=d.activity_id AND r.revision=d.revision
                WHERE (? IS NULL OR d.activity_id=?) ORDER BY d.activity_id
            """, (activity_id, activity_id)).fetchall()
        return [_indexed_record(row) for row in rows]


def apply_definitions(commitments, definitions):
    """Deep-copy commitments and overlay the latest matching definitions."""
    result = copy.deepcopy(commitments)
    latest = {}
    for candidate in copy.deepcopy(definitions):
        if not isinstance(candidate, dict):
            continue
        activity = candidate.get("activity_id")
        revision = candidate.get("revision")
        if (isinstance(activity, str) and isinstance(revision, int) and
                not isinstance(revision, bool) and
                (activity not in latest or revision > latest[activity]["revision"])):
            latest[activity] = candidate
    for commitment in result:
        if not isinstance(commitment, dict):
            continue
        definition = latest.get(commitment.get("id"))
        if definition is not None:
            commitment["units"] = copy.deepcopy(definition["units"])
            commitment["unit_definition_revision"] = definition["revision"]
            commitment["unit_definition_provenance"] = definition["provenance"]
    return result


def read_definitions(home=None):
    """Read current definitions without creating directories or database files."""
    if home is None:
        home = life_root() / "task-units"
    path = Path(home).expanduser().resolve() / "task-units.sqlite"
    if not path.is_file():
        return [], {"state": "not_connected", "records": 0}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            rows = db.execute("""
                SELECT d.activity_id,d.revision,d.payload,r.request_id AS indexed_request_id FROM definitions d
                JOIN (SELECT activity_id,MAX(revision) revision FROM definitions
                      GROUP BY activity_id) latest
                  ON latest.activity_id=d.activity_id AND latest.revision=d.revision
                LEFT JOIN definition_requests r ON r.activity_id=d.activity_id AND r.revision=d.revision
                ORDER BY d.activity_id
            """).fetchall()
        records = []
        for row in rows:
            records.append(_indexed_record(row))
        return records, {"state": "ready", "records": len(records)}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return [], {"state": "unavailable", "records": 0}
