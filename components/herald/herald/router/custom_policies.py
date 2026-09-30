"""Persistent store for user-defined routing policies declared in router.yaml.

Registration happens over HTTP (RouterClient -> POST /routing-policies) into
the running router server process, same as every other router.yaml construct
(projects, parts, tool instances) -- an in-process dict mutation during
`herald register .` would never reach the actual server process, since that's
a separate process. This mirrors registry.py's SQLite/WAL pattern so a
custom policy survives a router restart.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any

DEFAULT_DB_PATH = database_path(
    "custom_policies.db",
    env_var="HERALD_CUSTOM_POLICIES_DB",
    legacy_path=Path(__file__).resolve().parent / "custom_policies.db",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CustomPolicyStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
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
                CREATE TABLE IF NOT EXISTS custom_policies (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    automatic_order_json TEXT NOT NULL,
                    delegate_types_json TEXT NOT NULL,
                    free_only INTEGER NOT NULL DEFAULT 0,
                    compact_tool_catalog INTEGER NOT NULL DEFAULT 0,
                    tool_bridge TEXT,
                    project TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def upsert(
        self,
        name: str,
        *,
        description: str = "",
        automatic_order: list[str],
        delegate_types: list[str],
        free_only: bool = False,
        compact_tool_catalog: bool = False,
        tool_bridge: str | None = None,
        project: str | None = None,
    ) -> None:
        if not automatic_order:
            raise ValueError("automatic_order must be non-empty")
        if not delegate_types:
            raise ValueError("delegate_types must be non-empty")
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO custom_policies
                    (name, description, automatic_order_json, delegate_types_json,
                     free_only, compact_tool_catalog, tool_bridge, project, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    automatic_order_json = excluded.automatic_order_json,
                    delegate_types_json = excluded.delegate_types_json,
                    free_only = excluded.free_only,
                    compact_tool_catalog = excluded.compact_tool_catalog,
                    tool_bridge = excluded.tool_bridge,
                    project = excluded.project,
                    updated_at = excluded.updated_at
                """,
                (
                    name, description, json.dumps(automatic_order), json.dumps(delegate_types),
                    int(free_only), int(compact_tool_catalog), tool_bridge, project, _now_iso(),
                ),
            )

    def get(self, name: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM custom_policies WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "description": row["description"],
            "automatic_order": tuple(json.loads(row["automatic_order_json"])),
            "delegate_types": frozenset(json.loads(row["delegate_types_json"])),
            "free_only": bool(row["free_only"]),
            "compact_tool_catalog": bool(row["compact_tool_catalog"]),
            "tool_bridge": row["tool_bridge"],
        }

    def list_all(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT name FROM custom_policies ORDER BY name ASC").fetchall()
        return [self.get(r["name"]) for r in rows]


_store: CustomPolicyStore | None = None


def get_store() -> CustomPolicyStore:
    global _store
    if _store is None:
        _store = CustomPolicyStore()
    return _store
