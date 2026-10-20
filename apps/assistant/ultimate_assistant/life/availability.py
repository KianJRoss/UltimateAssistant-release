"""Durable, local records of personal outings.

``AvailabilityStore(home=None)`` stores one SQLite database in *home*.  With no
explicit home it uses
``life_root()/availability/availability.sqlite``.
Construction creates nothing until a record is written;
Importing this
module has no filesystem side effects.

``record_outing`` records intent only.  Destination is optional, an unknown
planned return is represented by ``expected_return=None``, and a later actual
return is kept separately in ``returned_at``.  Timestamps are timezone-aware
ISO 8601 strings and are normalized to UTC.  A request ID makes creation
retries durable and idempotent, including retries whose start time was
originally omitted.  The store does not cancel, reschedule, infer durations,
change device modes, or perform runtime actions.
"""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .paths import life_root


_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
READINESS_STATES = frozenset(("ready", "degraded", "offline", "blocked", "unavailable", "unknown"))


def readiness(state, *, permission="unknown", evidence=None, checked_at=None):
    """Return an honest, serializable readiness observation without probing."""
    if state not in READINESS_STATES:
        raise ValueError("invalid readiness state")
    if state == "ready" and not evidence:
        raise ValueError("ready state requires evidence")
    return {"state": state, "permission": str(permission), "evidence": evidence or {},
            "checked_at": checked_at}


def _text(value, field, maximum, *, empty=True):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if (not empty and not value) or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} is empty or too long")
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{field} contains invalid characters")
    return value


def _timestamp(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
        offset = parsed.utcoffset()
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp") from None
    if offset is None:
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class AvailabilityStore:
    """SQLite-backed outing records; constructing the store initializes it."""

    def __init__(self, home=None):
        if home is None:
            home = life_root() / "availability"
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "availability.sqlite"
        with self._db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS outings(
                    id TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    expected_return TEXT,
                    notes TEXT NOT NULL,
                    request_id TEXT UNIQUE,
                    start_was_omitted INTEGER NOT NULL,
                    returned_at TEXT
                )
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record(row):
        if row is None:
            return None
        return {key: row[key] for key in (
            "id", "destination", "starts_at", "expected_return", "notes",
            "request_id", "returned_at",
        )}

    def record_outing(self, destination="", *, starts_at=None,
                      expected_return=None, notes="", request_id=None):
        destination = _text(destination, "destination", 4096)
        notes = _text(notes, "notes", 65536)
        if request_id is not None:
            request_id = _text(request_id, "request_id", 256, empty=False)
            if not request_id.strip():
                raise ValueError("request_id cannot be blank")
        omitted = starts_at is None
        supplied_start = None if omitted else _timestamp(starts_at, "starts_at")
        planned = None if expected_return is None else _timestamp(expected_return, "expected_return")
        if supplied_start is not None and planned is not None and _instant(planned) < _instant(supplied_start):
            raise ValueError("expected_return cannot be before starts_at")

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_id is not None:
                old = db.execute("SELECT * FROM outings WHERE request_id=?", (request_id,)).fetchone()
                if old is not None:
                    same_start = (bool(old["start_was_omitted"]) == omitted and
                                  (omitted or old["starts_at"] == supplied_start))
                    if (old["destination"] != destination or old["notes"] != notes or
                            old["expected_return"] != planned or not same_start):
                        raise ValueError("request_id already belongs to a different outing request")
                    return self._record(old)

            start = _now() if omitted else supplied_start
            if planned is not None and _instant(planned) < _instant(start):
                raise ValueError("expected_return cannot be before starts_at")
            identifier = uuid.uuid4().hex
            db.execute("""
                INSERT INTO outings(id,destination,starts_at,expected_return,notes,
                                    request_id,start_was_omitted,returned_at)
                VALUES(?,?,?,?,?,?,?,NULL)
            """, (identifier, destination, start, planned, notes, request_id, int(omitted)))
            return self._record(db.execute("SELECT * FROM outings WHERE id=?", (identifier,)).fetchone())

    def list_outings(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM outings ORDER BY starts_at, rowid").fetchall()
            return [self._record(row) for row in rows]

    def return_from_outing(self, outing_id, *, returned_at=None):
        if not isinstance(outing_id, str) or _ID_RE.fullmatch(outing_id) is None:
            raise ValueError("outing_id must be a valid outing identifier")
        omitted = returned_at is None
        actual = None if omitted else _timestamp(returned_at, "returned_at")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outings WHERE id=?", (outing_id,)).fetchone()
            if row is None:
                raise KeyError(outing_id)
            if row["returned_at"] is not None and omitted:
                return self._record(row)
            if omitted:
                actual = _now()
            if _instant(actual) < _instant(row["starts_at"]):
                raise ValueError("returned_at cannot be before starts_at")
            if row["returned_at"] is not None:
                if _instant(row["returned_at"]) != _instant(actual):
                    raise ValueError("outing already has a different actual return")
                return self._record(row)
            db.execute("UPDATE outings SET returned_at=? WHERE id=?", (actual, outing_id))
            return self._record(db.execute("SELECT * FROM outings WHERE id=?", (outing_id,)).fetchone())
