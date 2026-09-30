"""Project → Part → ToolInstance registry.

Data model
----------

tool_instances
    A concrete, callable MCP endpoint (stdio subprocess or HTTP URL).
    The same *type* of tool (e.g. "notion") can exist as many instances,
    each with its own config (different tokens, vault paths, etc.).

    transport: "stdio" | "http" | "sse"
    config:    transport-specific — see ToolInstance docs below.

projects
    A named logical project (e.g. "college-assistant", "ficsit-control").

parts
    A named sub-component of a project (e.g. "moodle-agent", "study-agent").
    Parts are the unit of tool scoping: each part carries its own ordered
    list of tool_instance_ids.  The same tool_instance can appear in multiple
    parts (shared instance) or each part can have its own copy (own instance).

part_tools   (join table)
    Many-to-many between parts and tool instances, with an explicit `position`
    column so tool ordering inside a part is deterministic and meaningful
    (lower position = listed first to the model).

Design goals
------------
* Completely independent of registry.py / backends — this is a parallel axis
  of the router: *what tools a model call can see*, not *which model to call*.
* Importable and testable on its own (no FastAPI, no adapters).
* Same upsert-by-name / ALTER-safe migration pattern as registry.py.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any

DEFAULT_DB_PATH = database_path(
    "tool_registry.db",
    env_var="HERALD_TOOL_REGISTRY_DB",
    legacy_path=Path(__file__).resolve().parent / "tool_registry.db",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ToolInstance:
    """A single concrete MCP-server endpoint.

    transport / config shapes
    -------------------------
    stdio:
        config = {"command": ["python", "my_server.py"], "env": {...}}
        The router will manage process lifecycle for stdio servers (spawn on
        first use, keep alive, restart on crash).

    http:
        config = {"url": "http://host:port/mcp"}
        Streamable-HTTP MCP transport (the modern default).

    sse:
        config = {"url": "http://host:port/sse"}
        Legacy SSE MCP transport.
    """
    id: int
    name: str
    description: str
    transport: str          # "stdio" | "http" | "sse"
    config: dict[str, Any]
    tags: list[str]
    created_at: str
    updated_at: str
    package_name: str = ""
    version: str = "unversioned"
    scope: str = "global"
    project_id: int | None = None
    source: dict[str, Any] = field(default_factory=dict)
    isolation_key: str = ""
    alias: str | None = None
    allowed_tools: list[str] | None = None


@dataclass
class MCPGroup:
    """A reusable allowlisted collection of MCP server instances."""

    id: int
    name: str
    description: str
    created_at: str
    updated_at: str


@dataclass
class Project:
    id: int
    name: str
    description: str
    created_at: str
    updated_at: str


@dataclass
class Part:
    id: int
    project_id: int
    project_name: str
    name: str
    description: str
    created_at: str
    updated_at: str
    tool_instance_ids: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------
    # Schema (migration-safe: ALTER IF NOT EXISTS pattern)
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tool_instances (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT    NOT NULL UNIQUE,
                    description TEXT    NOT NULL DEFAULT '',
                    transport   TEXT    NOT NULL CHECK(transport IN ('stdio','http','sse')),
                    config_json TEXT    NOT NULL DEFAULT '{}',
                    tags_json   TEXT    NOT NULL DEFAULT '[]',
                    created_at  TEXT    NOT NULL,
                    updated_at  TEXT    NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT    NOT NULL UNIQUE,
                    description TEXT    NOT NULL DEFAULT '',
                    created_at  TEXT    NOT NULL,
                    updated_at  TEXT    NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS parts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    name        TEXT    NOT NULL,
                    description TEXT    NOT NULL DEFAULT '',
                    created_at  TEXT    NOT NULL,
                    updated_at  TEXT    NOT NULL,
                    UNIQUE(project_id, name)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS part_tools (
                    part_id          INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
                    tool_instance_id INTEGER NOT NULL REFERENCES tool_instances(id) ON DELETE CASCADE,
                    position         INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (part_id, tool_instance_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_parts_project ON parts(project_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_part_tools_part ON part_tools(part_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_part_tools_instance ON part_tools(tool_instance_id)")
            instance_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tool_instances)")}
            migrations = {
                "package_name": "TEXT NOT NULL DEFAULT ''",
                "version": "TEXT NOT NULL DEFAULT 'unversioned'",
                "scope": "TEXT NOT NULL DEFAULT 'global'",
                "project_id": "INTEGER",
                "source_json": "TEXT NOT NULL DEFAULT '{}'",
                "isolation_key": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in migrations.items():
                if column not in instance_cols:
                    conn.execute(f"ALTER TABLE tool_instances ADD COLUMN {column} {definition}")
            binding_cols = {row["name"] for row in conn.execute("PRAGMA table_info(part_tools)")}
            if "alias" not in binding_cols:
                conn.execute("ALTER TABLE part_tools ADD COLUMN alias TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_instances_scope ON tool_instances(scope, project_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mcp_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mcp_group_tools (
                    group_id INTEGER NOT NULL REFERENCES mcp_groups(id) ON DELETE CASCADE,
                    tool_instance_id INTEGER NOT NULL REFERENCES tool_instances(id) ON DELETE CASCADE,
                    allowed_tools_json TEXT,
                    alias TEXT,
                    position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, tool_instance_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mcp_group_bindings (
                    group_id INTEGER NOT NULL REFERENCES mcp_groups(id) ON DELETE CASCADE,
                    target_type TEXT NOT NULL CHECK(target_type IN ('global','project','part','cli')),
                    target_key TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, target_type, target_key)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_group_tools_group ON mcp_group_tools(group_id, position)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_group_bindings_target ON mcp_group_bindings(target_type, target_key, position)")

    # ------------------------------------------------------------------
    # ToolInstance CRUD
    # ------------------------------------------------------------------

    def register_tool_instance(
        self,
        *,
        name: str,
        transport: str,
        config: dict[str, Any],
        description: str = "",
        tags: list[str] | None = None,
        package_name: str = "",
        version: str = "unversioned",
        scope: str = "global",
        project: str | int | None = None,
        source: dict[str, Any] | None = None,
        isolation_key: str = "",
    ) -> int:
        """Upsert by name.  Re-registering updates config/transport without
        touching which parts reference this instance."""
        if scope not in {"global", "project"}:
            raise ValueError("tool scope must be 'global' or 'project'")
        project_id: int | None = None
        if scope == "project":
            if project is None:
                raise ValueError("project-scoped tools require a project")
            owner = self.get_project(project)
            if owner is None:
                raise ValueError(f"project '{project}' not found")
            project_id = owner.id
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO tool_instances
                    (name, description, transport, config_json, tags_json, created_at, updated_at,
                     package_name, version, scope, project_id, source_json, isolation_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description  = excluded.description,
                    transport    = excluded.transport,
                    config_json  = excluded.config_json,
                    tags_json    = excluded.tags_json,
                    package_name = excluded.package_name,
                    version      = excluded.version,
                    scope        = excluded.scope,
                    project_id   = excluded.project_id,
                    source_json  = excluded.source_json,
                    isolation_key = excluded.isolation_key,
                    updated_at   = excluded.updated_at
            """, (name, description, transport, json.dumps(config),
                  json.dumps(tags or []), now, now, package_name or name, version,
                  scope, project_id, json.dumps(source or {}), isolation_key or name))
            row = conn.execute(
                "SELECT id FROM tool_instances WHERE name = ?", (name,)
            ).fetchone()
            return int(row["id"])

    def get_tool_instance(self, name_or_id: str | int) -> ToolInstance | None:
        with closing(self._connect()) as conn:
            if isinstance(name_or_id, int):
                row = conn.execute(
                    "SELECT * FROM tool_instances WHERE id = ?", (name_or_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tool_instances WHERE name = ?", (name_or_id,)
                ).fetchone()
        return self._row_to_tool_instance(row) if row else None

    def list_tool_instances(
        self, *, scope: str | None = None, project: str | int | None = None,
        include_global: bool = False,
    ) -> list[ToolInstance]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if project is not None:
            owner = self.get_project(project)
            if owner is None:
                return []
            if include_global:
                clauses.append("(project_id = ? OR scope = 'global')")
            else:
                clauses.append("project_id = ?")
            params.append(owner.id)
        query = "SELECT * FROM tool_instances"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY name ASC"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_tool_instance(r) for r in rows]

    def remove_tool_instance(self, name_or_id: str | int) -> bool:
        """Remove a tool instance. Cascade removes part_tools rows."""
        with closing(self._connect()) as conn, conn:
            if isinstance(name_or_id, int):
                cur = conn.execute("DELETE FROM tool_instances WHERE id = ?", (name_or_id,))
            else:
                cur = conn.execute("DELETE FROM tool_instances WHERE name = ?", (name_or_id,))
        return cur.rowcount > 0

    def _row_to_tool_instance(self, row: sqlite3.Row) -> ToolInstance:
        return ToolInstance(
            id=row["id"], name=row["name"], description=row["description"],
            transport=row["transport"], config=json.loads(row["config_json"]),
            tags=json.loads(row["tags_json"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            package_name=row["package_name"] or row["name"], version=row["version"],
            scope=row["scope"], project_id=row["project_id"],
            source=json.loads(row["source_json"]), isolation_key=row["isolation_key"] or row["name"],
        )

    # ------------------------------------------------------------------
    # Project CRUD
    # ------------------------------------------------------------------

    def register_project(self, *, name: str, description: str = "") -> int:
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO projects (name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    updated_at  = excluded.updated_at
            """, (name, description, now, now))
            row = conn.execute(
                "SELECT id FROM projects WHERE name = ?", (name,)
            ).fetchone()
            return int(row["id"])

    def get_project(self, name_or_id: str | int) -> Project | None:
        with closing(self._connect()) as conn:
            if isinstance(name_or_id, int):
                row = conn.execute(
                    "SELECT * FROM projects WHERE id = ?", (name_or_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM projects WHERE name = ?", (name_or_id,)
                ).fetchone()
        return self._row_to_project(row) if row else None

    def list_projects(self) -> list[Project]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY name ASC"
            ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def remove_project(self, name_or_id: str | int) -> bool:
        """Remove project and cascade to its parts and part_tools."""
        with closing(self._connect()) as conn, conn:
            if isinstance(name_or_id, int):
                cur = conn.execute("DELETE FROM projects WHERE id = ?", (name_or_id,))
            else:
                cur = conn.execute("DELETE FROM projects WHERE name = ?", (name_or_id,))
        return cur.rowcount > 0

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"], name=row["name"], description=row["description"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Part CRUD
    # ------------------------------------------------------------------

    def register_part(
        self,
        *,
        project: str | int,
        name: str,
        description: str = "",
    ) -> int:
        """Create or update a part within a project. 'project' can be a
        project name or its integer id."""
        proj = self.get_project(project)
        if proj is None:
            raise ValueError(f"project '{project}' not found — register it first")
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO parts (project_id, name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, name) DO UPDATE SET
                    description = excluded.description,
                    updated_at  = excluded.updated_at
            """, (proj.id, name, description, now, now))
            row = conn.execute(
                "SELECT id FROM parts WHERE project_id = ? AND name = ?",
                (proj.id, name),
            ).fetchone()
            return int(row["id"])

    def get_part(self, *, project: str | int, part: str | int) -> Part | None:
        proj = self.get_project(project)
        if proj is None:
            return None
        with closing(self._connect()) as conn:
            if isinstance(part, int):
                row = conn.execute(
                    "SELECT * FROM parts WHERE id = ? AND project_id = ?",
                    (part, proj.id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM parts WHERE name = ? AND project_id = ?",
                    (part, proj.id),
                ).fetchone()
            if row is None:
                return None
            tool_rows = conn.execute(
                "SELECT tool_instance_id FROM part_tools WHERE part_id = ? ORDER BY position ASC",
                (row["id"],),
            ).fetchall()
        p = self._row_to_part(row, proj.name)
        p.tool_instance_ids = [r["tool_instance_id"] for r in tool_rows]
        return p

    def list_parts(self, project: str | int) -> list[Part]:
        proj = self.get_project(project)
        if proj is None:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM parts WHERE project_id = ? ORDER BY name ASC",
                (proj.id,),
            ).fetchall()
            parts = []
            for row in rows:
                tool_rows = conn.execute(
                    "SELECT tool_instance_id FROM part_tools WHERE part_id = ? ORDER BY position ASC",
                    (row["id"],),
                ).fetchall()
                p = self._row_to_part(row, proj.name)
                p.tool_instance_ids = [r["tool_instance_id"] for r in tool_rows]
                parts.append(p)
        return parts

    def remove_part(self, *, project: str | int, part: str | int) -> bool:
        p = self.get_part(project=project, part=part)
        if p is None:
            return False
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM parts WHERE id = ?", (p.id,))
        return True

    def _row_to_part(self, row: sqlite3.Row, project_name: str) -> Part:
        return Part(
            id=row["id"], project_id=row["project_id"], project_name=project_name,
            name=row["name"], description=row["description"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Part ↔ ToolInstance bindings
    # ------------------------------------------------------------------

    def bind_tool_to_part(
        self,
        *,
        project: str | int,
        part: str | int,
        tool: str | int,
        position: int | None = None,
        alias: str | None = None,
    ) -> None:
        """Attach a tool instance to a part. If position is None, appends
        after the current last tool. If the binding already exists, updates
        position only if a new position is explicitly provided."""
        p = self.get_part(project=project, part=part)
        if p is None:
            raise ValueError(f"part '{part}' not found in project '{project}'")
        ti = self.get_tool_instance(tool)
        if ti is None:
            raise ValueError(f"tool instance '{tool}' not found")
        if ti.scope == "project" and ti.project_id != p.project_id:
            raise ValueError(
                f"tool instance '{ti.name}' belongs to another project and cannot be bound to {project}/{part}"
            )

        if position is None:
            # auto-position: current max + 1
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT MAX(position) as m FROM part_tools WHERE part_id = ?",
                    (p.id,),
                ).fetchone()
            position = (row["m"] or -1) + 1

        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO part_tools (part_id, tool_instance_id, position, alias)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(part_id, tool_instance_id) DO UPDATE SET
                    position = excluded.position,
                    alias = excluded.alias
            """, (p.id, ti.id, position, alias))

    def unbind_tool_from_part(
        self,
        *,
        project: str | int,
        part: str | int,
        tool: str | int,
    ) -> bool:
        p = self.get_part(project=project, part=part)
        if p is None:
            return False
        ti = self.get_tool_instance(tool)
        if ti is None:
            return False
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM part_tools WHERE part_id = ? AND tool_instance_id = ?",
                (p.id, ti.id),
            )
        return cur.rowcount > 0

    def set_part_tools(
        self,
        *,
        project: str | int,
        part: str | int,
        tool_names_or_ids: list[str | int],
    ) -> None:
        """Replace a part's tool list entirely (ordered). Existing bindings
        are removed and replaced with exactly this list, in order. Useful for
        bulk-setting from a project config file."""
        p = self.get_part(project=project, part=part)
        if p is None:
            raise ValueError(f"part '{part}' not found in project '{project}'")
        instances = []
        for t in tool_names_or_ids:
            ti = self.get_tool_instance(t)
            if ti is None:
                raise ValueError(f"tool instance '{t}' not found")
            if ti.scope == "project" and ti.project_id != p.project_id:
                raise ValueError(
                    f"tool instance '{ti.name}' belongs to another project and cannot be bound to {project}/{part}"
                )
            instances.append(ti)
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM part_tools WHERE part_id = ?", (p.id,))
            for pos, ti in enumerate(instances):
                conn.execute(
                    "INSERT INTO part_tools (part_id, tool_instance_id, position) VALUES (?, ?, ?)",
                    (p.id, ti.id, pos),
                )

    # ------------------------------------------------------------------
    # Scope resolution — the key query used by the MCP layer
    # ------------------------------------------------------------------

    def resolve_scope(
        self,
        *,
        project: str | int,
        part: str | int,
    ) -> list[ToolInstance]:
        """Return the ordered list of ToolInstances bound to a specific
        project+part scope.  This is what the MCP dispatch layer calls to
        know which tools to make available for a given call."""
        p = self.get_part(project=project, part=part)
        if p is None:
            return []
        if not p.tool_instance_ids:
            return []
        with closing(self._connect()) as conn:
            # fetch all at once, preserving part_tools ordering
            placeholders = ",".join("?" * len(p.tool_instance_ids))
            rows = conn.execute(
                f"SELECT ti.*, pt.alias AS bound_alias FROM tool_instances ti "
                f"JOIN part_tools pt ON pt.tool_instance_id = ti.id "
                f"WHERE pt.part_id = ? AND ti.id IN ({placeholders})",
                [p.id, *p.tool_instance_ids],
            ).fetchall()
        # restore part ordering (SQL IN does not preserve order)
        by_id = {r["id"]: r for r in rows}
        resolved: list[ToolInstance] = []
        for tid in p.tool_instance_ids:
            if tid in by_id:
                instance = self._row_to_tool_instance(by_id[tid])
                instance.alias = by_id[tid]["bound_alias"]
                resolved.append(instance)
        return resolved

    # ------------------------------------------------------------------
    # Shared-instance query — which parts use a given tool instance?
    # ------------------------------------------------------------------

    def parts_using_tool(self, tool: str | int) -> list[dict[str, Any]]:
        """Return every (project_name, part_name, position) that references
        this tool instance — useful for understanding sharing relationships."""
        ti = self.get_tool_instance(tool)
        if ti is None:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute("""
                SELECT p.name AS part_name, proj.name AS project_name, pt.position
                FROM part_tools pt
                JOIN parts p ON p.id = pt.part_id
                JOIN projects proj ON proj.id = p.project_id
                WHERE pt.tool_instance_id = ?
                ORDER BY proj.name, p.name
            """, (ti.id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Controlled MCP groups and access bindings
    # ------------------------------------------------------------------

    def register_mcp_group(self, name: str, description: str = "") -> int:
        if not name.strip():
            raise ValueError("MCP group name cannot be empty")
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO mcp_groups (name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description, updated_at=excluded.updated_at
            """, (name, description, now, now))
            row = conn.execute("SELECT id FROM mcp_groups WHERE name=?", (name,)).fetchone()
        return int(row["id"])

    def get_mcp_group(self, name_or_id: str | int) -> MCPGroup | None:
        with closing(self._connect()) as conn:
            if isinstance(name_or_id, int):
                row = conn.execute("SELECT * FROM mcp_groups WHERE id=?", (name_or_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM mcp_groups WHERE name=?", (name_or_id,)).fetchone()
        return MCPGroup(**dict(row)) if row else None

    def list_mcp_groups(self) -> list[MCPGroup]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM mcp_groups ORDER BY name").fetchall()
        return [MCPGroup(**dict(row)) for row in rows]

    def remove_mcp_group(self, name_or_id: str | int) -> bool:
        group = self.get_mcp_group(name_or_id)
        if not group:
            return False
        with closing(self._connect()) as conn, conn:
            result = conn.execute("DELETE FROM mcp_groups WHERE id=?", (group.id,))
        return result.rowcount > 0

    def add_tool_to_mcp_group(
        self, *, group: str | int, tool: str | int,
        allowed_tools: list[str] | None = None, alias: str | None = None,
        position: int | None = None,
    ) -> None:
        owner = self.get_mcp_group(group)
        instance = self.get_tool_instance(tool)
        if not owner:
            raise ValueError(f"MCP group '{group}' not found")
        if not instance:
            raise ValueError(f"tool instance '{tool}' not found")
        normalized = None if not allowed_tools else sorted({str(item) for item in allowed_tools if str(item)})
        if position is None:
            with closing(self._connect()) as conn:
                existing = conn.execute(
                    "SELECT position FROM mcp_group_tools WHERE group_id=? AND tool_instance_id=?",
                    (owner.id, instance.id),
                ).fetchone()
                row = conn.execute(
                    "SELECT MAX(position) AS m FROM mcp_group_tools WHERE group_id=?", (owner.id,),
                ).fetchone()
            position = int(existing["position"]) if existing else int(row["m"] if row["m"] is not None else -1) + 1
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO mcp_group_tools
                    (group_id, tool_instance_id, allowed_tools_json, alias, position)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, tool_instance_id) DO UPDATE SET
                    allowed_tools_json=excluded.allowed_tools_json,
                    alias=excluded.alias, position=excluded.position
            """, (owner.id, instance.id,
                  json.dumps(normalized) if normalized is not None else None,
                  alias, position))

    def remove_tool_from_mcp_group(self, *, group: str | int, tool: str | int) -> bool:
        owner = self.get_mcp_group(group)
        instance = self.get_tool_instance(tool)
        if not owner or not instance:
            return False
        with closing(self._connect()) as conn, conn:
            result = conn.execute(
                "DELETE FROM mcp_group_tools WHERE group_id=? AND tool_instance_id=?",
                (owner.id, instance.id),
            )
        return result.rowcount > 0

    def bind_mcp_group(
        self, *, group: str | int, target_type: str, target_key: str = "*",
        position: int | None = None,
    ) -> None:
        owner = self.get_mcp_group(group)
        if not owner:
            raise ValueError(f"MCP group '{group}' not found")
        if target_type not in {"global", "project", "part", "cli"}:
            raise ValueError("target type must be global, project, part, or cli")
        target_key = "*" if target_type == "global" else target_key.strip()
        if not target_key:
            raise ValueError(f"{target_type} bindings require a target")
        if target_type == "project" and not self.get_project(target_key):
            raise ValueError(f"project '{target_key}' not found")
        if target_type == "part":
            if "/" not in target_key:
                raise ValueError("part targets use PROJECT/PART")
            project_name, part_name = target_key.split("/", 1)
            if not self.get_part(project=project_name, part=part_name):
                raise ValueError(f"part '{target_key}' not found")
        if position is None:
            with closing(self._connect()) as conn:
                existing = conn.execute("""
                    SELECT position FROM mcp_group_bindings
                    WHERE group_id=? AND target_type=? AND target_key=?
                """, (owner.id, target_type, target_key)).fetchone()
                row = conn.execute(
                    "SELECT MAX(position) AS m FROM mcp_group_bindings WHERE target_type=? AND target_key=?",
                    (target_type, target_key),
                ).fetchone()
            position = int(existing["position"]) if existing else int(row["m"] if row["m"] is not None else -1) + 1
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                INSERT INTO mcp_group_bindings (group_id, target_type, target_key, position)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, target_type, target_key) DO UPDATE SET position=excluded.position
            """, (owner.id, target_type, target_key, position))

    def unbind_mcp_group(self, *, group: str | int, target_type: str, target_key: str = "*") -> bool:
        owner = self.get_mcp_group(group)
        if not owner:
            return False
        target_key = "*" if target_type == "global" else target_key
        with closing(self._connect()) as conn, conn:
            result = conn.execute("""
                DELETE FROM mcp_group_bindings
                WHERE group_id=? AND target_type=? AND target_key=?
            """, (owner.id, target_type, target_key))
        return result.rowcount > 0

    def describe_mcp_group(self, group: str | int) -> dict[str, Any] | None:
        owner = self.get_mcp_group(group)
        if not owner:
            return None
        with closing(self._connect()) as conn:
            tools = conn.execute("""
                SELECT ti.name, ti.scope, ti.package_name, ti.version,
                       gt.allowed_tools_json, gt.alias, gt.position
                FROM mcp_group_tools gt
                JOIN tool_instances ti ON ti.id=gt.tool_instance_id
                WHERE gt.group_id=? ORDER BY gt.position, ti.name
            """, (owner.id,)).fetchall()
            bindings = conn.execute("""
                SELECT target_type, target_key, position FROM mcp_group_bindings
                WHERE group_id=? ORDER BY target_type, position, target_key
            """, (owner.id,)).fetchall()
        return {
            "id": owner.id, "name": owner.name, "description": owner.description,
            "created_at": owner.created_at, "updated_at": owner.updated_at,
            "tools": [{
                "name": row["name"], "scope": row["scope"],
                "package": row["package_name"], "version": row["version"],
                "allowed_tools": json.loads(row["allowed_tools_json"]) if row["allowed_tools_json"] else None,
                "alias": row["alias"], "position": row["position"],
            } for row in tools],
            "bindings": [dict(row) for row in bindings],
        }

    def resolve_mcp_access(
        self, *, groups: list[str] | None = None, project: str | None = None,
        part: str | None = None, profile: str | None = None,
    ) -> list[ToolInstance]:
        """Resolve legacy part bindings plus reusable groups at each access level."""
        resolved: list[ToolInstance] = []
        if bool(project) != bool(part):
            raise ValueError("project and part must be provided together")
        if project and part:
            resolved.extend(self.resolve_scope(project=project, part=part))

        group_ids: list[int] = []
        with closing(self._connect()) as conn:
            if groups:
                for name in groups:
                    row = conn.execute("SELECT id FROM mcp_groups WHERE name=?", (name,)).fetchone()
                    if not row:
                        raise ValueError(f"MCP group '{name}' not found")
                    group_ids.append(int(row["id"]))
            else:
                targets = [("global", "*")]
                if project:
                    targets.extend([("project", project), ("part", f"{project}/{part}")])
                if profile:
                    targets.append(("cli", profile))
                for target_type, target_key in targets:
                    rows = conn.execute("""
                        SELECT group_id FROM mcp_group_bindings
                        WHERE target_type=? AND target_key=? ORDER BY position, group_id
                    """, (target_type, target_key)).fetchall()
                    group_ids.extend(int(row["group_id"]) for row in rows)

            project_owner = self.get_project(project) if project else None
            for group_id in dict.fromkeys(group_ids):
                rows = conn.execute("""
                    SELECT ti.*, gt.allowed_tools_json, gt.alias AS group_alias
                    FROM mcp_group_tools gt
                    JOIN tool_instances ti ON ti.id=gt.tool_instance_id
                    WHERE gt.group_id=? ORDER BY gt.position, ti.name
                """, (group_id,)).fetchall()
                for row in rows:
                    if row["scope"] == "project" and (
                        project_owner is None or row["project_id"] != project_owner.id
                    ):
                        continue
                    instance = self._row_to_tool_instance(row)
                    instance.alias = row["group_alias"] or instance.alias
                    instance.allowed_tools = (
                        json.loads(row["allowed_tools_json"])
                        if row["allowed_tools_json"] else None
                    )
                    resolved.append(instance)

        unique: dict[tuple[int, str | None], ToolInstance] = {}
        for instance in resolved:
            key = (instance.id, instance.alias)
            previous = unique.get(key)
            if previous and previous.allowed_tools is not None:
                if instance.allowed_tools is None:
                    previous.allowed_tools = None
                else:
                    previous.allowed_tools = sorted(set(previous.allowed_tools + instance.allowed_tools))
            elif not previous:
                unique[key] = instance
        return list(unique.values())
