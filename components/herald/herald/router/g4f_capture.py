"""No-model Kapture capture state machine for G4F browser accounts."""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from typing import Callable

import httpx

from herald.router.secret_vault import SecretVault


DEFAULT_DB_PATH = Path.home() / ".herald" / "capture.db"
KAPTURE_BASE = "http://127.0.0.1:61822"
CONVERSATION_URL = "https://chatgpt.com/backend-api/f/conversation"
FINAL_STATUSES = {"captured", "materialized", "failed", "cancelled", "expired"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def find_accounts_file() -> Path:
    configured = os.environ.get("HERALD_G4F_ACCOUNTS_FILE")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(os.environ.get("HERALD_DATA_DIR", str(Path.home() / ".herald"))) / "g4f" / "accounts.json",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("G4F accounts.json was not found; set HERALD_G4F_ACCOUNTS_FILE")


def load_account(name: str, accounts_file: str | Path | None = None) -> tuple[dict[str, Any], Path]:
    path = Path(accounts_file).resolve() if accounts_file else find_accounts_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    for account in data.get("accounts", []):
        if account.get("name") == name:
            return account, path
    raise ValueError(f"G4F account '{name}' was not found")


@dataclass(frozen=True)
class CaptureSession:
    id: str
    account: str
    provider: str
    status: str
    started_at: str
    updated_at: str
    deadline_at: float
    artifact_ref: str | None
    identity_verified: bool
    materialized_path: str | None
    error: str | None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "account": self.account, "provider": self.provider,
            "status": self.status, "started_at": self.started_at,
            "updated_at": self.updated_at, "deadline_at": self.deadline_at,
            "artifact_saved": bool(self.artifact_ref),
            "identity_verified": self.identity_verified,
            "session_materialized": bool(self.materialized_path), "error": self.error,
        }


class CaptureRegistry:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path if db_path is not None else Path(os.environ.get("HERALD_DATA_DIR", str(Path.home() / ".herald"))) / "capture.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capture_sessions (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL,
                    status TEXT NOT NULL, tab_id TEXT, cursor INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL, updated_at TEXT NOT NULL, deadline_at REAL NOT NULL,
                    artifact_ref TEXT, identity_verified INTEGER NOT NULL DEFAULT 0,
                    materialized_path TEXT, error TEXT
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def create(self, account: str, tab_id: str, cursor: int, timeout: float) -> CaptureSession:
        session_id, now = uuid.uuid4().hex, _now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO capture_sessions "
                "(id,account,provider,status,tab_id,cursor,started_at,updated_at,deadline_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, account, "openai-chat", "watching", tab_id, cursor,
                 now, now, time.time() + timeout),
            )
        return self.get(session_id)

    def get(self, session_id: str) -> CaptureSession | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM capture_sessions WHERE id=?", (session_id,)).fetchone()
        return self._public_row(row) if row else None

    def internal(self, session_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT * FROM capture_sessions WHERE id=?", (session_id,)).fetchone()

    def list(self, limit: int = 50) -> list[CaptureSession]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM capture_sessions ORDER BY started_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [self._public_row(row) for row in rows]

    def update(self, session_id: str, **values: Any) -> CaptureSession:
        allowed = {
            "status", "cursor", "artifact_ref", "identity_verified",
            "materialized_path", "error",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        values["updated_at"] = _now()
        assignments = ",".join(f"{key}=?" for key in values)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"UPDATE capture_sessions SET {assignments} WHERE id=?",
                (*values.values(), session_id),
            )
        session = self.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    @staticmethod
    def _public_row(row: sqlite3.Row) -> CaptureSession:
        return CaptureSession(
            id=row["id"], account=row["account"], provider=row["provider"],
            status=row["status"], started_at=row["started_at"], updated_at=row["updated_at"],
            deadline_at=row["deadline_at"], artifact_ref=row["artifact_ref"],
            identity_verified=bool(row["identity_verified"]),
            materialized_path=row["materialized_path"], error=row["error"],
        )


class KaptureHTTP:
    def __init__(self, base_url: str = KAPTURE_BASE) -> None:
        self.base_url = base_url.rstrip("/")

    def get(self, path: str) -> Any:
        response = httpx.get(f"{self.base_url}{path}", timeout=15)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        response = httpx.post(f"{self.base_url}{path}", json=body or {}, timeout=30)
        response.raise_for_status()
        return response.json()


def _verify_identity(headers: dict[str, str], expected_hint: str) -> None:
    authorization = next((value for key, value in headers.items() if key.lower() == "authorization"), "")
    proof = next((value for key, value in headers.items() if key.lower() == "openai-sentinel-proof-token"), "")
    if not authorization.lower().startswith("bearer ") or not proof:
        raise ValueError("captured request did not contain the required authenticated headers")
    try:
        token = authorization.split(" ", 1)[1]
        payload_value = token.split(".")[1]
        payload_value += "=" * (-len(payload_value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_value))
        email = payload.get("https://api.openai.com/profile", {}).get("email", "")
        expires = int(payload.get("exp") or 0)
    except (ValueError, IndexError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("captured bearer token could not be structurally verified") from exc
    hint_parts = [part.lower() for part in expected_hint.replace("*", " ").split() if part]
    if not hint_parts:
        raise ValueError("selected G4F account has no identity hint for safe verification")
    if not email or not all(part in email.lower() for part in hint_parts):
        raise ValueError("captured identity did not match the selected G4F account")
    if expires and expires <= int(time.time()):
        raise ValueError("captured bearer token was already expired")


def _build_har(headers: dict[str, str]) -> dict[str, Any]:
    safe_headers = dict(headers)
    proof_key = next(key for key in safe_headers if key.lower() == "openai-sentinel-proof-token")
    safe_headers[proof_key] = re.sub(r"~[A-Za-z]$", "", safe_headers[proof_key])
    # Native g4f's HAR parser identifies the provider by Host and consumes
    # request.cookies, not the raw Cookie header. CDP may omit Host entirely.
    if not any(key.lower() in {"host", ":authority"} for key in safe_headers):
        safe_headers["Host"] = "chatgpt.com"
    from http.cookies import SimpleCookie
    cookie_header = next((value for key, value in headers.items() if key.lower() == "cookie"), "")
    parsed_cookies = SimpleCookie()
    parsed_cookies.load(cookie_header)
    return {
        "log": {
            "version": "1.2", "creator": {"name": "herald-kapture", "version": "1"},
            "entries": [{
                "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "request": {
                    "method": "POST", "url": CONVERSATION_URL,
                    "headers": [{"name": key, "value": value} for key, value in safe_headers.items()],
                    "cookies": [{"name": key, "value": item.value} for key, item in parsed_cookies.items()],
                    "postData": {"mimeType": "application/json", "text": "{}", "params": []},
                },
                "response": {"status": 200, "content": {"mimeType": "text/event-stream", "text": ""}},
            }],
        }
    }


def _atomic_materialize(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".session.", suffix=".har", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            Path(temp_name).chmod(0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        Path(temp_name).unlink(missing_ok=True)


class G4FCaptureManager:
    def __init__(
        self, registry: CaptureRegistry | None = None, vault: SecretVault | None = None,
        kapture: KaptureHTTP | None = None, accounts_file: str | Path | None = None,
        emit_event: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.registry = registry or CaptureRegistry()
        self.vault = vault or SecretVault()
        self.kapture = kapture or KaptureHTTP()
        self.accounts_file = Path(accounts_file).resolve() if accounts_file else None
        self.emit_event = emit_event
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def start(self, account_name: str, *, timeout: float = 120) -> CaptureSession:
        account, _ = load_account(account_name, self.accounts_file)
        if not account.get("enabled", True):
            raise ValueError(f"G4F account '{account_name}' is disabled")
        tabs = self.kapture.get("/tabs")
        candidates = [tab for tab in tabs if str(tab.get("url", "")).startswith("https://chatgpt.com")]
        if not candidates:
            raise ValueError("no connected chatgpt.com Kapture tab is available")
        candidates.sort(key=lambda tab: tab.get("lastPing", 0), reverse=True)
        tab_id = str(candidates[0]["tabId"])
        self.kapture.post(f"/tab/{tab_id}/network_monitor", {"enabled": True})
        primer = self.kapture.post(f"/tab/{tab_id}/network_requests", {"limit": 1})
        bounded_timeout = max(15.0, min(float(timeout), 900.0))
        session = self.registry.create(account_name, tab_id, int(primer.get("cursor") or 0), bounded_timeout)
        thread = threading.Thread(target=self._watch, args=(session.id,), daemon=True)
        with self._lock:
            self._threads[session.id] = thread
        thread.start()
        self._emit("auth.g4f.capture.started", session)
        return session

    def _emit(self, topic: str, session: CaptureSession) -> None:
        if self.emit_event:
            self.emit_event(topic, session.public())

    def _watch(self, session_id: str) -> None:
        try:
            while True:
                session = self.registry.get(session_id)
                if session is None or session.status != "watching":
                    return
                if time.time() >= session.deadline_at:
                    expired = self.registry.update(session_id, status="expired", error="no qualifying ChatGPT request was captured before timeout")
                    self._emit("auth.g4f.capture.expired", expired)
                    return
                if self.poll_once(session_id):
                    return
                time.sleep(0.5)
        except Exception:  # do not persist response bodies, headers, or exception repr
            failed = self.registry.update(session_id, status="failed", error="Kapture capture failed; inspect router logs")
            self._emit("auth.g4f.capture.failed", failed)
        finally:
            row = self.registry.internal(session_id)
            if row and row["tab_id"]:
                try:
                    self.kapture.post(f"/tab/{row['tab_id']}/network_monitor", {"enabled": False})
                except Exception:
                    pass
            with self._lock:
                self._threads.pop(session_id, None)

    def poll_once(self, session_id: str) -> bool:
        row = self.registry.internal(session_id)
        if row is None:
            raise KeyError(session_id)
        if row["status"] != "watching":
            return row["status"] in FINAL_STATUSES
        result = self.kapture.post(
            f"/tab/{row['tab_id']}/network_requests",
            {"since": row["cursor"], "limit": 200},
        )
        cursor = int(result.get("cursor") or row["cursor"])
        self.registry.update(session_id, cursor=cursor)
        for request in result.get("requests", []):
            if request.get("method") != "POST" or request.get("url") != CONVERSATION_URL:
                continue
            body = self.kapture.post(
                f"/tab/{row['tab_id']}/network_body",
                {"requestId": request["requestId"], "maxBytes": 4096},
            )
            headers = body.get("requestHeaders") or {}
            if not isinstance(headers, dict):
                continue
            account, accounts_path = load_account(row["account"], self.accounts_file)
            try:
                _verify_identity({str(k): str(v) for k, v in headers.items()}, str(account.get("email_hint", "")))
            except ValueError as exc:
                failed = self.registry.update(session_id, status="failed", error=str(exc))
                self._emit("auth.g4f.capture.failed", failed)
                return True
            har = _build_har({str(k): str(v) for k, v in headers.items()})
            payload = json.dumps(har, separators=(",", ":")).encode()
            artifact_name = f"g4f/{row['account']}/{session_id}"
            artifact_ref = self.vault.put(
                artifact_name, payload,
                metadata={"provider": "openai-chat", "account": row["account"], "kind": "session-har"},
            )
            session_path = accounts_path.parent / account["dir"] / "har_and_cookies" / "session.har"
            _atomic_materialize(session_path, payload)
            completed = self.registry.update(
                session_id, status="materialized", artifact_ref=artifact_ref,
                identity_verified=1, materialized_path=str(session_path), error=None,
            )
            self._emit("auth.g4f.capture.materialized", completed)
            return True
        return False

    def cancel(self, session_id: str) -> CaptureSession:
        session = self.registry.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.status not in FINAL_STATUSES:
            session = self.registry.update(session_id, status="cancelled", error=None)
            self._emit("auth.g4f.capture.cancelled", session)
            return session
        return session
