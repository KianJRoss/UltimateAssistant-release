"""The phone's durable view of the assistant's conversation: queued requests, replies, events and uploads."""
from __future__ import annotations

import json
import math
import numbers
import os
import sqlite3
import time
import uuid
import hashlib
import re
import threading
import inspect
from contextlib import contextmanager
from pathlib import Path

from . import DEFAULT_DEVICE, conversation_id as default_conversation


class ConversationConflict(RuntimeError):
    pass


class UploadTooLarge(ValueError):
    pass


_WORKERS = {}
_WORKERS_LOCK = threading.Lock()


class MobileConversation:
    """Persist mobile history/events while delegating replies to the same assistant."""

    def __init__(self, assistant, handoff_store, home=None, *, clock=None):
        if home is None:
            from . import phone_root
            home = phone_root() / "mobile"
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "conversation.sqlite"
        self.assistant = assistant
        self.handoffs = handoff_store
        self.clock = clock or time.time
        self.voice = None
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS messages(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
              conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
              attachments TEXT NOT NULL, continuation_id TEXT, continuation_revision INTEGER,
              correlation_id TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              revision INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
              conversation_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
              created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(
              request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
              assistant_message_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proactive_deliveries(
              delivery_key TEXT PRIMARY KEY, message_id TEXT NOT NULL,
              conversation_id TEXT NOT NULL, record_id TEXT NOT NULL,
              revision INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS presentation_deliveries(
              presentation_id TEXT NOT NULL, revision INTEGER NOT NULL,
              conversation_id TEXT NOT NULL, event_revision INTEGER,
              payload TEXT, created REAL NOT NULL,
              PRIMARY KEY(presentation_id,revision));
            CREATE INDEX IF NOT EXISTS mobile_message_thread ON messages(conversation_id,sequence);
            CREATE INDEX IF NOT EXISTS mobile_event_thread ON events(conversation_id,revision);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            for name, definition in {
                "envelope": "TEXT", "state": "TEXT NOT NULL DEFAULT 'completed'",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt": "REAL NOT NULL DEFAULT 0", "error": "TEXT",
                "session_id": "TEXT", "correlation_id": "TEXT",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE requests ADD COLUMN {name} {definition}")
            message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "request_id" not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN request_id TEXT")
                db.execute("""UPDATE messages SET request_id=(
                    SELECT request_id FROM requests
                    WHERE requests.assistant_message_id=messages.message_id)
                    WHERE message_id IN (SELECT assistant_message_id FROM requests)""")
                # Recover historical user linkage only when the durable request
                # identifies a single matching user message and correlation.
                for request in db.execute("SELECT request_id,envelope FROM requests"):
                    envelope = json.loads(request["envelope"] or "{}")
                    conversation_id = envelope.get("conversation_id")
                    correlation_id = envelope.get("correlation_id")
                    if not conversation_id or not correlation_id:
                        continue
                    matches = db.execute("""SELECT message_id FROM messages
                        WHERE conversation_id=? AND correlation_id=? AND role='user'""",
                        (conversation_id, correlation_id)).fetchall()
                    owners = db.execute("SELECT request_id FROM requests WHERE correlation_id=?",
                                        (correlation_id,)).fetchall()
                    if len(matches) == 1 and len(owners) == 1:
                        db.execute("UPDATE messages SET request_id=? WHERE message_id=?",
                                   (request["request_id"], matches[0]["message_id"]))
                for event in db.execute("SELECT revision,payload FROM events WHERE kind='conversation.message'"):
                    payload = json.loads(event["payload"])
                    message = db.execute("SELECT request_id FROM messages WHERE message_id=?",
                                         (payload.get("message_id"),)).fetchone()
                    if message:
                        payload["request_id"] = message["request_id"]
                        db.execute("UPDATE events SET payload=? WHERE revision=?",
                                   (json.dumps(payload, ensure_ascii=False, sort_keys=True), event["revision"]))
            presentation_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(presentation_deliveries)")}
            if "payload" not in presentation_columns:
                db.execute("ALTER TABLE presentation_deliveries ADD COLUMN payload TEXT")
        self._start_worker()

    def _start_worker(self):
        key = str(self.path.resolve())
        with _WORKERS_LOCK:
            if key in _WORKERS:
                self._worker = _WORKERS[key]
                self._wake = self._worker["wake"]
                return
            self._wake = threading.Event()
            self._worker = {"wake": self._wake, "stop": threading.Event(),
                            "events_changed": threading.Condition()}
            _WORKERS[key] = self._worker
            with self._db() as db:
                db.execute("UPDATE requests SET state='queued' WHERE state='running'")
            thread = threading.Thread(target=self._work, name="mobile-conversation", daemon=True)
            self._worker["thread"] = thread
            thread.start()

    def close(self, *, timeout=10):
        """Stop the queue shared by all instances for this storage path.

        Accepted queued requests remain durable for the next worker. An active
        response finishes first; a timeout retains its record to prevent a
        second worker from opening the same queue.
        """
        worker = self._worker
        worker["stop"].set()
        worker["wake"].set()
        with worker["events_changed"]:
            worker["events_changed"].notify_all()
        worker["thread"].join(timeout)
        if worker["thread"].is_alive():
            raise TimeoutError("Mobile conversation worker is still finishing an active response")
        with _WORKERS_LOCK:
            key = str(self.path.resolve())
            if _WORKERS.get(key) is worker:
                del _WORKERS[key]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _work(self):
        while not self._worker["stop"].is_set():
            row = None
            try:
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    candidate = db.execute(
                        "SELECT request_id FROM requests "
                        "WHERE state='queued' AND next_attempt<=? ORDER BY rowid LIMIT 1",
                        (self.clock(),)).fetchone()
                    if candidate:
                        db.execute(
                            "UPDATE requests SET state='running',attempts=attempts+1 "
                            "WHERE request_id=? AND state='queued'",
                            (candidate["request_id"],))
                        # Use the claimed durable row below.  In particular, its
                        # attempt count includes this claim and must not lag the
                        # value exposed by requests().
                        row = db.execute("SELECT * FROM requests WHERE request_id=?",
                                         (candidate["request_id"],)).fetchone()
                if row:
                    self._respond_saved(dict(row))
                    continue
            except Exception:
                # A temporary storage failure must not kill the only queue worker.
                if row is not None:
                    try:
                        with self._db() as db:
                            db.execute("UPDATE requests SET state='queued',next_attempt=? WHERE request_id=? AND state='running'",
                                       (self.clock() + 5, row["request_id"]))
                    except Exception:
                        pass
            self._wake.wait(5)
            self._wake.clear()

    def _respond_saved(self, row):
        envelope = json.loads(row["envelope"])
        activity_id = envelope.get("activity_id") or row["request_id"]
        session_id = envelope.get("session_id") or row.get("session_id")
        correlation_id = envelope.get("correlation_id") or row.get("correlation_id")
        def progress(value):
            self._request_event(envelope["conversation_id"], row["request_id"], activity_id,
                                value["state"], value["label"], value.get("tool_name"),
                                session_id=session_id, correlation_id=correlation_id)
        progress({"state": "preparing", "label": "Preparing assistant context"})
        try:
            if "activity_id" in inspect.signature(self.assistant.respond).parameters:
                extra = {}
                if 'origin_device' in inspect.signature(self.assistant.respond).parameters:
                    extra['origin_device'] = envelope.get('origin_device') or {}
                reply = self.assistant.respond(envelope["prompt"], activity_id=activity_id,
                            request_id=row["request_id"], progress=progress, **extra)
            else:
                progress({"state": "responding", "label": "Assistant is responding"})
                reply = self.assistant.respond(envelope["prompt"])
            if not isinstance(reply, str) or not reply.strip():
                raise RuntimeError("The assistant returned no answer. The request was not completed.")
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            recoverable = (isinstance(exc, (ConnectionError, TimeoutError))
                           or "timeout" in type(exc).__name__.lower()
                           or "connect" in type(exc).__name__.lower()
                           or "readerror" in type(exc).__name__.lower()
                           or status == 429 or (isinstance(status, int) and status >= 500))
            attempt = row["attempts"]
            retry = recoverable and attempt < 3
            from herald.router.sanitization import sanitize_error
            error = sanitize_error(f"{type(exc).__name__}: {str(exc)[:1000]}")
            next_attempt = self.clock() + (10 * (2 ** (attempt - 1))) if retry else 0
            with self._db() as db:
                # The state transition and its observable event are one durable
                # transaction.  Consumers can therefore never see "retrying"
                # while the request still appears to be running.
                db.execute("UPDATE requests SET state=?,error=?,next_attempt=? WHERE request_id=?",
                           ("queued" if retry else "failed", error,
                            next_attempt, row["request_id"]))
                retry_payload = {"request_id": row["request_id"], "activity_id": activity_id,
                                 "state": "retrying" if retry else "failed",
                                 "label": "Retrying interrupted response" if retry else "Assistant response failed",
                                 "error": error, "attempts": attempt}
                if retry:
                    retry_payload["next_attempt"] = next_attempt
                if session_id:
                    retry_payload["session_id"] = session_id
                if correlation_id:
                    retry_payload["correlation_id"] = correlation_id
                db.execute("INSERT INTO events(event_id,conversation_id,kind,payload,created) VALUES(?,?,?,?,?)",
                           (uuid.uuid4().hex, envelope["conversation_id"], "conversation.request",
                            json.dumps(retry_payload), self.clock()))
                if not retry:
                    message = self._append(db, envelope["conversation_id"], "assistant",
                                           "Assistant response failed: " + error, [],
                                           envelope["correlation_id"], envelope["continuation"],
                                   request_id=row["request_id"])
                    db.execute("UPDATE requests SET assistant_message_id=? WHERE request_id=?",
                               (message["message_id"], row["request_id"]))
            return
        with self._db() as db:
            message = self._append(db, envelope["conversation_id"], "assistant", reply, [],
                                   envelope["correlation_id"], envelope["continuation"],
                                   request_id=row["request_id"])
            db.execute("UPDATE requests SET state='completed',assistant_message_id=?,error=NULL WHERE request_id=?",
                       (message["message_id"], row["request_id"]))
            completed_payload = {"request_id": row["request_id"], "activity_id": activity_id,
                                 "state": "completed", "label": "Completed"}
            if session_id:
                completed_payload["session_id"] = session_id
            if correlation_id:
                completed_payload["correlation_id"] = correlation_id
            db.execute("INSERT INTO events(event_id,conversation_id,kind,payload,created) VALUES(?,?,?,?,?)",
                       (uuid.uuid4().hex, envelope["conversation_id"], "conversation.request",
                        json.dumps(completed_payload), self.clock()))
        if getattr(self, "voice", None) is not None:
            try:
                self.voice.response_ready(
                    request_id=row["request_id"],
                    session_id=session_id,
                    correlation_id=correlation_id,
                    conversation_id=envelope["conversation_id"],
                    device_id=envelope.get("origin_device", {}).get("device_id", DEFAULT_DEVICE),
                )
            except Exception:
                pass

    def _request_event(self, conversation_id, request_id, activity_id, state, label, tool_name=None,
                       session_id=None, correlation_id=None):
        payload = {"request_id": request_id, "activity_id": activity_id,
                   "state": state, "label": label}
        if tool_name:
            payload["tool_name"] = tool_name
        if session_id:
            payload["session_id"] = session_id
        if correlation_id:
            payload["correlation_id"] = correlation_id
        with self._db() as db:
            db.execute("INSERT INTO events(event_id,conversation_id,kind,payload,created) VALUES(?,?,?,?,?)",
                       (uuid.uuid4().hex, conversation_id, "conversation.request",
                        json.dumps(payload), self.clock()))

    def requests(self, conversation_id, *, limit=200):
        conversation_id = self._text(conversation_id, "conversation_id", 256)
        with self._db() as db:
            rows = db.execute("SELECT * FROM requests WHERE json_extract(envelope,'$.conversation_id')=? ORDER BY rowid DESC LIMIT ?",
                              (conversation_id, min(max(int(limit), 1), 500))).fetchall()
            events = db.execute("SELECT payload FROM events WHERE conversation_id=? AND kind='conversation.request' ORDER BY revision",
                                (conversation_id,)).fetchall()
        latest = {json.loads(item[0])["request_id"]: json.loads(item[0]) for item in events}
        result = []
        for row in rows:
            envelope = json.loads(row["envelope"] or "{}")
            if envelope.get("conversation_id") == conversation_id:
                value = dict(latest.get(row["request_id"], {}), request_id=row["request_id"],
                             activity_id=envelope.get("activity_id") or row["request_id"],
                             request_state=row["state"], attempts=row["attempts"],
                             next_attempt=row["next_attempt"])
                if row["error"]: value["error"] = row["error"]
                session_val = envelope.get("session_id") or (row["session_id"] if "session_id" in row.keys() else None)
                if session_val:
                    value["session_id"] = session_val
                corr_val = envelope.get("correlation_id") or (row["correlation_id"] if "correlation_id" in row.keys() else None)
                if corr_val:
                    value["correlation_id"] = corr_val
                result.append(value)
        return result

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
            # Publish only after successful durable commit. Read connections
            # and rolled-back writes cannot wake clients with unseen events.
            worker = getattr(self, "_worker", None)
            if db.total_changes and worker is not None:
                with worker["events_changed"]:
                    worker["events_changed"].notify_all()
        finally:
            db.close()

    @staticmethod
    def _text(value, name, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} must be a bounded non-empty string")
        return value.strip()

    async def capture_upload(self, stream, capture_id, *, name, media_type,
                             verify_authority):
        """Durably ingest one phone capture into retained preparation sources."""
        from .phone_pairing import _path_lock
        from ..life.preparation import Store
        capture_id = self._text(capture_id, "capture_id", 256)
        name = self._text(name, "name", 512)
        media_type = self._text(media_type, "media_type", 256)
        if any(ord(char) < 32 for char in name + media_type):
            raise ValueError("upload metadata cannot contain control characters")
        uploads = (self.home / "uploads").resolve()
        uploads.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256((DEFAULT_DEVICE + ":" + capture_id).encode()).hexdigest()
        folder = uploads / identity
        pending = uploads / (".pending-" + uuid.uuid4().hex)
        digest, size = hashlib.sha256(), 0
        try:
            with pending.open("xb") as output:
                async for chunk in stream:
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise UploadTooLarge("upload exceeds 64 MiB")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            # Re-check after the network stream, before publishing any capture.
            verify_authority()
            with _path_lock(folder):
                if folder.is_symlink() or not folder.resolve().is_relative_to(uploads):
                    raise ValueError("upload directory must stay inside mobile storage")
                metadata = folder / "provenance.json"
                if metadata.is_file():
                    saved = json.loads(metadata.read_text(encoding="utf-8"))
                    if saved["sha256"] != digest.hexdigest():
                        raise ConversationConflict("capture_id belongs to different bytes")
                    return saved["attachment"]
                folder.mkdir(parents=True, exist_ok=True)
                safe_name = name.replace("\\", "/").rsplit("/", 1)[-1]
                suffix = Path(safe_name).suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                    suffix = ".bin"
                target = folder / ("content" + suffix)
                recovered = list(folder.glob("content.*"))
                if recovered:
                    target = recovered[0]
                if target.is_symlink() or not target.resolve().is_relative_to(folder.resolve()):
                    raise ValueError("upload file must stay inside capture storage")
                if target.exists():
                    old_digest = hashlib.sha256()
                    with target.open("rb") as previous:
                        for chunk in iter(lambda: previous.read(1024 * 1024), b""):
                            old_digest.update(chunk)
                    if old_digest.hexdigest() != digest.hexdigest():
                        raise ConversationConflict("capture_id belongs to different bytes")
                else:
                    pending.replace(target)
                store = Store(self.home.parent / "preparation")
                source_id = store.register_source(folder, device=DEFAULT_DEVICE)
                store.scan_source(source_id)
                attachment = {"kind": "source", "source_id": source_id,
                              "revision": 1, "name": name,
                              "media_type": media_type, "uri": target.as_uri()}
                record = {"device_id": DEFAULT_DEVICE, "capture_id": capture_id,
                          "sha256": digest.hexdigest(), "size": size,
                          "received_at": self.clock(), "attachment": attachment}
                temporary = folder / (".provenance-" + uuid.uuid4().hex)
                temporary.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
                temporary.replace(metadata)
                return attachment
        finally:
            pending.unlink(missing_ok=True)

    @staticmethod
    def _attachments(values):
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > 32:
            raise ValueError("attachments must be a bounded list")
        result = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("attachment must be an object")
            kind = value.get("kind")
            identity = value.get("source_id") if kind == "source" else value.get("artifact_id")
            if kind not in {"source", "artifact"} or not isinstance(identity, str) or not identity:
                raise ValueError("attachment requires kind and source_id or artifact_id")
            if (isinstance(value.get("revision"), bool)
                    or not isinstance(value.get("revision"), int)
                    or value["revision"] < 1):
                raise ValueError("attachment revision must be a positive integer")
            result.append({key: value[key] for key in
                           ("kind", "source_id", "artifact_id", "revision", "name", "media_type", "uri")
                           if key in value})
        json.dumps(result, allow_nan=False)
        return result

    @staticmethod
    def _message(row):
        item = dict(row)
        item["attachments"] = json.loads(item["attachments"])
        return item

    def history(self, conversation_id, *, after_sequence=0, limit=200):
        conversation_id = self._text(conversation_id, "conversation_id", 256)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (conversation_id, max(0, int(after_sequence)), min(max(1, int(limit)), 500))).fetchall()
        return [self._message(row) for row in rows]

    def events(self, conversation_id, *, after_revision=0, limit=200):
        conversation_id = self._text(conversation_id, "conversation_id", 256)
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE conversation_id=? AND revision>? ORDER BY revision LIMIT ?",
                (conversation_id, max(0, int(after_revision)), min(max(1, int(limit)), 500))).fetchall()
        return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def wait_events(self, conversation_id, *, after_revision=0, timeout=5):
        """Wait for committed events without losing a commit between read and wait.

        SQLite remains authoritative; the bounded heartbeat also discovers writes
        from other processes, whose condition notifications are not shared.
        """
        deadline = time.monotonic() + min(max(float(timeout), 0), 5)
        condition = self._worker["events_changed"]
        with condition:
            while True:
                rows = self.events(conversation_id, after_revision=after_revision)
                if rows:
                    return rows
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                condition.wait(remaining)

    def emit_event(self, conversation_id, kind, payload):
        """Append a non-message event to the authoritative conversation stream."""
        conversation_id = self._text(conversation_id, "conversation_id", 256)
        kind = self._text(kind, "kind", 128)
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             allow_nan=False)
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO events(event_id,conversation_id,kind,payload,created) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, conversation_id, kind, encoded, self.clock()))
            revision = cursor.lastrowid
        return {"revision": revision, "conversation_id": conversation_id,
                "kind": kind, "payload": payload}

    def publish_presentation(self, presentation, *, conversation_id=None):
        """Publish one validated projection revision without adding transcript text."""
        from .overlay_presentation import validate_presentation
        value = validate_presentation(presentation)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        provenance = value["provenance"]
        conversation_id = self._text(
            conversation_id or provenance["conversation_id"], "conversation_id", 256)
        if conversation_id != provenance["conversation_id"]:
            raise ConversationConflict("presentation provenance belongs to another conversation")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT event_revision,payload FROM presentation_deliveries WHERE presentation_id=? AND revision=?",
                (value["id"], value["revision"])).fetchone()
            if prior:
                if prior["payload"] is not None and prior["payload"] != encoded:
                    raise ConversationConflict(
                        "presentation id and revision belong to a different payload")
                return {"state": "duplicate", "event_revision": prior["event_revision"],
                        "presentation": value}
            latest = db.execute(
                "SELECT MAX(revision) FROM presentation_deliveries WHERE presentation_id=?",
                (value["id"],)).fetchone()[0]
            if latest is not None and value["revision"] < latest:
                raise ConversationConflict("presentation revision is stale")
            cursor = db.execute(
                "INSERT INTO events(event_id,conversation_id,kind,payload,created) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, conversation_id, "conversation.presentation",
                 encoded, self.clock()))
            event_revision = cursor.lastrowid
            db.execute("""INSERT INTO presentation_deliveries(
                       presentation_id,revision,conversation_id,event_revision,payload,created)
                       VALUES(?,?,?,?,?,?)""",
                       (value["id"], value["revision"], conversation_id,
                        event_revision, encoded, self.clock()))
        return {"state": "ready", "event_revision": event_revision,
                "presentation": value}

    def deliver_proactive(self, record, envelope, *, conversation_id=None):
        """Append one durable assistant-initiated message to the shared thread.

        The record/revision key is the transport idempotency boundary.  Retrying
        a tick or reopening the runtime therefore returns the original message
        instead of producing another desktop/phone-visible event.
        """
        if not isinstance(record, dict) or not isinstance(envelope, dict):
            raise ValueError("record and envelope must be objects")
        record_id = self._text(envelope.get("record_id") or record.get("id"),
                               "record_id", 256)
        revision = envelope.get("revision", record.get("revision", 1))
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("revision must be a positive integer")
        conversation_id = self._text(
            conversation_id or default_conversation(),
            "conversation_id", 256)
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        content = payload.get("message") or payload.get("title") or record.get("kind")
        content = self._text(content, "content", 65536)
        delivery_key = f"{record_id}:{revision}"
        correlation_id = str(envelope.get("correlation_id") or
                             envelope.get("intent_key") or delivery_key)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT message_id FROM proactive_deliveries WHERE delivery_key=?",
                (delivery_key,)).fetchone()
            if prior:
                return {"ok": True, "state": "accepted", "provider": "conversation",
                        "message_id": prior["message_id"], "duplicate": True,
                        "conversation_id": conversation_id}
            message = self._append(db, conversation_id, "assistant", content, [],
                                   correlation_id,
                                   {"context_id": record_id, "revision": revision})
            db.execute("""INSERT INTO proactive_deliveries(
                       delivery_key,message_id,conversation_id,record_id,revision,created)
                       VALUES(?,?,?,?,?,?)""",
                       (delivery_key, message["message_id"], conversation_id,
                        record_id, revision, self.clock()))
            db.execute("INSERT INTO events(event_id,conversation_id,kind,payload,created) VALUES(?,?,?,?,?)",
                       (uuid.uuid4().hex, conversation_id, "conversation.proactive",
                        json.dumps({"record_id": record_id, "revision": revision,
                                    "message_id": message["message_id"],
                                    "intent_key": envelope.get("intent_key")},
                                   ensure_ascii=False, sort_keys=True), self.clock()))
        return {"ok": True, "state": "accepted", "provider": "conversation",
                "message_id": message["message_id"], "duplicate": False,
                "conversation_id": conversation_id}

    def _append(self, db, conversation_id, role, content, attachments,
                correlation_id, continuation=None, *, request_id=None):
        message_id = uuid.uuid4().hex
        continuation = continuation or {}
        db.execute("""INSERT INTO messages(message_id,conversation_id,role,content,attachments,
                   continuation_id,continuation_revision,correlation_id,created,request_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (message_id, conversation_id, role, content,
                    json.dumps(attachments, ensure_ascii=False, sort_keys=True),
                    continuation.get("context_id"), continuation.get("revision"),
                    correlation_id, self.clock(), request_id))
        row = db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
        message = self._message(row)
        db.execute("INSERT INTO events(event_id,conversation_id,kind,payload,created) VALUES(?,?,?,?,?)",
                   (uuid.uuid4().hex, conversation_id, "conversation.message",
                    json.dumps(message, ensure_ascii=False, sort_keys=True), self.clock()))
        return message

    def accept(self, *, conversation_id, request_id, content, attachments=None,
               correlation_id=None, continuation=None, expires_at=None, origin_device=None,
               session_id=None, interaction_context=None):
        conversation_id = self._text(conversation_id, "conversation_id", 256)
        request_id = self._text(request_id, "request_id", 256)
        content = self._text(content, "content", 65536)
        if expires_at is not None:
            if (isinstance(expires_at, bool) or not isinstance(expires_at, numbers.Real)
                    or not math.isfinite(expires_at)):
                raise ValueError("expires_at must be a finite Unix timestamp")
            if expires_at <= self.clock():
                raise ConversationConflict("offline conversation request has expired")
        attachments = self._attachments(attachments)
        correlation_id = correlation_id or uuid.uuid4().hex
        session_id = session_id or (origin_device.get("session_id") if isinstance(origin_device, dict) else None)
        if session_id is None and isinstance(interaction_context, dict):
            session_id = interaction_context.get("session_id")
        if session_id is not None:
            session_id = self._text(session_id, "session_id", 256)
        if interaction_context is not None and not isinstance(interaction_context, dict):
            raise ValueError("interaction_context must be an object")
        continuation = dict(continuation or {})
        fingerprint = json.dumps({"conversation_id": conversation_id, "content": content,
                                  "attachments": attachments, "continuation": continuation,
                                  "expires_at": expires_at},
                                 sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            retry = db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if retry:
                if retry["fingerprint"] != fingerprint:
                    raise ConversationConflict("request_id belongs to a different message")
                retry_env = json.loads(retry["envelope"] or "{}")
                return {"state": retry["state"], "request_id": request_id,
                        "conversation_id": conversation_id, "duplicate": True,
                        "activity_id": retry_env.get("activity_id"),
                        "correlation_id": retry_env.get("correlation_id", correlation_id),
                        "session_id": retry_env.get("session_id", session_id)}
            current = None
            if continuation:
                update = continuation.get("update")
                if update is not None:
                    if not isinstance(update, dict):
                        raise ValueError("continuation update must be an object")
                    try:
                        current = self.handoffs.record_continuation(
                            update["current_task"], update["workspace_binding_ref"],
                            update["next_step"], request_id=update.get("request_id", request_id),
                            context_id=continuation.get("context_id"),
                            expected_revision=continuation.get("expected_revision"),
                            metadata=update.get("metadata"))
                    except KeyError as exc:
                        raise ValueError(f"missing continuation field: {exc.args[0]}") from exc
                    except ValueError as exc:
                        if "Revision conflict" in str(exc):
                            raise ConversationConflict(str(exc)) from exc
                        raise
                else:
                    current = self.handoffs.get_latest_context(continuation.get("context_id"))
                    expected = continuation.get("expected_revision")
                    if expected is not None and (not current or current.get("revision") != expected):
                        raise ConversationConflict("continuation revision is stale")
            self._append(db, conversation_id, "user", content, attachments,
                         correlation_id, current, request_id=request_id)
            origin = dict(origin_device or {})
            prompt = content
            if origin:
                # One short line, not a JSON block on every message: the device, platform and interface are
                # what the agent needs ('this phone' = this device); the per-message evidence stays in the envelope.
                where = " / ".join(str(origin[key]) for key in ("device_id", "platform", "interface") if origin.get(key))
                prompt = f"[Authenticated message origin: {where}; 'this phone' means this device]\n{prompt}"
            if attachments:
                prompt += "\nAttached source/artifact references: " + json.dumps(attachments, ensure_ascii=False)
                prompt += "\nUse read_phone_source(source_id) to read retained phone uploads on the assistant's main computer. For image evidence inspect the returned path with your native image viewing tool; these references alone are not pixels."
            if current:
                prompt += "\nPortable continuation: " + json.dumps(current, ensure_ascii=False)
            envelope = {"conversation_id": conversation_id, "prompt": prompt,
                        "correlation_id": correlation_id, "continuation": current or {},
                        "activity_id": uuid.uuid4().hex, "origin_device": origin}
            if session_id:
                envelope["session_id"] = session_id
            if interaction_context:
                envelope["interaction_context"] = interaction_context
            db.execute("INSERT INTO requests(request_id,fingerprint,assistant_message_id,envelope,state,session_id,correlation_id) VALUES(?,?,?,?,'queued',?,?)",
                       (request_id, fingerprint, "", json.dumps(envelope, ensure_ascii=False), session_id, correlation_id))
            event_payload = {"request_id": request_id, "activity_id": envelope["activity_id"],
                             "state": "accepted", "label": "Request accepted",
                             "correlation_id": correlation_id}
            if session_id:
                event_payload["session_id"] = session_id
            db.execute("INSERT INTO events(event_id,conversation_id,kind,payload,created) VALUES(?,?,?,?,?)",
                       (uuid.uuid4().hex, conversation_id, "conversation.request",
                        json.dumps(event_payload), self.clock()))
        self._wake.set()
        return {"state": "accepted", "request_id": request_id,
                "conversation_id": conversation_id, "duplicate": False,
                "activity_id": envelope["activity_id"],
                "correlation_id": correlation_id,
                "session_id": session_id,
                "continuation_id": current.get("context_id") if current else None,
                "continuation_revision": current.get("revision") if current else None}

    def submit(self, **arguments):
        """Retain synchronous callers; HTTP transports use durable accept instead."""
        accepted = self.accept(**arguments)
        while True:
            with self._db() as db:
                request = db.execute("SELECT * FROM requests WHERE request_id=?",
                                     (accepted["request_id"],)).fetchone()
                if request["state"] == "failed":
                    raise RuntimeError(request["error"])
                if request["state"] == "completed":
                    message = db.execute("SELECT * FROM messages WHERE message_id=?",
                                         (request["assistant_message_id"],)).fetchone()
                    return dict(accepted, state="completed", message=self._message(message))
            time.sleep(0.1)
