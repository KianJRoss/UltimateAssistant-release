"""Durable recurring-routine storage with SQLite ledger and deterministic due metadata."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid
import time
from .paths import life_root


class RoutineConflict(ValueError):
    """Raised on stale revisions, changed idempotency payloads, or conflicting mutations."""
    pass


_MAX_ID_LEN = 256
_MAX_TITLE_LEN = 200
_MAX_NOTES_LEN = 4000
_MAX_INTERVAL_DAYS = 3660
_MAX_ESTIMATED_MINUTES = 1440


def _validate_bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _validate_positive_int(value, name: str, max_val: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be strictly positive")
    if value > max_val:
        raise ValueError(f"{name} exceeds maximum allowed value ({max_val})")
    return value


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


def _compute_next_due(completed_at_str: str, interval_days: int) -> str:
    """Elapsed 24-hour days from actual completion, including early/late work."""
    return (_dt_from_utc_str(completed_at_str)+timedelta(days=interval_days)).isoformat().replace('+00:00','Z')


def _validate_minutes(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)):
        raise ValueError('estimated_minutes must be numeric')
    if not 0<value<=_MAX_ESTIMATED_MINUTES or not math.isfinite(value):
        raise ValueError('estimated_minutes must be finite and between 0 and 1440')
    return float(value)


class RoutineStore:
    """Durable recurring-routine storage backed by SQLite."""

    def __init__(self, home=None, *, clock=time.time):
        if home is None:
            home = life_root() / "routines"
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / "routines.sqlite"
        self.clock = clock
        # Note: Construction MUST NOT create directories or database file.

    def _now(self):
        return datetime.fromtimestamp(self.clock(),timezone.utc).isoformat().replace("+00:00","Z")

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
            CREATE TABLE IF NOT EXISTS routines (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                interval_days INTEGER NOT NULL,
                estimated_minutes REAL NOT NULL,
                next_due_at TEXT NOT NULL,
                active INTEGER NOT NULL,
                notes TEXT NOT NULL,
                revision INTEGER NOT NULL,
                last_completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_ledger (
                request_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                routine_id TEXT NOT NULL,
                canonical_payload TEXT NOT NULL,
                response_record TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

    @staticmethod
    def _format_record(row_dict: dict) -> dict:
        return {
            "id": row_dict["id"],
            "title": row_dict["title"],
            "interval_days": int(row_dict["interval_days"]),
            "estimated_minutes": float(row_dict["estimated_minutes"]),
            "next_due_at": row_dict["next_due_at"],
            "active": bool(row_dict["active"]),
            "notes": row_dict["notes"],
            "revision": int(row_dict["revision"]),
            "last_completed_at": row_dict["last_completed_at"],
            "created_at": row_dict["created_at"],
            "updated_at": row_dict["updated_at"],
        }

    def create(
        self,
        title: str,
        interval_days: int,
        estimated_minutes: int,
        next_due_at: str,
        *,
        notes: str = "",
        request_id: str,
    ) -> dict:
        # Bounded validation
        title = _validate_bounded_text(title, "title", _MAX_TITLE_LEN)
        interval_days = _validate_positive_int(interval_days, "interval_days", _MAX_INTERVAL_DAYS)
        estimated_minutes = _validate_minutes(estimated_minutes)
        next_due_at = _parse_aware_utc(next_due_at, "next_due_at")
        notes = _validate_bounded_text(notes, "notes", _MAX_NOTES_LEN, allow_empty=True)
        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)

        payload_obj = {
            "title": title,
            "interval_days": interval_days,
            "estimated_minutes": estimated_minutes,
            "next_due_at": next_due_at,
            "notes": notes,
        }
        canonical_payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"))

        with self._write_db() as db:
            # Check request ledger
            ledger_row = db.execute(
                "SELECT * FROM request_ledger WHERE request_id = ?", (request_id,)
            ).fetchone()
            if ledger_row is not None:
                if ledger_row["canonical_payload"] != canonical_payload or ledger_row["action"] != "create":
                    raise RoutineConflict(f"request_id '{request_id}' already used with different payload or action")
                return json.loads(ledger_row["response_record"])

            routine_id = uuid.uuid4().hex
            now_iso = self._now()
            db.execute(
                """INSERT INTO routines (
                    id, title, interval_days, estimated_minutes, next_due_at,
                    active, notes, revision, last_completed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    routine_id,
                    title,
                    interval_days,
                    estimated_minutes,
                    next_due_at,
                    1,  # active
                    notes,
                    1,  # revision
                    None,
                    now_iso,
                    now_iso,
                ),
            )
            created_dict = {
                "id": routine_id,
                "title": title,
                "interval_days": interval_days,
                "estimated_minutes": estimated_minutes,
                "next_due_at": next_due_at,
                "active": True,
                "notes": notes,
                "revision": 1,
                "last_completed_at": None,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            resp_json = json.dumps(created_dict, sort_keys=True, separators=(",", ":"))
            db.execute(
                """INSERT INTO request_ledger (
                    request_id, action, routine_id, canonical_payload, response_record, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, "create", routine_id, canonical_payload, resp_json, now_iso),
            )
            return created_dict

    def complete(
        self,
        routine_id: str,
        revision: int,
        *,
        completed_at: str | None = None,
        request_id: str,
    ) -> dict:
        routine_id = _validate_bounded_text(routine_id, "routine_id", _MAX_ID_LEN)
        revision = _validate_positive_int(revision, "revision", 2**63 - 1)
        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)

        explicit_completed_at = None
        if completed_at is not None:
            explicit_completed_at = _parse_aware_utc(completed_at, "completed_at")

        payload_obj = {
            "routine_id": routine_id,
            "revision": revision,
            "completed_at": explicit_completed_at,
        }
        canonical_payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"))

        with self._write_db() as db:
            # Check request ledger BEFORE sampling clock if completed_at is None
            ledger_row = db.execute(
                "SELECT * FROM request_ledger WHERE request_id = ?", (request_id,)
            ).fetchone()
            if ledger_row is not None:
                if ledger_row["canonical_payload"] != canonical_payload or ledger_row["action"] != "complete":
                    raise RoutineConflict(f"request_id '{request_id}' already used with different payload or action")
                return json.loads(ledger_row["response_record"])

            routine_row = db.execute("SELECT * FROM routines WHERE id = ?", (routine_id,)).fetchone()
            if routine_row is None:
                raise KeyError(f"Routine '{routine_id}' not found")

            current_dict = dict(routine_row)
            if current_dict["revision"] != revision:
                raise RoutineConflict(
                    f"Stale revision: expected {revision}, current is {current_dict['revision']}"
                )

            # Sample clock now if completed_at was None
            final_completed_at = explicit_completed_at if explicit_completed_at is not None else self._now()

            if current_dict['last_completed_at'] is not None and _dt_from_utc_str(final_completed_at)<_dt_from_utc_str(current_dict['last_completed_at']):
                raise ValueError('completed_at cannot precede prior last_completed_at')
            if _dt_from_utc_str(final_completed_at)>_dt_from_utc_str(self._now())+timedelta(seconds=300):
                raise ValueError('completed_at cannot be more than 300 seconds in the future')
            new_next_due=_compute_next_due(final_completed_at,current_dict['interval_days'])

            new_revision = current_dict["revision"] + 1
            now_iso = self._now()

            db.execute(
                """UPDATE routines SET
                    next_due_at = ?,
                    revision = ?,
                    last_completed_at = ?,
                    updated_at = ?
                WHERE id = ?""",
                (new_next_due, new_revision, final_completed_at, now_iso, routine_id),
            )

            updated_dict = {
                "id": current_dict["id"],
                "title": current_dict["title"],
                "interval_days": current_dict["interval_days"],
                "estimated_minutes": current_dict["estimated_minutes"],
                "next_due_at": new_next_due,
                "active": bool(current_dict["active"]),  # Paused routines remain paused
                "notes": current_dict["notes"],
                "revision": new_revision,
                "last_completed_at": final_completed_at,
                "created_at": current_dict["created_at"],
                "updated_at": now_iso,
            }
            resp_json = json.dumps(updated_dict, sort_keys=True, separators=(",", ":"))
            db.execute(
                """INSERT INTO request_ledger (
                    request_id, action, routine_id, canonical_payload, response_record, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, "complete", routine_id, canonical_payload, resp_json, now_iso),
            )
            return updated_dict

    def set_active(
        self,
        routine_id: str,
        revision: int,
        *,
        active: bool,
        request_id: str,
    ) -> dict:
        routine_id = _validate_bounded_text(routine_id, "routine_id", _MAX_ID_LEN)
        active = _validate_bool(active, "active")
        revision = _validate_positive_int(revision, "revision", 2**63 - 1)
        request_id = _validate_bounded_text(request_id, "request_id", _MAX_ID_LEN)

        payload_obj = {
            "routine_id": routine_id,
            "active": active,
            "revision": revision,
        }
        canonical_payload = json.dumps(payload_obj, sort_keys=True, separators=(",", ":"))

        with self._write_db() as db:
            ledger_row = db.execute(
                "SELECT * FROM request_ledger WHERE request_id = ?", (request_id,)
            ).fetchone()
            if ledger_row is not None:
                if ledger_row["canonical_payload"] != canonical_payload or ledger_row["action"] != "set_active":
                    raise RoutineConflict(f"request_id '{request_id}' already used with different payload or action")
                return json.loads(ledger_row["response_record"])

            routine_row = db.execute("SELECT * FROM routines WHERE id = ?", (routine_id,)).fetchone()
            if routine_row is None:
                raise KeyError(f"Routine '{routine_id}' not found")

            current_dict = dict(routine_row)
            if current_dict["revision"] != revision:
                raise RoutineConflict(
                    f"Stale revision: expected {revision}, current is {current_dict['revision']}"
                )

            # Preserve next_due_at on pause/resume
            new_revision = current_dict["revision"] + 1
            now_iso = self._now()

            db.execute(
                """UPDATE routines SET
                    active = ?,
                    revision = ?,
                    updated_at = ?
                WHERE id = ?""",
                (int(active), new_revision, now_iso, routine_id),
            )

            updated_dict = {
                "id": current_dict["id"],
                "title": current_dict["title"],
                "interval_days": current_dict["interval_days"],
                "estimated_minutes": current_dict["estimated_minutes"],
                "next_due_at": current_dict["next_due_at"],  # Preserved
                "active": active,
                "notes": current_dict["notes"],
                "revision": new_revision,
                "last_completed_at": current_dict["last_completed_at"],
                "created_at": current_dict["created_at"],
                "updated_at": now_iso,
            }
            resp_json = json.dumps(updated_dict, sort_keys=True, separators=(",", ":"))
            db.execute(
                """INSERT INTO request_ledger (
                    request_id, action, routine_id, canonical_payload, response_record, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, "set_active", routine_id, canonical_payload, resp_json, now_iso),
            )
            return updated_dict

    def snapshot(self, *, now=None, limit=100):
        """Bounded read-only snapshot; construction/reads never create storage."""
        if isinstance(limit,bool) or not isinstance(limit,int) or not 0<=limit<=10000:
            raise ValueError('limit must be an integer between 0 and 10000')
        if now is None:
            as_of=self._now()
        elif isinstance(now,datetime):
            if now.tzinfo is None or now.utcoffset() is None:raise ValueError('now must be aware')
            as_of=now.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
        else:
            as_of=_parse_aware_utc(now,'now')
        value={'state':'not_connected','as_of':as_of,'items':[],'total':0,'omitted':0}
        db=None
        try:
            if not self.path.exists():return value
            db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            # Keep count and bounded page consistent under simultaneous writes.
            db.execute('BEGIN')
            total=db.execute('SELECT COUNT(*) FROM routines').fetchone()[0]
            # UTC ISO strings omit zero fractions, so raw lexical order reverses
            # whole seconds and fractional seconds. Compare padded microseconds
            # separately to preserve exact chronology without julianday rounding.
            rows=db.execute("""SELECT * FROM routines ORDER BY
                substr(next_due_at,1,19),
                CASE WHEN substr(next_due_at,20,1)='.'
                  THEN substr(replace(substr(next_due_at,21),'Z','')||'000000',1,6)
                  ELSE '000000' END,id LIMIT ?""",(limit,)).fetchall()
            items=[]
            for row in rows:
                item=self._format_record(dict(row))
                due=item['active'] and _dt_from_utc_str(item['next_due_at'])<=_dt_from_utc_str(as_of)
                item.update(due=due,due_status='paused' if not item['active'] else 'due' if due else 'upcoming')
                items.append(item)
            value.update(state='ready',items=items,total=total,omitted=max(0,total-len(items)))
        except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):
            value.update(state='unavailable',items=[],total=0,omitted=0)
        finally:
            if db is not None:db.close()
        return value
