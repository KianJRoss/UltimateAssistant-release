"""Phone-local delivery facade over the authoritative ControlStore."""
from __future__ import annotations

import time
import json
import uuid
import math
import numbers

from . import DEFAULT_DEVICE
from .control_store import TERMINAL_STATES

_MISSING = object()


class PhoneCommandService:
    def __init__(self, control_store, pairing_store, *, device_id=DEFAULT_DEVICE, clock=None, registry=None):
        self.control = control_store
        self.pairing = pairing_store
        self.device_id = device_id
        self.clock = clock or time.time
        self.registry = registry
        self._spoken_messages: set[str] = set()

    def enqueue(self, command, arguments, *, idempotency_key, expires_at,
                pairing_revision, expected_revision=None, correlation_id=None):
        self.pairing.authorize("conversation", self.device_id,
                               expected_pairing_revision=pairing_revision)
        if (isinstance(expires_at, bool) or not isinstance(expires_at, numbers.Real)
                or not math.isfinite(expires_at)):
            raise ValueError("phone command expiry must be a finite Unix timestamp")
        if expires_at <= self.clock():
            raise ValueError("phone command expiry must be in the future")
        payload = dict(arguments or {})

        # Owning-device fence check
        owning = payload.get("owning_device")
        if owning and owning != self.device_id:
            raise ValueError(f"phone command is fenced to owning device '{owning}'")

        # Same-job continuation fencing
        curr_job = payload.get("current_job")
        if isinstance(curr_job, dict):
            job_owner = curr_job.get("owning_device")
            if job_owner and job_owner != self.device_id:
                raise ValueError(f"phone command job is fenced to owning device '{job_owner}'")

        payload.update(pairing_revision=pairing_revision,
                       protocol="phone.v1")
        return self.control.enqueue(
            command, payload, target_device=self.device_id,
            idempotency_key=idempotency_key, expires_at=expires_at,
            expected_revision=expected_revision, correlation_id=correlation_id)

    def pending(self, *, pairing_revision):
        self.pairing.authorize("conversation", self.device_id,
                               expected_pairing_revision=pairing_revision)
        if self.registry and hasattr(self.registry, "record_presence"):
            try:
                self.registry.record_presence(self.device_id, state="active", last_active=self.clock(),
                                              interaction_type="phone_poll")
            except Exception:
                pass
        now = self.clock()
        rejected = []
        with self.control._db() as db:
            db.execute("BEGIN IMMEDIATE")
            stale_rows = db.execute(
                "SELECT id FROM commands WHERE target_device=? AND (state='expired' OR (state='queued' AND expires_at<=?))",
                (self.device_id, now)
            ).fetchall()
            for r in stale_rows:
                rejected.append(r["id"])
            self.control._reconcile(db)
            rows = [self.control._record(r) for r in db.execute(
                "SELECT * FROM commands WHERE target_device=? AND state IN ('queued','interrupted') ORDER BY created",
                (self.device_id,)
            ).fetchall()]
        deliverable = []
        for row in rows:
            # Authenticated host ADB owns this family; never send it to APK dispatch.
            if row["command"] == "phone.adb" or row["command"].startswith("phone.adb."):
                continue
            args = row.get("arguments") or {}
            msg_id = args.get("message_id") or args.get("record_id")
            if row["command"] == "phone.announcement" and msg_id and msg_id in self._spoken_messages:
                if row["id"] not in rejected:
                    rejected.append(row["id"])
                continue
            if row["expires_at"] <= now or args.get("pairing_revision") != pairing_revision:
                if row["id"] not in rejected:
                    rejected.append(row["id"])
            elif row["state"] == "queued":
                deliverable.append(row)
        return {"commands": deliverable, "rejected_stale": rejected,
                "delivery": "at_least_once"}

    def accept(self, command_id, *, owner, pairing_revision, lease_seconds=30, expected_family="native"):
        self.pairing.authorize("conversation", self.device_id,
                               expected_pairing_revision=pairing_revision)
        if (not isinstance(owner, str) or not owner or len(owner) > 256
                or isinstance(lease_seconds, bool)
                or not isinstance(lease_seconds, numbers.Real)
                or not math.isfinite(lease_seconds)
                or not 1 <= lease_seconds <= 3600):
            raise ValueError("bounded owner and lease are required")
        if expected_family not in {"native", "phone.adb"}:
            raise ValueError("unsupported phone execution family")
        # Family is selected by the server-owned caller, never an HTTP owner alias.
        # This is the same transaction and schema used by ControlStore.claim;
        # selecting by id lets an HTTP phone worker accept only what it received.
        with self.control._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self.control._reconcile(db)
            row = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            if (not row or row["target_device"] != self.device_id or row["state"] != "queued"
                    or row["expires_at"] <= self.clock()
                    or row["arguments"] is None):
                return None
            is_host_adb = row["command"] == "phone.adb" or row["command"].startswith("phone.adb.")
            if is_host_adb != (expected_family == "phone.adb"):
                return None
            if db.execute("SELECT 1 FROM commands WHERE state IN ('accepted','running') AND target_device=? LIMIT 1",
                          (self.device_id,)).fetchone():
                return None
            arguments = self.control._record(row)["arguments"]
            if arguments.get("pairing_revision") != pairing_revision:
                return None
            current = db.execute("SELECT revision FROM device_status WHERE singleton=1").fetchone()
            current_revision = current[0] if current else 0
            if row["expected_revision"] is not None and row["expected_revision"] != current_revision:
                message = f"Expected device revision {row['expected_revision']}, found {current_revision}."
                db.execute("UPDATE commands SET state='failed',error=?,updated=? WHERE id=?",
                           (message, self.clock(), command_id))
                self.control._event(db, "revision_conflict", command_id,
                                    {"expected": row["expected_revision"], "actual": current_revision},
                                    correlation_id=row["correlation_id"])
                return None
            now, token = self.clock(), uuid.uuid4().hex
            db.execute("UPDATE commands SET state='accepted',owner=?,lease_token=?,lease_until=?,"
                       "attempts=attempts+1,updated=? WHERE id=?",
                       (owner, token, min(now + lease_seconds, row["expires_at"]), now, command_id))
            self.control._event(db, "accepted", command_id, {"owner": owner},
                                correlation_id=row["correlation_id"])
            if self.registry and hasattr(self.registry, "record_presence"):
                try:
                    self.registry.record_presence(self.device_id, state="active", last_active=now,
                                                  interaction_type="command_accepted")
                except Exception:
                    pass
            return self.control._record(
                db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())

    def heartbeat(self, command_id, *, owner, lease_token, checkpoint=None,
                  lease_seconds=30, running=True):
        return self.control.heartbeat(command_id, owner, lease_token,
                                      checkpoint=checkpoint,
                                      lease_seconds=lease_seconds, running=running)

    def acknowledge(self, command_id, *, owner, lease_token, result=_MISSING, error=_MISSING,
                    pairing_revision):
        self.pairing.authorize("conversation", self.device_id,
                               expected_pairing_revision=pairing_revision)
        if (result is _MISSING) == (error is _MISSING):
            raise ValueError("exactly one of result or error is required")
        current = self.control.get_command(command_id)
        if not current or current.get("target_device") != self.device_id:
            raise KeyError("unknown phone command")

        # Track spoken announcement to prevent duplicate speech
        if current.get("command") == "phone.announcement":
            args = current.get("arguments") or {}
            msg_id = args.get("message_id") or args.get("record_id")
            if msg_id:
                self._spoken_messages.add(msg_id)

        # Android failure envelopes retain evidence while the authoritative job stays failed.
        retained_result = None if result is _MISSING else result
        retained_error = None if error is _MISSING else error
        if isinstance(retained_error, dict):
            if len(json.dumps(retained_error, allow_nan=False).encode('utf-8')) > 65536:
                raise ValueError('phone error evidence exceeds 64 KiB')
            if retained_error.get('status') != 'failed' or not isinstance(retained_error.get('error'), str):
                raise ValueError('structured phone error must describe a failed outcome')
            retained_result = retained_error
            retained_error = retained_error['error'].strip() or 'Android command failed without an error message'
        if error is not _MISSING and not retained_error:
            retained_error = 'Android command failed without an error message'
        details = retained_result.get('result') if isinstance(retained_result, dict) else None
        unknown = isinstance(retained_result, dict) and (
            retained_result.get('outcome_unknown') is True or
            (isinstance(details, dict) and details.get('outcome_unknown') is True))
        if current.get("state") in TERMINAL_STATES:
            if unknown and current["state"] == "interrupted":
                changed = self.control.interrupt(command_id, owner, lease_token,
                    retained_error or 'Android execution outcome unknown after dispatch', result=retained_result)
                return {"acknowledged": changed, "duplicate": current.get("result") is not None,
                        "command": self.control.get_command(command_id)}
            if current["state"] == "done":
                with self.control._db() as db:
                    db.execute("UPDATE commands SET state='completed' WHERE id=? AND state='done'", (command_id,))
                current = self.control.get_command(command_id)
            return {"acknowledged": True, "duplicate": True, "command": current}
        if unknown:
            changed = self.control.interrupt(command_id, owner, lease_token,
                retained_error or 'Android execution outcome unknown after dispatch', result=retained_result)
        else:
            changed = self.control.complete(
                command_id, owner, lease_token,
                result=retained_result,
                error=retained_error)
            if changed and (error is _MISSING or not retained_error):
                with self.control._db() as db:
                    db.execute("UPDATE commands SET state='completed' WHERE id=? AND state='done'", (command_id,))
        return {"acknowledged": changed, "duplicate": False,
                "command": self.control.get_command(command_id)}

    def cancel(self, command_id):
        changed = self.control.cancel(command_id)
        return {"accepted": changed, "command": self.control.get_command(command_id),
                "execution_may_have_occurred": bool(
                    changed and self.control.get_command(command_id).get("review_required"))}

    def record_voice_interaction(self, *, session_id: str, prompt: str | None = None,
                                 interaction_type: str = "voice") -> dict:
        """Record native voice session presence on the phone without interrupting working voice lifecycle."""
        now = self.clock()
        if self.registry and hasattr(self.registry, "record_presence"):
            try:
                self.registry.record_presence(
                    self.device_id,
                    state="active",
                    last_active=now,
                    interaction_type=interaction_type,
                    details={"session_id": session_id, "has_prompt": bool(prompt)},
                )
            except Exception:
                pass
        return {
            "device_id": self.device_id,
            "session_id": session_id,
            "presence": "active",
            "recorded_at": now,
        }
