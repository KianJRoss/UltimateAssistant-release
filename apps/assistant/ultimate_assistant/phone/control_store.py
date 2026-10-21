"""Durable, transport-neutral device command/job/event authority.

The store records intent and observed outcomes.  It never probes a device or
executes an OS action.  Every mutation is transactional and emits a versioned
event; execution is fenced by an opaque lease token.
"""
from __future__ import annotations

import json
import math
import os
import platform
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


TERMINAL_STATES = frozenset(("completed", "done", "failed", "unavailable", "expired", "cancelled", "interrupted"))

NEGATIVE_COMPLETION_STATUSES = frozenset(
    ("error", "unsupported", "rejected", "failed", "expired")
)
INTERRUPTED_COMPLETION_STATUSES = frozenset(("partial", "interrupted", "lease_lost"))


def canonical_completion(result=None, error=None):
    """Return the durable state, normalized result, error, and review fence."""
    if result is not None and not isinstance(result, dict):
        raise ValueError("Command result must be an object")
    result_was_none = result is None
    normalized = dict(result or {})
    raw_status = normalized.get("status")
    status = raw_status.lower() if isinstance(raw_status, str) else raw_status
    evidence = error or normalized.get("error") or normalized.get("reason")

    if status in INTERRUPTED_COMPLETION_STATUSES:
        terminal_state, review_required = "interrupted", 1
        evidence = evidence or "Execution outcome may be partial; review before retry."
    elif status == "unavailable":
        terminal_state, review_required = "unavailable", 0
        evidence = evidence or "Execution provider is unavailable."
    elif error is not None or status in NEGATIVE_COMPLETION_STATUSES:
        terminal_state, review_required = "failed", 0
        evidence = evidence or (str(raw_status) if raw_status else "Command reported failure.")
    else:
        terminal_state, review_required = "done", 0
        evidence = None

    if terminal_state != "done" or "effects" in normalized:
        normalized.setdefault("effects", {
            "status": "possible" if review_required else "none",
            "review_required": bool(review_required),
        })
    persisted_result = None if result_was_none and terminal_state == "done" else normalized
    return terminal_state, persisted_result, (str(evidence) if evidence else None), review_required
NON_REPLAYABLE_COMMANDS = frozenset(("set_mode", "close_app", "close_apps"))


class RevisionConflict(RuntimeError):
    """The caller based a command on stale device state."""


class ControlStore:
    def __init__(self, home=None, *, clock=None, read_only=False):
        if home is None:
            from . import phone_root
            home = phone_root() / "control"
        self.home = Path(home).expanduser().resolve()
        self.path = self.home / "control.sqlite"
        self.clock = clock or time.time
        self.read_only = bool(read_only)
        if self.read_only:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            # Open now so corrupt/not-a-database state is reported at construction.
            with self._db() as db:
                db.execute("SELECT 1 FROM identity LIMIT 1").fetchone()
        else:
            self.home.mkdir(parents=True, exist_ok=True)
            self._initialize()
        self.device_id = self.identity()["device_id"]

    def _initialize(self):
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1), device_id TEXT NOT NULL, hostname TEXT NOT NULL, platform TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS capabilities(name TEXT PRIMARY KEY, available INTEGER NOT NULL, details TEXT NOT NULL, updated REAL NOT NULL, readiness TEXT NOT NULL DEFAULT 'unknown', permission TEXT NOT NULL DEFAULT 'unknown', evidence TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS device_status(singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, target_device TEXT NOT NULL, command TEXT NOT NULL, arguments TEXT NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL, owner TEXT, lease_token TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, review_required INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL, expected_revision INTEGER, correlation_id TEXT, checkpoint TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, kind TEXT NOT NULL, command_id TEXT, details TEXT NOT NULL, created REAL NOT NULL, revision INTEGER, origin_device TEXT, correlation_id TEXT, importance TEXT NOT NULL DEFAULT 'normal', retention_class TEXT NOT NULL DEFAULT 'operational');
            CREATE INDEX IF NOT EXISTS command_state ON commands(state,created);
            """)
            # Migrate databases produced by the first local ledger.
            additions = {
                "capabilities": (("readiness", "TEXT NOT NULL DEFAULT 'unknown'"), ("permission", "TEXT NOT NULL DEFAULT 'unknown'"), ("evidence", "TEXT NOT NULL DEFAULT '{}'"), ("revision", "INTEGER NOT NULL DEFAULT 1")),
                "device_status": (("revision", "INTEGER NOT NULL DEFAULT 1"),),
                "commands": (("expected_revision", "INTEGER"), ("correlation_id", "TEXT"), ("checkpoint", "TEXT"), ("cancel_requested", "INTEGER NOT NULL DEFAULT 0")),
                "events": (("event_id", "TEXT"), ("revision", "INTEGER"), ("origin_device", "TEXT"), ("correlation_id", "TEXT"), ("importance", "TEXT NOT NULL DEFAULT 'normal'"), ("retention_class", "TEXT NOT NULL DEFAULT 'operational'")),
            }
            for table, columns in additions.items():
                existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns:
                    if name not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            now = self.clock()
            db.execute("INSERT INTO identity VALUES(1,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET hostname=excluded.hostname,platform=excluded.platform,seen=excluded.seen",
                       (uuid.uuid4().hex, socket.gethostname(), platform.system(), now, now))
            device_id = db.execute("SELECT device_id FROM identity WHERE singleton=1").fetchone()[0]
            for event in db.execute("SELECT sequence FROM events WHERE event_id IS NULL OR revision IS NULL OR origin_device IS NULL ORDER BY sequence"):
                db.execute("UPDATE events SET event_id=COALESCE(event_id,?),revision=COALESCE(revision,?),origin_device=COALESCE(origin_device,?) WHERE sequence=?",
                           (uuid.uuid4().hex, event[0], device_id, event[0]))

    @contextmanager
    def _db(self):
        uri = f"file:{self.path.as_posix()}?mode=ro" if self.read_only else str(self.path)
        db = sqlite3.connect(uri, timeout=10, uri=self.read_only)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _writable(self):
        if self.read_only:
            raise PermissionError("ControlStore is read-only")

    @staticmethod
    def _json(value):
        try:
            text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Record must be JSON serializable") from exc
        if len(text.encode("utf-8")) > 65536:
            raise ValueError("Record exceeds 64 KiB")
        return text

    @staticmethod
    def _record(row):
        if row is None:
            return None
        record = dict(row)
        for key in ("arguments", "result", "checkpoint"):
            if record.get(key) is not None:
                record[key] = json.loads(record[key])
        record["review_required"] = bool(record["review_required"])
        record["cancel_requested"] = bool(record.get("cancel_requested", 0))
        return record

    def _event(self, db, kind, command_id=None, details=None, *, correlation_id=None,
               importance="normal", retention_class="operational"):
        revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM events WHERE origin_device=?", (self.device_id,)).fetchone()[0]
        db.execute("INSERT INTO events(event_id,kind,command_id,details,created,revision,origin_device,correlation_id,importance,retention_class) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex, kind, command_id, self._json(details or {}), self.clock(), revision,
                    self.device_id, correlation_id, importance, retention_class))

    def identity(self):
        with self._db() as db:
            row = db.execute("SELECT * FROM identity WHERE singleton=1").fetchone()
            if row is None:
                raise sqlite3.DatabaseError("control store has no identity")
            return dict(row)

    def set_capability(self, name, available=True, details=None, *, readiness=None,
                       permission="unknown", evidence=None):
        self._writable()
        if not isinstance(name, str) or not name or len(name) > 128:
            raise ValueError("Capability needs a short name")
        readiness = readiness or ("ready" if available else "unavailable")
        if readiness not in {"ready", "degraded", "offline", "blocked", "unavailable", "unknown"}:
            raise ValueError("Invalid readiness")
        with self._db() as db:
            old = db.execute("SELECT revision FROM capabilities WHERE name=?", (name,)).fetchone()
            revision = (old[0] + 1) if old else 1
            db.execute("INSERT INTO capabilities(name,available,details,updated,readiness,permission,evidence,revision) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET available=excluded.available,details=excluded.details,updated=excluded.updated,readiness=excluded.readiness,permission=excluded.permission,evidence=excluded.evidence,revision=excluded.revision",
                       (name, int(bool(available)), self._json(details or {}), self.clock(), readiness,
                        str(permission), self._json(evidence or {}), revision))

    def save_status(self, values, *, expected_revision=None):
        self._writable()
        if not isinstance(values, dict):
            raise ValueError("Status must be a dictionary")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT revision FROM device_status WHERE singleton=1").fetchone()
            current = old[0] if old else 0
            if expected_revision is not None and expected_revision != current:
                raise RevisionConflict(f"expected device revision {expected_revision}, found {current}")
            revision = current + 1
            db.execute("INSERT INTO device_status VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload,updated=excluded.updated,revision=excluded.revision",
                       (self._json(values), self.clock(), revision))
            self._event(db, "device_status", details={"revision": revision})
            return revision

    def record_event(self, kind, details=None, command_id=None, **metadata):
        self._writable()
        with self._db() as db:
            self._event(db, kind, command_id, details, **metadata)

    def list_events(self, limit=100, *, after_revision=None):
        with self._db() as db:
            query = "SELECT * FROM events"
            values = []
            if after_revision is not None:
                query += " WHERE revision>?"
                values.append(int(after_revision))
            query += " ORDER BY sequence DESC LIMIT ?"
            values.append(min(max(int(limit), 1), 1000))
            return [dict(row, details=json.loads(row["details"])) for row in db.execute(query, values)]

    def enqueue(self, command, arguments=None, *, target_device, idempotency_key,
                expires_at=None, expected_revision=None, correlation_id=None):
        self._writable()
        if not all(isinstance(v, str) and v and len(v) <= 256 for v in (command, target_device, idempotency_key)):
            raise ValueError("Command, device, and idempotency key must be nonempty strings")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("Arguments must be a dictionary")
        if expires_at is not None and (not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at)):
            raise ValueError("Expiry must be a finite Unix timestamp")
        if expected_revision is not None and (not isinstance(expected_revision, int) or expected_revision < 0):
            raise ValueError("Expected revision must be a nonnegative integer")
        payload = self._json(arguments or {})
        correlation_id = correlation_id or uuid.uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                supplied_expiry_matches = expires_at is None or existing["expires_at"] == expires_at
                if (existing["command"] != command or existing["target_device"] != target_device or
                    existing["arguments"] != payload or not supplied_expiry_matches or
                    existing["expected_revision"] != expected_revision):
                    raise ValueError("Idempotency key already belongs to a different command")
                return self._record(existing)
            now = self.clock()
            expires_at = now + 60 if expires_at is None else expires_at
            identifier = uuid.uuid4().hex
            db.execute("INSERT INTO commands(id,idempotency_key,target_device,command,arguments,expires_at,state,created,updated,expected_revision,correlation_id) VALUES(?,?,?,?,?,?,'queued',?,?,?,?)",
                       (identifier, idempotency_key, target_device, command, payload, expires_at, now, now, expected_revision, correlation_id))
            self._event(db, "queued", identifier, correlation_id=correlation_id)
            return self._record(db.execute("SELECT * FROM commands WHERE id=?", (identifier,)).fetchone())

    def _reconcile(self, db):
        now = self.clock()
        rows = db.execute("SELECT * FROM commands WHERE (state IN ('accepted','running') AND (lease_until<=? OR expires_at<=?)) OR (state='queued' AND expires_at<=?)", (now, now, now)).fetchall()
        for row in rows:
            if row["state"] in ("accepted", "running"):
                state, error, review = "interrupted", "Execution outcome unknown after lease/command expiry; review before retry.", 1
            else:
                state, error, review = "expired", "Command expired before execution.", 0
            db.execute("UPDATE commands SET state=?,error=?,review_required=?,lease_until=NULL,updated=? WHERE id=?", (state, error, review, now, row["id"]))
            self._event(db, state, row["id"], {"reason": error}, correlation_id=row["correlation_id"])

    def reconcile(self, *, target_device=None):
        """Expire stale work and return unresolved records for reconnect handling."""
        self._writable()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            device = target_device or self.device_id
            rows = db.execute("SELECT * FROM commands WHERE target_device=? AND state IN ('queued','interrupted') ORDER BY created", (device,)).fetchall()
            return [self._record(row) for row in rows]

    def claim(self, owner, lease_seconds=30, *, supported_families=None):
        """Atomically lease the oldest local command a worker can execute.

        ``supported_families`` is an optional collection of command names or
        dotted family prefixes (``device`` matches ``device.browser.inspect``).
        The predicate is applied while the write transaction is held so an
        incompatible worker can never consume and fail somebody else's job.
        Omitting it preserves the legacy unfiltered claim behaviour.
        """
        self._writable()
        if not isinstance(owner, str) or not owner or len(owner) > 256 or not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600:
            raise ValueError("Worker identity and a bounded positive lease are required")
        families = None
        if supported_families is not None:
            if isinstance(supported_families, str):
                supported_families = (supported_families,)
            try:
                families = tuple(dict.fromkeys(supported_families))
            except TypeError as exc:
                raise ValueError("supported_families must be a collection of command families") from exc
            if not families or any(not isinstance(item, str) or not item or len(item) > 256
                                   or item.startswith(".") or item.endswith(".") for item in families):
                raise ValueError("supported_families must contain bounded command names or prefixes")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            if db.execute("SELECT 1 FROM commands WHERE state IN ('accepted','running') AND target_device=? LIMIT 1", (self.device_id,)).fetchone():
                return None
            sql = "SELECT * FROM commands WHERE state='queued' AND target_device=? AND expires_at>?"
            parameters = [self.device_id, self.clock()]
            if families is not None:
                clauses = []
                for family in families:
                    clauses.append("(command=? OR command LIKE ? ESCAPE '\\')")
                    escaped = family.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    parameters.extend((family, escaped + ".%"))
                sql += " AND (" + " OR ".join(clauses) + ")"
            row = db.execute(sql + " ORDER BY created,rowid LIMIT 1", parameters).fetchone()
            if row is None:
                return None
            current = db.execute("SELECT revision FROM device_status WHERE singleton=1").fetchone()
            current_revision = current[0] if current else 0
            if row["expected_revision"] is not None and row["expected_revision"] != current_revision:
                message = f"Expected device revision {row['expected_revision']}, found {current_revision}."
                db.execute("UPDATE commands SET state='failed',error=?,updated=? WHERE id=?", (message, self.clock(), row["id"]))
                self._event(db, "revision_conflict", row["id"], {"expected": row["expected_revision"], "actual": current_revision}, correlation_id=row["correlation_id"])
                return None
            now = self.clock()
            token = uuid.uuid4().hex
            db.execute("UPDATE commands SET state='accepted',owner=?,lease_token=?,lease_until=?,attempts=attempts+1,updated=? WHERE id=?", (owner, token, min(now + lease_seconds, row["expires_at"]), now, row["id"]))
            self._event(db, "accepted", row["id"], {"owner": owner}, correlation_id=row["correlation_id"])
            return self._record(db.execute("SELECT * FROM commands WHERE id=?", (row["id"],)).fetchone())

    def heartbeat(self, id, owner, lease_token, *, lease_seconds=30, checkpoint=None, running=True):
        self._writable()
        if not isinstance(lease_seconds, (int, float)) or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid lease duration")
        encoded = None if checkpoint is None else self._json(checkpoint)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            row = db.execute("SELECT * FROM commands WHERE id=?", (id,)).fetchone()
            if not row or row["owner"] != owner or row["lease_token"] != lease_token or row["state"] not in ("accepted", "running") or row["cancel_requested"]:
                return False
            now = self.clock()
            until = min(now + lease_seconds, row["expires_at"])
            db.execute("UPDATE commands SET state=?,lease_until=?,checkpoint=COALESCE(?,checkpoint),updated=? WHERE id=?", ("running" if running else row["state"], until, encoded, now, id))
            self._event(db, "checkpoint" if checkpoint is not None else "heartbeat", id, {"owner": owner}, correlation_id=row["correlation_id"])
            return True

    def complete(self, id, owner, lease_token, result=None, error=None):
        self._writable()
        terminal_state, result, error, review_required = canonical_completion(result, error)
        encoded = self._json(result)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            now = self.clock()
            changed = db.execute("UPDATE commands SET state=?,result=?,error=?,review_required=?,lease_until=NULL,updated=? WHERE id=? AND owner=? AND lease_token=? AND state IN ('accepted','running') AND cancel_requested=0 AND lease_until>? AND expires_at>?", (terminal_state, encoded, str(error) if error else None, review_required, now, id, owner, lease_token, now, now)).rowcount
            if changed:
                row = db.execute("SELECT correlation_id FROM commands WHERE id=?", (id,)).fetchone()
                details = {"effects": result["effects"]} if result and "effects" in result else None
                self._event(db, "completed" if terminal_state == "done" else terminal_state,
                            id, details, correlation_id=row[0])
            return bool(changed)

    def retry(self, id, *, expires_at=None):
        self._writable()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            row = db.execute("SELECT * FROM commands WHERE id=?", (id,)).fetchone()
            if row is None or row["state"] not in {"interrupted", "failed", "cancelled", "expired", "unavailable"} or row["target_device"] != self.device_id:
                return False
            if row["command"] in NON_REPLAYABLE_COMMANDS and row["state"] in {"interrupted", "expired"}:
                # A fresh idempotency key is required for time-sensitive effects.
                return False
            expires = row["expires_at"] if expires_at is None else expires_at
            if not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= self.clock():
                raise ValueError("Explicit retry requires an expiry in the future")
            db.execute("UPDATE commands SET state='queued',expires_at=?,owner=NULL,lease_token=NULL,lease_until=NULL,review_required=0,result=NULL,error=NULL,cancel_requested=0,updated=? WHERE id=?", (expires, self.clock(), id))
            self._event(db, "retry_requested", id, {"previous_state": row["state"], "checkpoint_preserved": row["checkpoint"] is not None}, correlation_id=row["correlation_id"])
            return True

    def interrupt(self, id, owner, lease_token, reason, *, result=None):
        self._writable()
        encoded = self._json(result) if result is not None else None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            row = db.execute("SELECT * FROM commands WHERE id=?", (id,)).fetchone()
            if row is None or row["owner"] != owner or row["lease_token"] != lease_token or row["state"] not in {"accepted", "running", "interrupted"}:
                return False
            if row["state"] == "interrupted" and encoded is not None:
                # Original lease identity can reconcile late evidence, never replace history.
                if row["result"] is not None:
                    return row["result"] == encoded
                db.execute("UPDATE commands SET result=?,updated=? WHERE id=?", (encoded, self.clock(), id))
                self._event(db, "interrupted_evidence", id, {"owner": owner})
            if row["state"] != "interrupted":
                db.execute("UPDATE commands SET state='interrupted',review_required=1,error=?,result=COALESCE(?,result),lease_until=NULL,updated=? WHERE id=?", (str(reason), encoded, self.clock(), id))
                self._event(db, "interrupted", id, {"reason": str(reason), "owner": owner}, correlation_id=row["correlation_id"])
            return True

    def cancel(self, id):
        self._writable()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile(db)
            row = db.execute("SELECT * FROM commands WHERE id=?", (id,)).fetchone()
            if row is None or row["state"] not in {"queued", "accepted", "running", "interrupted"}:
                return False
            ambiguous = row["state"] in {"accepted", "running", "interrupted"}
            db.execute("UPDATE commands SET state='cancelled',cancel_requested=1,review_required=?,error=?,lease_until=NULL,updated=? WHERE id=?", (int(ambiguous), "Cancellation cannot undo an action already executing." if ambiguous else None, self.clock(), id))
            self._event(db, "cancelled", id, {"execution_may_have_occurred": ambiguous}, correlation_id=row["correlation_id"])
            return True

    def get_command(self, id):
        with self._db() as db:
            if not self.read_only:
                db.execute("BEGIN IMMEDIATE")
                self._reconcile(db)
            return self._record(db.execute("SELECT * FROM commands WHERE id=?", (id,)).fetchone())

    def list_commands(self, limit=100):
        with self._db() as db:
            if not self.read_only:
                db.execute("BEGIN IMMEDIATE")
                self._reconcile(db)
            return [self._record(row) for row in db.execute("SELECT * FROM commands ORDER BY created DESC,rowid DESC LIMIT ?", (min(max(int(limit), 1), 1000),))]

    def read_status(self):
        with self._db() as db:
            if not self.read_only:
                db.execute("BEGIN IMMEDIATE")
                self._reconcile(db)
            status = db.execute("SELECT * FROM device_status WHERE singleton=1").fetchone()
            capabilities = []
            for row in db.execute("SELECT * FROM capabilities ORDER BY name"):
                item = dict(row)
                item.update(details=json.loads(row["details"]), evidence=json.loads(row["evidence"]), available=bool(row["available"]))
                capabilities.append(item)
            return {"identity": dict(db.execute("SELECT * FROM identity WHERE singleton=1").fetchone()), "capabilities": capabilities,
                    "status": json.loads(status["payload"]) if status else {}, "status_updated": status["updated"] if status else None,
                    "revision": status["revision"] if status else 0,
                    "command_counts": {row["state"]: row["count"] for row in db.execute("SELECT state,COUNT(*) AS count FROM commands GROUP BY state")},
                    "review_required": db.execute("SELECT COUNT(*) FROM commands WHERE review_required=1").fetchone()[0]}
