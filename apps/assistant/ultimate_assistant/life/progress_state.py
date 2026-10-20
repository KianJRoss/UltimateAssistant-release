"""Durable explicit progress evidence and a pure commitment overlay.

The ledger records what a caller explicitly reports.  It does not update the
source task database, infer learning, interpret timers, or complete tasks.
"""
from __future__ import annotations

import copy
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .paths import life_root


_MAX_ID = 512
_MAX_NOTE = 16384
_MAX_UNITS = 1000


def _bounded_text(value, name, maximum):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    if len(value) > maximum:
        raise ValueError(f"{name} is too long (maximum {maximum} characters)")
    return value


def _note(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("note must describe observed work")
    if len(value) > _MAX_NOTE:
        raise ValueError(f"note is too long (maximum {_MAX_NOTE} characters)")
    return value


def _remaining_minutes(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("remaining_minutes must be a finite nonnegative number or null")
    if (isinstance(value, float) and not math.isfinite(value)) or value < 0:
        raise ValueError("remaining_minutes must be a finite nonnegative number or null")
    try:
        result=float(value)
    except OverflowError as exc:
        raise ValueError("remaining_minutes is too large") from exc
    if not math.isfinite(result):
        raise ValueError("remaining_minutes must be finite")
    return result


def _unit_ids(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("completed_unit_ids must be a list of strings")
    if len(value) > _MAX_UNITS:
        raise ValueError(f"completed_unit_ids may contain at most {_MAX_UNITS} items")
    result = []
    seen = set()
    for item in value:
        item = _bounded_text(item, "completed unit id", _MAX_ID)
        if item in seen:
            raise ValueError("completed_unit_ids must be unique")
        seen.add(item)
        result.append(item)
    return sorted(result)


def _observed_at(value):
    if value is None:
        return None
    if not isinstance(value,str):
        raise ValueError('observed_at must be an aware ISO timestamp')
    try:
        instant=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError as exc:
        raise ValueError('observed_at must be an aware ISO timestamp') from exc
    if instant.utcoffset() is None:
        raise ValueError('observed_at requires a timezone offset')
    return instant.astimezone(timezone.utc).isoformat()


def _evidence_epoch(record):
    try:
        value=_observed_at(record.get('observed_at') or record.get('created_at'))
        return datetime.fromisoformat(value).timestamp() if value is not None else None
    except (ValueError,TypeError,OverflowError):
        return None


class ProgressStore:
    """SQLite ledger of immutable, explicitly submitted progress records."""

    def __init__(self, home=None):
        if home is None:
            home = life_root() / "progress"
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "progress.sqlite"
        with self._db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS progress_records(
                       sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                       request_id TEXT UNIQUE,
                       payload TEXT NOT NULL
                   )"""
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
    def _decode(row):
        value = json.loads(row["payload"])
        value["sequence"] = row["sequence"]
        return value

    def record(
        self, activity_id, *, note, completed_unit_ids=None,
        remaining_minutes=None, request_id, observed_at=None,
    ):
        observed_at = _observed_at(observed_at)
        activity_id = _bounded_text(activity_id, "activity_id", _MAX_ID)
        request_id = _bounded_text(request_id, "request_id", _MAX_ID)
        note = _note(note)
        completed_unit_ids = _unit_ids(completed_unit_ids)
        remaining_minutes = _remaining_minutes(remaining_minutes)
        identity = {
            "activity_id": activity_id,
            "note": note,
            "completed_unit_ids": completed_unit_ids,
            "remaining_minutes": remaining_minutes,
            "request_id": request_id,
        }
        identity_text = json.dumps(
            identity, ensure_ascii=True, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
        with self._db() as db:
            # Serialize the read/check/insert so separate connections have the
            # same retry and conflict behavior.
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT sequence,payload FROM progress_records WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if old is not None:
                old_value = self._decode(old)
                old_identity = {key: old_value[key] for key in identity}
                old_text = json.dumps(
                    old_identity, ensure_ascii=True, allow_nan=False,
                    sort_keys=True, separators=(",", ":"),
                )
                if old_text != identity_text:
                    raise ValueError("request_id already belongs to different progress evidence")
                if observed_at is not None and _observed_at(old_value.get("observed_at") or old_value["created_at"]) != observed_at:
                    raise ValueError("request_id already belongs to a different observation time")
                return old_value

            payload = dict(identity)
            payload["created_at"] = datetime.now(timezone.utc).isoformat()
            payload["observed_at"] = observed_at or payload["created_at"]
            text = json.dumps(
                payload, ensure_ascii=True, allow_nan=False, sort_keys=True,
                separators=(",", ":"),
            )
            cursor = db.execute(
                "INSERT INTO progress_records(request_id,payload) VALUES(?,?)",
                (request_id, text),
            )
            payload["sequence"] = cursor.lastrowid
            return payload

    def list_records(self, activity_id=None):
        if activity_id is not None:
            activity_id = _bounded_text(activity_id, "activity_id", _MAX_ID)
        with self._db() as db:
            rows = db.execute(
                "SELECT sequence,payload FROM progress_records ORDER BY sequence"
            ).fetchall()
        records = [self._decode(row) for row in rows]
        if activity_id is not None:
            records = [r for r in records if r.get("activity_id") == activity_id]
        return records


def apply_progress(commitments, records):
    """Return detached commitments overlaid with explicit progress evidence."""
    result = copy.deepcopy(commitments)
    evidence = copy.deepcopy(records)
    if not evidence:
        return result

    by_activity = {}
    for record in evidence:
        if isinstance(record, dict):
            by_activity.setdefault(record.get("activity_id"), []).append(record)

    for commitment in result:
        if not isinstance(commitment, dict):
            continue
        matching = by_activity.get(commitment.get("id"), [])
        if not matching:
            continue
        commitment["progress_evidence"] = matching
        commitment["source_estimated_minutes"] = commitment.get("estimated_minutes")

        if isinstance(commitment.get("units"), list):
            completed = {
                unit_id
                for record in matching
                for unit_id in record.get("completed_unit_ids", [])
            }
            remaining = 0
            for unit in commitment["units"]:
                if not isinstance(unit, dict):
                    continue
                if unit.get("id") in completed:
                    unit["completed"] = True
                if not unit.get("completed"):
                    minutes = unit.get("estimated_minutes")
                    if (isinstance(minutes, (int, float))
                            and not isinstance(minutes, bool)
                            and (not isinstance(minutes, float)
                                 or math.isfinite(minutes))):
                        remaining += minutes
            commitment["estimated_minutes"] = remaining
            commitment["remaining_effort_minutes"] = remaining
        else:
            explicit = [
                record for record in matching
                if record.get("remaining_minutes") is not None
            ]
            if explicit:
                # Recovery receipt time must not override a newer observation.
                epochs=[_evidence_epoch(record) for record in explicit]
                if all(epoch is not None for epoch in epochs):
                    latest=max(zip(explicit,epochs),key=lambda item:(item[1],item[0].get('sequence',0)))[0]
                elif all(isinstance(record.get('sequence'),int) for record in explicit):
                    latest=max(explicit,key=lambda record:record['sequence'])
                else:
                    latest=explicit[-1]
                commitment["estimated_minutes"] = latest["remaining_minutes"]
                commitment["remaining_effort_minutes"] = latest["remaining_minutes"]
    return result
