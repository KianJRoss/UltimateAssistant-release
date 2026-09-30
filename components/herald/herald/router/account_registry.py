"""Named identities and model lanes for every credential source Herald can use.

Account metadata is intentionally separate from callable backends.  One login
may expose several lanes (for example Antigravity Gemini, Claude, and GPT), and
one routing pool may contain lanes from several independent accounts.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any


DEFAULT_DB_PATH = database_path(
    "accounts.db",
    env_var="HERALD_ACCOUNTS_DB",
    legacy_path=Path(__file__).resolve().parent / "accounts.db",
)
AUTH_KINDS = {"cli_profile", "api_key", "browser_session", "oauth", "local"}
SECRET_REF_SCHEMES = {"env", "keyring", "vault"}
FORBIDDEN_CONFIG_KEYS = {
    "api_key", "access_token", "refresh_token", "password", "secret", "token",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _contains_inline_secret(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in FORBIDDEN_CONFIG_KEYS and nested not in (None, ""):
                return key
            found = _contains_inline_secret(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _contains_inline_secret(nested)
            if found:
                return found
    return None


def _strip_inline_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_inline_secrets(nested)
            for key, nested in value.items()
            if key.lower() not in FORBIDDEN_CONFIG_KEYS
            and not any(marker in key.lower() for marker in ("authorization", "password", "secret"))
        }
    if isinstance(value, list):
        return [_strip_inline_secrets(nested) for nested in value]
    return value


def validate_secret_ref(secret_ref: str | None) -> None:
    if not secret_ref:
        return
    scheme, separator, target = secret_ref.partition(":")
    if not separator or scheme not in SECRET_REF_SCHEMES or not target.strip():
        supported = ", ".join(f"{name}:..." for name in sorted(SECRET_REF_SCHEMES))
        raise ValueError(f"secret_ref must use one of: {supported}")


def resolve_secret_ref(secret_ref: str) -> str:
    """Resolve a secret only at call time; never persist the returned value."""
    validate_secret_ref(secret_ref)
    scheme, _, target = secret_ref.partition(":")
    target = target.strip()
    if scheme == "env":
        value = os.environ.get(target)
        if not value:
            raise ValueError(f"environment secret '{target}' is not set")
        return value
    if scheme == "keyring":
        try:
            import keyring
            value = keyring.get_password("herald", target)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("the system keyring is unavailable") from exc
        if not value:
            raise ValueError(f"keyring secret '{target}' was not found")
        return value
    if scheme == "vault":
        from herald.router.secret_vault import SecretVault
        try:
            return SecretVault().get(target).decode()
        except (KeyError, RuntimeError, UnicodeDecodeError) as exc:
            raise ValueError(f"vault secret '{target}' is unavailable") from exc
    raise ValueError("unsupported secret reference")


@dataclass
class Account:
    id: int
    name: str
    provider: str
    auth_kind: str
    config: dict[str, Any]
    secret_ref: str | None
    enabled: bool
    priority: int
    tags: list[str]
    created_at: str
    updated_at: str


@dataclass
class AccountLane:
    id: int
    account_id: int
    account_name: str
    name: str
    backend_name: str
    model: str
    capabilities: dict[str, Any]
    priority: int
    enabled: bool
    created_at: str
    updated_at: str


class AccountRegistry:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    auth_kind TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    secret_ref TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 100,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_lanes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    backend_name TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL DEFAULT '',
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    priority INTEGER NOT NULL DEFAULT 100,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, name)
                )
            """)

    def register_account(
        self, *, name: str, provider: str, auth_kind: str,
        config: dict[str, Any] | None = None, secret_ref: str | None = None,
        enabled: bool = True, priority: int = 100, tags: list[str] | None = None,
    ) -> int:
        if not name.strip() or not provider.strip():
            raise ValueError("account name and provider are required")
        if auth_kind not in AUTH_KINDS:
            raise ValueError(f"unsupported auth_kind '{auth_kind}'")
        config = config or {}
        forbidden = _contains_inline_secret(config)
        if forbidden:
            raise ValueError(
                f"inline secret field '{forbidden}' is not allowed; use secret_ref instead"
            )
        validate_secret_ref(secret_ref)
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO accounts
                    (name, provider, auth_kind, config_json, secret_ref, enabled,
                     priority, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    provider = excluded.provider,
                    auth_kind = excluded.auth_kind,
                    config_json = excluded.config_json,
                    secret_ref = excluded.secret_ref,
                    enabled = excluded.enabled,
                    priority = excluded.priority,
                    tags_json = excluded.tags_json,
                    updated_at = excluded.updated_at
            """, (
                name.strip(), provider.strip(), auth_kind, json.dumps(config), secret_ref,
                int(enabled), int(priority), json.dumps(tags or []), now, now,
            ))
            row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name.strip(),)).fetchone()
            return int(row["id"])

    def get_account(self, name_or_id: str | int) -> Account | None:
        with closing(self._connect()) as conn:
            if isinstance(name_or_id, int):
                row = conn.execute("SELECT * FROM accounts WHERE id = ?", (name_or_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM accounts WHERE name = ?", (name_or_id,)).fetchone()
        return self._row_to_account(row) if row else None

    def list_accounts(self, *, enabled_only: bool = False, provider: str | None = None) -> list[Account]:
        clauses, params = [], []
        if enabled_only:
            clauses.append("enabled = 1")
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        query = "SELECT * FROM accounts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY priority ASC, name ASC"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_account(row) for row in rows]

    def remove_account(self, name_or_id: str | int) -> bool:
        with closing(self._connect()) as conn, conn:
            field = "id" if isinstance(name_or_id, int) else "name"
            result = conn.execute(f"DELETE FROM accounts WHERE {field} = ?", (name_or_id,))
        return result.rowcount > 0

    def register_lane(
        self, *, account: str | int, name: str, backend_name: str,
        model: str = "", capabilities: dict[str, Any] | None = None,
        priority: int = 100, enabled: bool = True,
    ) -> int:
        owner = self.get_account(account)
        if owner is None:
            raise ValueError(f"account '{account}' not found")
        if not name.strip() or not backend_name.strip():
            raise ValueError("lane name and backend_name are required")
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO account_lanes
                    (account_id, name, backend_name, model, capabilities_json,
                     priority, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, name) DO UPDATE SET
                    backend_name = excluded.backend_name,
                    model = excluded.model,
                    capabilities_json = excluded.capabilities_json,
                    priority = excluded.priority,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
            """, (
                owner.id, name.strip(), backend_name.strip(), model,
                json.dumps(capabilities or {}), int(priority), int(enabled), now, now,
            ))
            row = conn.execute(
                "SELECT id FROM account_lanes WHERE account_id = ? AND name = ?",
                (owner.id, name.strip()),
            ).fetchone()
            return int(row["id"])

    def list_lanes(self, *, account: str | int | None = None, enabled_only: bool = False) -> list[AccountLane]:
        clauses, params = [], []
        if account is not None:
            owner = self.get_account(account)
            if owner is None:
                return []
            clauses.append("l.account_id = ?")
            params.append(owner.id)
        if enabled_only:
            clauses.append("l.enabled = 1 AND a.enabled = 1")
        query = (
            "SELECT l.*, a.name AS account_name FROM account_lanes l "
            "JOIN accounts a ON a.id = l.account_id"
        )
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY l.priority ASC, a.name ASC, l.name ASC"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_lane(row) for row in rows]

    def select_enabled_lane(self, account: str | int) -> AccountLane | None:
        """Return the account's deterministic enabled lane, if it has one."""
        lanes = self.list_lanes(account=account)
        enabled = [lane for lane in lanes if lane.enabled]
        return min(enabled, key=lambda lane: (lane.priority, lane.name, lane.id)) if enabled else None

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"], name=row["name"], provider=row["provider"],
            auth_kind=row["auth_kind"], config=json.loads(row["config_json"]),
            secret_ref=row["secret_ref"], enabled=bool(row["enabled"]),
            priority=row["priority"], tags=json.loads(row["tags_json"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_lane(row: sqlite3.Row) -> AccountLane:
        return AccountLane(
            id=row["id"], account_id=row["account_id"], account_name=row["account_name"],
            name=row["name"], backend_name=row["backend_name"], model=row["model"],
            capabilities=json.loads(row["capabilities_json"]), priority=row["priority"],
            enabled=bool(row["enabled"]), created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def import_backends(accounts: AccountRegistry, backends: Any) -> list[str]:
    """Idempotently expose existing router rows as named accounts and lanes."""
    imported: list[str] = []
    kind_by_backend = {
        "cli": "cli_profile", "api_key": "api_key",
        "browser_session": "browser_session", "local_model": "local",
    }
    for backend in backends.list_all():
        auth_kind = kind_by_backend.get(backend.backend_type)
        if auth_kind is None:
            continue
        cli_name = str(backend.config.get("cli_name") or "")
        if backend.pool_name == "g4f-gateway" or backend.name.startswith("g4f-"):
            provider, auth_kind = "g4f", "browser_session"
        elif cli_name.startswith("profile_"):
            provider = "antigravity"
        else:
            provider = backend.config.get("provider") or cli_name
            if not provider:
                provider = next(
                    (candidate for candidate in ("codex", "claude", "qwen", "gemini", "antigravity")
                     if candidate in backend.name.lower()),
                    backend.backend_type,
                )
        existing = accounts.get_account(backend.name)
        if existing is None:
            accounts.register_account(
                name=backend.name, provider=str(provider), auth_kind=auth_kind,
                config=_strip_inline_secrets(backend.config), enabled=backend.enabled,
                priority=backend.priority,
                tags=[backend.pool_name] if backend.pool_name else [],
            )
        elif (
            existing.provider == "local_model"
            or existing.provider.startswith("profile_")
        ) and provider not in {"local_model", existing.provider}:
            # One-time correction for accounts first imported from a proxy row:
            # preserve user metadata while replacing transport-derived identity.
            accounts.register_account(
                name=existing.name, provider=str(provider), auth_kind=auth_kind,
                config=existing.config, secret_ref=existing.secret_ref,
                enabled=existing.enabled, priority=existing.priority, tags=existing.tags,
            )
        if not accounts.list_lanes(account=backend.name):
            accounts.register_lane(
                account=backend.name, name="default", backend_name=backend.name,
                model=str(backend.config.get("model") or backend.config.get("model_name") or ""),
                capabilities=backend.capabilities, priority=backend.priority,
                enabled=backend.enabled,
            )
        imported.append(backend.name)
    return imported
