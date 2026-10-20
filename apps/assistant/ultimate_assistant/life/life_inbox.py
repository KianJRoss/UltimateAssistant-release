"""Durable everyday life capture over routines and task units.

Captures chores (via RoutineStore), estimates (via TaskUnitStore),
and appointments, going-away/return estimates (away windows), and optional
hobby next actions in a thin SQLite table with request_id idempotency, revisions,
and read-only snapshots returning ready/not_connected/unavailable without creating files.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .routine_store import RoutineConflict, RoutineStore
from .task_units import TaskUnitStore
from .paths import life_root


class LifeConflict(ValueError):
    """Raised on stale revisions, changed idempotency payloads, or conflicting mutations."""
    pass


_MAX_ID_LEN = 256
_MAX_TITLE_LEN = 500
_MAX_NOTES_LEN = 8000
_MAX_LOCATION_LEN = 2000
_MAX_HOBBY_NAME_LEN = 200
_MAX_NEXT_ACTION_LEN = 2000
_MAX_CONTEXT_LEN = 256

_VALID_RECORD_KINDS = {"appointment", "away_window", "hobby_action"}


def _validate_bounded_text(value, name: str, max_len: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} cannot be empty or whitespace only")
    if len(value) > max_len:
        raise ValueError(f"{name} exceeds maximum length of {max_len}")
    return value


def _parse_aware_utc(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty ISO 8601 string")
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a valid ISO 8601 datetime") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dt_from_utc_str(s: str) -> datetime:
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


class LifeInbox:
    """Everyday life capture store backed by RoutineStore, TaskUnitStore, and a thin SQLite table."""

    def __init__(self, home=None, *, routine_home=None, unit_home=None, clock=time.time):
        if home is None:
            home = life_root() / "life"
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / "life.sqlite"
        self.clock = clock

        # Store delegates
        # RoutineStore and TaskUnitStore do not create files on construction if not written,
        # but TaskUnitStore.__init__ actually creates directories if called!
        # Wait: does TaskUnitStore.__init__ create directory? Let's check:
        # In task_units.py:
        # self.home.mkdir(parents=True, exist_ok=True)
        # self.path = self.home / "task-units.sqlite"
        # with self._db() as db:
        # ... it creates tables on __init__!
        # BUT LifeInbox construction MUST NOT create files or directories if routine_home/unit_home
        # are managed lazily, or we delay instantiating TaskUnitStore until needed,
        # OR we pass routine_store / unit_store when mutating!
        if routine_home is not None:
            self.routine_home = Path(routine_home).expanduser().resolve()
        elif home is not None:
            self.routine_home = self.home.parent / "routines"
        else:
            self.routine_home = None

        if unit_home is not None:
            self.unit_home = Path(unit_home).expanduser().resolve()
        elif home is not None:
            self.unit_home = self.home.parent / "task-units"
        else:
            self.unit_home = None

        self._routine_store = None
        self._unit_store = None

    def _get_routine_store(self) -> RoutineStore:
        if self._routine_store is None:
            if self.routine_home:
                self._routine_store = RoutineStore(self.routine_home, clock=self.clock)
            else:
                self._routine_store = RoutineStore(clock=self.clock)
        return self._routine_store

    def _get_unit_store(self) -> TaskUnitStore:
        if self._unit_store is None:
            if self.unit_home:
                self._unit_store = TaskUnitStore(self.unit_home)
            else:
                self._unit_store = TaskUnitStore()
        return self._unit_store

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z")

    def _ensure_storage_dir(self):
        self.home.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _write_db(self):
        self._ensure_storage_dir()
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("PRAGMA synchronous=FULL")
            self._ensure_tables(db)
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            db.close()

    def _ensure_tables(self, db: sqlite3.Connection):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS life_items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                scheduled_start TEXT,
                scheduled_end TEXT,
                location TEXT NOT NULL,
                notes TEXT NOT NULL,
                details_json TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_life_items_kind_start ON life_items(kind, scheduled_start);
            CREATE TABLE IF NOT EXISTS request_ledger (
                request_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                item_id TEXT NOT NULL,
                canonical_payload TEXT NOT NULL,
                response_record TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

    @staticmethod
    def _format_record(row_dict: dict) -> dict:
        return {
            "id": row_dict["id"],
            "kind": row_dict["kind"],
            "title": row_dict["title"],
            "scheduled_start": row_dict["scheduled_start"],
            "scheduled_end": row_dict["scheduled_end"],
            "location": row_dict["location"],
            "notes": row_dict["notes"],
            "details": json.loads(row_dict["details_json"]),
            "status": row_dict["status"],
            "revision": int(row_dict["revision"]),
            "created_at": row_dict["created_at"],
            "updated_at": row_dict["updated_at"],
        }

    # --- Delegated Chore / Routine methods ---

    def capture_chore(
        self,
        title: str,
        interval_days: int,
        estimated_minutes: int,
        next_due_at: str,
        *,
        notes: str = "",
        request_id: str,
    ) -> dict:
        """Capture a recurring chore via RoutineStore."""
        store = self._get_routine_store()
        return store.create(
            title=title,
            interval_days=interval_days,
            estimated_minutes=estimated_minutes,
            next_due_at=next_due_at,
            notes=notes,
            request_id=request_id,
        )

    def complete_chore(
        self,
        routine_id: str,
        *,
        revision: int,
        request_id: str,
        completed_at: str | None = None,
    ) -> dict:
        """Complete a recurring chore via RoutineStore."""
        store = self._get_routine_store()
        return store.complete(
            routine_id=routine_id,
            revision=revision,
            request_id=request_id,
            completed_at=completed_at,
        )

    # --- Delegated Task Unit / Estimate methods ---

    def define_estimates(
        self,
        activity_id: str,
        units: list[dict],
        *,
        request_id: str,
        expected_revision: int | None = None,
        provenance: str = "",
    ) -> dict:
        """Define granular estimated units for an activity via TaskUnitStore."""
        store = self._get_unit_store()
        return store.define(
            activity_id=activity_id,
            units=units,
            request_id=request_id,
            expected_revision=expected_revision,
            provenance=provenance,
        )

    # --- Thin SQLite table: Appointments, Away Windows, Hobby Next Actions ---

    def capture_appointment(
        self,
        title: str,
        starts_at: str,
        *,
        ends_at: str | None = None,
        location: str = "",
        notes: str = "",
        request_id: str,
    ) -> dict:
        """Capture an appointment with aware UTC start and optional end/location."""
        title = _validate_bounded_text(title, "title", _MAX_TITLE_LEN)
        starts_at_iso = _parse_aware_utc(starts_at, "starts_at")
        ends_at_iso = _parse_aware_utc(ends_at, "ends_at") if ends_at else None
        if ends_at_iso and _dt_from_utc_str(ends_at_iso) < _dt_from_utc_str(starts_at_iso):
            raise ValueError("ends_at cannot be earlier than starts_at")

        location = _validate_bounded_text(location, "location", _MAX_LOCATION_LEN, allow_empty=True)
        notes = _validate_bounded_text(notes, "notes", _MAX_NOTES_LEN, allow_empty=True)
        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)

        details = {}
        return self._mutate_create(
            kind="appointment",
            title=title,
            scheduled_start=starts_at_iso,
            scheduled_end=ends_at_iso,
            location=location,
            notes=notes,
            details=details,
            request_id=request_id,
        )

    def capture_away_window(
        self,
        *,
        destination: str = "",
        starts_at: str,
        expected_return: str | None = None,
        notes: str = "",
        request_id: str,
    ) -> dict:
        """Capture a going-away window with optional destination and return estimate."""
        destination = _validate_bounded_text(destination, "destination", _MAX_LOCATION_LEN, allow_empty=True)
        starts_at_iso = _parse_aware_utc(starts_at, "starts_at")
        expected_return_iso = _parse_aware_utc(expected_return, "expected_return") if expected_return else None
        if expected_return_iso and _dt_from_utc_str(expected_return_iso) < _dt_from_utc_str(starts_at_iso):
            raise ValueError("expected_return cannot be earlier than starts_at")

        notes = _validate_bounded_text(notes, "notes", _MAX_NOTES_LEN, allow_empty=True)
        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)

        title = f"Away: {destination}" if destination else "Away"
        details = {
            "destination": destination,
            "actual_return": None,
        }
        return self._mutate_create(
            kind="away_window",
            title=title,
            scheduled_start=starts_at_iso,
            scheduled_end=expected_return_iso,
            location=destination,
            notes=notes,
            details=details,
            request_id=request_id,
        )

    def record_return(
        self,
        item_id: str,
        *,
        revision: int,
        returned_at: str | None = None,
        request_id: str,
    ) -> dict:
        """Record return from an away window."""
        item_id = _validate_bounded_text(item_id, "item_id", _MAX_ID_LEN)
        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("revision must be a positive integer")

        returned_at_iso = _parse_aware_utc(returned_at, "returned_at") if returned_at else self._now()

        payload_obj = {
            "returned_at": returned_at_iso,
            "revision": revision,
        }
        canonical_payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"))

        with self._write_db() as db:
            ledger_row = db.execute(
                "SELECT * FROM request_ledger WHERE request_id = ?", (request_id,)
            ).fetchone()
            if ledger_row is not None:
                if ledger_row["canonical_payload"] != canonical_payload or ledger_row["action"] != "record_return":
                    raise LifeConflict(f"request_id '{request_id}' already used with different payload or action")
                return json.loads(ledger_row["response_record"])

            row = db.execute("SELECT * FROM life_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(f"Life item '{item_id}' not found")

            if row["kind"] != "away_window":
                raise ValueError(f"Life item '{item_id}' is not an away_window")

            if row["revision"] != revision:
                raise LifeConflict(
                    f"Conflict on item '{item_id}': current revision is {row['revision']}, expected {revision}"
                )

            if row["scheduled_start"] and _dt_from_utc_str(returned_at_iso) < _dt_from_utc_str(row["scheduled_start"]):
                raise ValueError("returned_at cannot be earlier than starts_at")

            now_iso = self._now()
            new_revision = revision + 1
            details = json.loads(row["details_json"])
            details["actual_return"] = returned_at_iso
            details_json = json.dumps(details, sort_keys=True, separators=(",", ":"))

            db.execute(
                """UPDATE life_items SET
                    details_json = ?,
                    status = 'completed',
                    revision = ?,
                    updated_at = ?
                WHERE id = ?""",
                (details_json, new_revision, now_iso, item_id),
            )

            updated_dict = {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "scheduled_start": row["scheduled_start"],
                "scheduled_end": row["scheduled_end"],
                "location": row["location"],
                "notes": row["notes"],
                "details": details,
                "status": "completed",
                "revision": new_revision,
                "created_at": row["created_at"],
                "updated_at": now_iso,
            }
            resp_json = json.dumps(updated_dict, sort_keys=True, separators=(",", ":"))
            db.execute(
                """INSERT INTO request_ledger (
                    request_id, action, item_id, canonical_payload, response_record, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, "record_return", item_id, canonical_payload, resp_json, now_iso),
            )
            return updated_dict

    def capture_hobby_action(
        self,
        hobby_name: str,
        next_action: str,
        *,
        estimated_minutes: int | float | None = None,
        context: str | None = None,
        target_date: str | None = None,
        notes: str = "",
        request_id: str,
    ) -> dict:
        """Capture an optional hobby next-action."""
        hobby_name = _validate_bounded_text(hobby_name, "hobby_name", _MAX_HOBBY_NAME_LEN)
        next_action = _validate_bounded_text(next_action, "next_action", _MAX_NEXT_ACTION_LEN)
        target_date_iso = _parse_aware_utc(target_date, "target_date") if target_date else None
        notes = _validate_bounded_text(notes, "notes", _MAX_NOTES_LEN, allow_empty=True)
        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)

        if estimated_minutes is not None:
            if isinstance(estimated_minutes, bool) or not isinstance(estimated_minutes, (int, float)):
                raise ValueError("estimated_minutes must be a number")
            if not math.isfinite(estimated_minutes):
                raise ValueError("estimated_minutes must be finite")
            if estimated_minutes <= 0:
                raise ValueError("estimated_minutes must be greater than zero")

        if context is not None:
            context = _validate_bounded_text(context, "context", _MAX_CONTEXT_LEN)

        title = f"[{hobby_name}] {next_action}"
        details = {
            "hobby_name": hobby_name,
            "next_action": next_action,
        }
        if estimated_minutes is not None:
            details["estimated_minutes"] = estimated_minutes
        if context is not None:
            details["context"] = context
        return self._mutate_create(
            kind="hobby_action",
            title=title,
            scheduled_start=target_date_iso,
            scheduled_end=None,
            location="",
            notes=notes,
            details=details,
            request_id=request_id,
        )

    def update_item_status(
        self,
        item_id: str,
        *,
        status: str,
        revision: int,
        request_id: str,
    ) -> dict:
        """Update status of a life item (e.g. 'completed', 'active', 'cancelled')."""
        item_id = _validate_bounded_text(item_id, "item_id", _MAX_ID_LEN)
        status = _validate_bounded_text(status, "status", 64)
        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("revision must be a positive integer")

        payload_obj = {
            "status": status,
            "revision": revision,
        }
        canonical_payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"))

        with self._write_db() as db:
            ledger_row = db.execute(
                "SELECT * FROM request_ledger WHERE request_id = ?", (request_id,)
            ).fetchone()
            if ledger_row is not None:
                if ledger_row["canonical_payload"] != canonical_payload or ledger_row["action"] != "update_status":
                    raise LifeConflict(f"request_id '{request_id}' already used with different payload or action")
                return json.loads(ledger_row["response_record"])

            row = db.execute("SELECT * FROM life_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(f"Life item '{item_id}' not found")

            if row["revision"] != revision:
                raise LifeConflict(
                    f"Conflict on item '{item_id}': current revision is {row['revision']}, expected {revision}"
                )

            now_iso = self._now()
            new_revision = revision + 1

            db.execute(
                """UPDATE life_items SET
                    status = ?,
                    revision = ?,
                    updated_at = ?
                WHERE id = ?""",
                (status, new_revision, now_iso, item_id),
            )

            updated_dict = {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "scheduled_start": row["scheduled_start"],
                "scheduled_end": row["scheduled_end"],
                "location": row["location"],
                "notes": row["notes"],
                "details": json.loads(row["details_json"]),
                "status": status,
                "revision": new_revision,
                "created_at": row["created_at"],
                "updated_at": now_iso,
            }
            resp_json = json.dumps(updated_dict, sort_keys=True, separators=(",", ":"))
            db.execute(
                """INSERT INTO request_ledger (
                    request_id, action, item_id, canonical_payload, response_record, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, "update_status", item_id, canonical_payload, resp_json, now_iso),
            )
            return updated_dict

    def _mutate_create(
        self,
        *,
        kind: str,
        title: str,
        scheduled_start: str | None,
        scheduled_end: str | None,
        location: str,
        notes: str,
        details: dict,
        request_id: str,
    ) -> dict:
        if kind not in _VALID_RECORD_KINDS:
            raise ValueError(f"Invalid kind '{kind}'")

        payload_obj = {
            "kind": kind,
            "title": title,
            "scheduled_start": scheduled_start,
            "scheduled_end": scheduled_end,
            "location": location,
            "notes": notes,
            "details": details,
        }
        canonical_payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"))

        with self._write_db() as db:
            ledger_row = db.execute(
                "SELECT * FROM request_ledger WHERE request_id = ?", (request_id,)
            ).fetchone()
            if ledger_row is not None:
                if ledger_row["canonical_payload"] != canonical_payload or ledger_row["action"] != "create":
                    raise LifeConflict(f"request_id '{request_id}' already used with different payload or action")
                return json.loads(ledger_row["response_record"])

            item_id = uuid.uuid4().hex
            now_iso = self._now()
            details_json = json.dumps(details, sort_keys=True, separators=(",", ":"))
            status = "active"

            db.execute(
                """INSERT INTO life_items (
                    id, kind, title, scheduled_start, scheduled_end, location,
                    notes, details_json, status, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    kind,
                    title,
                    scheduled_start,
                    scheduled_end,
                    location,
                    notes,
                    details_json,
                    status,
                    1,
                    now_iso,
                    now_iso,
                ),
            )

            created_dict = {
                "id": item_id,
                "kind": kind,
                "title": title,
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
                "location": location,
                "notes": notes,
                "details": copy.deepcopy(details),
                "status": status,
                "revision": 1,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            resp_json = json.dumps(created_dict, sort_keys=True, separators=(",", ":"))
            db.execute(
                """INSERT INTO request_ledger (
                    request_id, action, item_id, canonical_payload, response_record, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, "create", item_id, canonical_payload, resp_json, now_iso),
            )
            return created_dict

    # --- Read-only Snapshot methods ---

    def snapshot_items(self, *, kind: str | None = None, now=None, limit: int = 100) -> dict:
        """Read-only snapshot of life items without side effects or creating files."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 10000:
            raise ValueError("limit must be an integer between 0 and 10000")
        if kind is not None and kind not in _VALID_RECORD_KINDS:
            raise ValueError(f"Invalid kind '{kind}'")

        if now is None:
            as_of = self._now()
        elif isinstance(now, datetime):
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("now must be timezone-aware")
            as_of = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            as_of = _parse_aware_utc(now, "now")

        value = {"state": "not_connected", "as_of": as_of, "items": [], "total": 0, "omitted": 0}

        db = None
        try:
            if not self.path.exists():
                return value
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")

            if kind is None:
                total = db.execute("SELECT COUNT(*) FROM life_items").fetchone()[0]
                rows = db.execute(
                    """SELECT * FROM life_items ORDER BY
                    scheduled_start IS NULL,
                    scheduled_start,
                    id LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                total = db.execute("SELECT COUNT(*) FROM life_items WHERE kind = ?", (kind,)).fetchone()[0]
                rows = db.execute(
                    """SELECT * FROM life_items WHERE kind = ? ORDER BY
                    scheduled_start IS NULL,
                    scheduled_start,
                    id LIMIT ?""",
                    (kind, limit),
                ).fetchall()

            items = []
            for row in rows:
                rec = self._format_record(dict(row))
                if rec["kind"] == "away_window":
                    # Classify return state: returned, overdue, or unknown/open
                    actual_return = rec["details"].get("actual_return")
                    exp_return = rec["scheduled_end"]
                    if actual_return or rec["status"] == "completed":
                        return_status = "returned"
                    elif exp_return is None:
                        return_status = "unknown_return"
                    else:
                        try:
                            overdue = _dt_from_utc_str(exp_return) < _dt_from_utc_str(as_of)
                            return_status = "overdue_return" if overdue else "away"
                        except Exception:
                            return_status = "unknown_return"
                    rec["return_status"] = return_status
                items.append(rec)
            value.update(state="ready", items=items, total=total, omitted=max(0, total - len(items)))
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            value.update(state="unavailable", items=[], total=0, omitted=0)
        finally:
            if db is not None:
                db.close()
        return value

    def snapshot(self, *, kind: str | None = None, now=None, limit: int = 100) -> dict:
        """Cohesive read-only snapshot of life inbox (chores, items, estimates) without creating files."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 10000:
            raise ValueError("limit must be an integer between 0 and 10000")

        as_of = self._now() if now is None else (_parse_aware_utc(now, "now") if isinstance(now, str) else now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))

        # 1. Snapshot thin items
        items_snap = self.snapshot_items(kind=kind, now=as_of, limit=limit)

        # 2. Snapshot routines/chores
        routine_store = RoutineStore(self.routine_home, clock=self.clock) if self.routine_home else (
            RoutineStore(clock=self.clock)
        )
        if routine_store is not None:
            chores_snap = routine_store.snapshot(now=as_of, limit=limit)
        else:
            chores_snap = {"state": "not_connected", "as_of": as_of, "items": [], "total": 0, "omitted": 0}

        # 3. Snapshot task units / estimates
        from .task_units import read_definitions
        unit_records, unit_status = read_definitions(self.unit_home)

        # Determine composite state:
        # If any is unavailable, state is 'unavailable'.
        # If any is ready, state is 'ready'.
        # If all not_connected, state is 'not_connected'.
        sub_states = [items_snap["state"], chores_snap["state"], unit_status["state"]]
        if "unavailable" in sub_states:
            overall_state = "unavailable"
        elif "ready" in sub_states:
            overall_state = "ready"
        else:
            overall_state = "not_connected"

        return {
            "state": overall_state,
            "as_of": as_of,
            "items": items_snap["items"],
            "total_items": items_snap["total"],
            "chores": chores_snap,
            "task_units": {
                "state": unit_status["state"],
                "records": unit_records[:limit],
                "total": unit_status["records"],
            },
        }
