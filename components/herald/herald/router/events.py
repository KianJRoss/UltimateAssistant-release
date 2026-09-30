"""Sanitized router events and opt-in HTTP/command hooks."""
from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from herald.router.account_registry import resolve_secret_ref


DEFAULT_DB_PATH = Path.home() / ".herald" / "events.db"
SENSITIVE_MARKERS = ("authorization", "cookie", "token", "secret", "password", "credential", "header")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def sanitize(value: Any, key: str = "") -> Any:
    if any(marker in key.lower() for marker in SENSITIVE_MARKERS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, key) for item in value]
    if isinstance(value, str):
        return value[:4000]
    return value


class EventBus:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, topic TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hooks (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, pattern TEXT NOT NULL,
                    transport TEXT NOT NULL, config_json TEXT NOT NULL,
                    secret_ref TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hook_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                    hook_id TEXT NOT NULL, success INTEGER NOT NULL, error TEXT,
                    created_at TEXT NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def emit(self, topic: str, payload: dict[str, Any], *, dispatch: bool = True) -> dict[str, Any]:
        event = {
            "id": uuid.uuid4().hex, "topic": topic,
            "payload": sanitize(payload), "created_at": _now(),
        }
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO events (id,topic,payload_json,created_at) VALUES (?,?,?,?)",
                (event["id"], topic, json.dumps(event["payload"]), event["created_at"]),
            )
        if dispatch:
            threading.Thread(target=self._dispatch, args=(event,), daemon=True).start()
        return event

    def list_events(self, limit: int = 100, topic: str | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM events", []
        if topic:
            query += " WHERE topic=?"
            args.append(topic)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(query, args).fetchall()
        return [
            {"id": row["id"], "topic": row["topic"],
             "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def register_hook(self, name: str, pattern: str, transport: str, config: dict[str, Any],
                      *, secret_ref: str | None = None, enabled: bool = True) -> dict[str, Any]:
        if transport not in {"http", "command"}:
            raise ValueError("hook transport must be http or command")
        if transport == "http":
            parsed = urlsplit(str(config.get("url") or ""))
            sensitive_query = any(
                marker in key.lower()
                for key, _ in parse_qsl(parsed.query)
                for marker in ("key", "token", "secret", "password")
            )
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("HTTP hooks require an http(s) config.url")
            if parsed.username or parsed.password or sensitive_query:
                raise ValueError("hook URLs cannot embed credentials; use secret_ref")
            if config.get("headers"):
                raise ValueError("hook headers are not stored inline; use secret_ref")
        command = config.get("command")
        if transport == "command" and (not isinstance(command, list) or not command):
            raise ValueError("command hooks require config.command as a non-empty list")
        if transport == "command" and any(
            item.startswith("-") and any(
                marker in item.lower() for marker in ("key", "token", "secret", "password")
            )
            for item in map(str, command)
        ):
            raise ValueError("command hook arguments cannot contain credentials; use environment indirection")
        now = _now()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO hooks (id,name,pattern,transport,config_json,secret_ref,enabled,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET pattern=excluded.pattern,
                    transport=excluded.transport, config_json=excluded.config_json,
                    secret_ref=excluded.secret_ref, enabled=excluded.enabled,
                    updated_at=excluded.updated_at
            """, (
                uuid.uuid4().hex, name, pattern, transport, json.dumps(config),
                secret_ref, int(enabled), now, now,
            ))
        return next(hook for hook in self.list_hooks() if hook["name"] == name)

    def list_hooks(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM hooks ORDER BY name").fetchall()
        return [self._hook_public(row) for row in rows]

    def remove_hook(self, name: str) -> bool:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute("DELETE FROM hooks WHERE name=?", (name,))
        return cursor.rowcount > 0

    def _dispatch(self, event: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            hooks = conn.execute("SELECT * FROM hooks WHERE enabled=1").fetchall()
        for hook in hooks:
            if not fnmatch.fnmatchcase(event["topic"], hook["pattern"]):
                continue
            error = None
            try:
                config = json.loads(hook["config_json"])
                timeout = max(1.0, min(float(config.get("timeout", 10)), 60.0))
                if hook["transport"] == "http":
                    headers = {"content-type": "application/json"}
                    if hook["secret_ref"]:
                        headers["authorization"] = f"Bearer {resolve_secret_ref(hook['secret_ref'])}"
                    response = httpx.post(config["url"], json=event, headers=headers, timeout=timeout)
                    response.raise_for_status()
                else:
                    env = os.environ.copy()
                    env.update({"HERALD_EVENT_ID": event["id"], "HERALD_EVENT_TOPIC": event["topic"]})
                    subprocess.run(
                        [str(item) for item in config["command"]],
                        input=json.dumps(event), text=True, capture_output=True,
                        timeout=timeout, check=True, env=env,
                    )
                success = True
            except Exception as exc:  # never persist response bodies or resolved credentials
                success, error = False, f"{type(exc).__name__}: delivery failed"
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO hook_deliveries (event_id,hook_id,success,error,created_at) VALUES (?,?,?,?,?)",
                    (event["id"], hook["id"], int(success), error, _now()),
                )

    @staticmethod
    def _hook_public(row: sqlite3.Row) -> dict[str, Any]:
        config = json.loads(row["config_json"])
        return {
            "id": row["id"], "name": row["name"], "pattern": row["pattern"],
            "transport": row["transport"], "config": sanitize(config),
            "secret_ref": row["secret_ref"], "enabled": bool(row["enabled"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
