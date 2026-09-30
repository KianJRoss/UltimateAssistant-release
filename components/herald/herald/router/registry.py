"""SQLite-backed backend registry for the unified router.

One row per callable backend, regardless of category -- an API-key model, a
CLI tool (via clink, including Antigravity's per-model profiles), a
network-reachable local model (LM Studio/Ollama on another node), or a
browser-session account (g4f). Router picks among rows matching the
requested name, respecting priority and per-row circuit breakers, so a dead
connection or expired session routes around itself instead of hard failing.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any

DEFAULT_DB_PATH = database_path(
    "router.db",
    env_var="HERALD_ROUTER_DB",
    legacy_path=Path(__file__).resolve().parent / "router.db",
)
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 60
ROUTE_CAPABILITIES = {
    "code": "code",
    "reason": "reasoning",
    "fast": "fast",
}
_SECRET_KEYS = {"api_key", "access_token", "refresh_token", "password", "secret", "token"}


def _validate_backend_config(config: dict[str, Any]) -> None:
    """Prevent credentials from entering the plaintext router database."""
    def walk(value: Any) -> str | None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = key.lower()
                if normalized == "secret_ref":
                    continue
                if (
                    normalized in _SECRET_KEYS
                    or any(marker in normalized for marker in ("authorization", "cookie", "password", "secret"))
                ) and nested not in (None, ""):
                    return key
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = walk(nested)
                if found:
                    return found
        return None

    forbidden = walk(config)
    if forbidden:
        raise ValueError(
            f"inline secret field '{forbidden}' is not allowed; use env:, keyring:, or vault: secret_ref"
        )
    reference = config.get("secret_ref")
    if reference is not None:
        from herald.router.account_registry import validate_secret_ref
        validate_secret_ref(str(reference))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Backend:
    id: int
    backend_type: str
    name: str
    config: dict[str, Any]
    capabilities: dict[str, Any]
    cost_per_1k_tokens: float | None
    priority: int
    enabled: bool
    circuit_open_until: str | None
    consecutive_failures: int
    pool_name: str | None = None

    @property
    def circuit_open(self) -> bool:
        if not self.circuit_open_until:
            return False
        return datetime.fromisoformat(self.circuit_open_until) > datetime.now(UTC)


class Registry:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    backend_type TEXT NOT NULL CHECK(backend_type IN
                        ('api_key', 'cli', 'local_model', 'browser_session')),
                    name TEXT NOT NULL UNIQUE,
                    config_json TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    cost_per_1k_tokens REAL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    circuit_open_until TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # pool_name groups multiple rows (e.g. several API keys for the
            # same logical model) under one lookup name for failover/pooling.
            # NULL means "not pooled" -- looked up by its own unique `name`,
            # identical to pre-pooling behavior, so existing rows are
            # unaffected. Added via ALTER since CREATE TABLE IF NOT EXISTS
            # won't retrofit existing databases.
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(backends)")}
            if "pool_name" not in existing_cols:
                conn.execute("ALTER TABLE backends ADD COLUMN pool_name TEXT")

    def register(
        self,
        *,
        backend_type: str,
        name: str,
        config: dict[str, Any],
        capabilities: dict[str, Any] | None = None,
        cost_per_1k_tokens: float | None = None,
        priority: int = 100,
        enabled: bool = True,
        pool_name: str | None = None,
    ) -> int:
        """Upsert by name -- re-registering an existing backend updates its
        config/priority without resetting its circuit-breaker health state."""
        _validate_backend_config(config)
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO backends
                    (backend_type, name, config_json, capabilities_json,
                     cost_per_1k_tokens, priority, enabled, pool_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    backend_type = excluded.backend_type,
                    config_json = excluded.config_json,
                    capabilities_json = excluded.capabilities_json,
                    cost_per_1k_tokens = excluded.cost_per_1k_tokens,
                    priority = excluded.priority,
                    enabled = excluded.enabled,
                    pool_name = excluded.pool_name,
                    updated_at = excluded.updated_at
                """,
                (
                    backend_type, name, json.dumps(config), json.dumps(capabilities or {}),
                    cost_per_1k_tokens, priority, int(enabled), pool_name, now, now,
                ),
            )
            row = conn.execute("SELECT id FROM backends WHERE name = ?", (name,)).fetchone()
            return int(row["id"])

    def _row_to_backend(self, row: sqlite3.Row) -> Backend:
        return Backend(
            id=row["id"], backend_type=row["backend_type"], name=row["name"],
            config=json.loads(row["config_json"]), capabilities=json.loads(row["capabilities_json"]),
            cost_per_1k_tokens=row["cost_per_1k_tokens"], priority=row["priority"],
            enabled=bool(row["enabled"]), circuit_open_until=row["circuit_open_until"],
            consecutive_failures=row["consecutive_failures"], pool_name=row["pool_name"],
        )

    def get(self, name: str) -> Backend | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM backends WHERE name = ?", (name,)).fetchone()
        return self._row_to_backend(row) if row else None

    def list_all(self, *, enabled_only: bool = False) -> list[Backend]:
        query = "SELECT * FROM backends"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY priority ASC, name ASC"
        with closing(self._connect()) as conn:
            rows = conn.execute(query).fetchall()
        return [self._row_to_backend(r) for r in rows]

    def list_pool(self, pool_or_name: str) -> list[Backend]:
        """Resolve a requested model name to its candidate backend rows, in
        priority order. A backend registered WITHOUT a pool_name is looked up
        by its own unique `name` only (today's exact-match behavior,
        unchanged); one registered WITH a pool_name is joined by any other
        row sharing that same pool_name, so N API keys for one logical model
        become N candidates the router can fail over across."""
        query = (
            "SELECT * FROM backends WHERE pool_name = ? OR name = ? "
            "ORDER BY priority ASC, name ASC"
        )
        with closing(self._connect()) as conn:
            rows = conn.execute(query, (pool_or_name, pool_or_name)).fetchall()
        backends = [self._row_to_backend(r) for r in rows]
        if backends:
            return backends

        # Presets are virtual pools. A backend can participate in several
        # presets through capabilities while retaining its account/model pool.
        capability = ROUTE_CAPABILITIES.get(pool_or_name)
        if capability:
            return [
                backend for backend in self.list_all()
                if backend.capabilities.get(capability) is True
            ]
        return []

    def set_enabled(self, name: str, enabled: bool) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE backends SET enabled = ?, updated_at = ? WHERE name = ?",
                (int(enabled), _now_iso(), name),
            )

    def record_success(self, name: str) -> None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT circuit_open_until FROM backends WHERE name = ?", (name,)
            ).fetchone()
            was_open = bool(row and row["circuit_open_until"])
            conn.execute(
                "UPDATE backends SET consecutive_failures = 0, circuit_open_until = NULL, "
                "updated_at = ? WHERE name = ?",
                (_now_iso(), name),
            )
        if was_open:
            self._emit_circuit_event("backend.circuit_closed", name, importance=0.6)

    def record_failure(self, name: str) -> None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM backends WHERE name = ?", (name,)
            ).fetchone()
            failures = (row["consecutive_failures"] if row else 0) + 1
            circuit_open_until = None
            newly_opened = False
            if failures >= CIRCUIT_FAILURE_THRESHOLD:
                circuit_open_until = (
                    datetime.now(UTC) + timedelta(seconds=CIRCUIT_COOLDOWN_SECONDS)
                ).isoformat()
                newly_opened = True
            conn.execute(
                "UPDATE backends SET consecutive_failures = ?, circuit_open_until = ?, "
                "updated_at = ? WHERE name = ?",
                (failures, circuit_open_until, _now_iso(), name),
            )
        if newly_opened:
            self._emit_circuit_event("backend.circuit_opened", name, importance=0.7)

    @staticmethod
    def _emit_circuit_event(event_type: str, backend_name: str, *, importance: float) -> None:
        try:
            from herald.router import event_bus
            event_bus.emit_nowait(
                event_type, importance=importance,
                payload={"backend": backend_name}, source="registry",
            )
        except Exception:  # noqa: BLE001 -- circuit tracking must never fail on event emission
            pass

    def remove(self, name: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM backends WHERE name = ?", (name,))
