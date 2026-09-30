"""Risk-tiered approval gate for coding_tools.py -- roadmap #4.

Two orthogonal axes, not one flat approve/deny gate:

  - capability ceiling: what the process is technically allowed to do
    (read-only / workspace-write / full-access), borrowed from Codex CLI's
    sandbox_mode. A ceiling below what a call needs is an outright rejection,
    not a pending approval.
  - operation risk tier: SAFE / REQUIRES_CONFIRMATION / DESTRUCTIVE /
    IRREVERSIBLE, borrowed from the user's own Assistant2 project's
    role x operation-type dispatcher. Only DESTRUCTIVE/IRREVERSIBLE calls
    actually pause and wait when gating is enabled; SAFE/REQUIRES_CONFIRMATION
    proceed but are logged.

Default posture: OFF. The user runs bypass-permissions everywhere else by
habit -- this whole module is opt-in, controlled by env vars set at process
launch (HERALD_CAPABILITY_CEILING, HERALD_REQUIRE_APPROVAL). Unset means
full-access + no gating, i.e. today's existing behavior, unchanged.

Known limitation: coding_tools.py currently runs as ONE global stdio MCP
subprocess shared by every part (see bootstrap.py's _register_coding_tools),
not one instance per part. So gating today is process-wide, not truly
per-part -- router.yaml's `require_approval`/`capability_ceiling` fields
only take effect if/when coding_tools.py is instantiated per-part with its
own env, which it isn't yet. Flagged here rather than pretended around.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = database_path(
    "approvals.db",
    env_var="HERALD_APPROVALS_DB",
    legacy_path=Path(__file__).resolve().parent / "approvals.db",
)

RISK_SAFE = "SAFE"
RISK_CONFIRM = "REQUIRES_CONFIRMATION"
RISK_DESTRUCTIVE = "DESTRUCTIVE"
RISK_IRREVERSIBLE = "IRREVERSIBLE"
RISK_ORDER = (RISK_SAFE, RISK_CONFIRM, RISK_DESTRUCTIVE, RISK_IRREVERSIBLE)

CEILING_READ_ONLY = "read-only"
CEILING_WORKSPACE_WRITE = "workspace-write"
CEILING_FULL_ACCESS = "full-access"

# Best-effort, pattern-based -- not a static analyzer. Checked in order;
# first match wins. IRREVERSIBLE patterns checked before DESTRUCTIVE ones.
_IRREVERSIBLE_COMMAND_PATTERNS = [
    r"\brm\s+-[a-z]*r[a-z]*f\b", r"\brm\s+-[a-z]*f[a-z]*r\b",  # rm -rf / -fr, any flag order
    r"\bgit\s+reset\s+--hard\b",
    r"\bdrop\s+(database|table)\b",
    r"\bformat\s+[a-z]:\b",
    r"\bdel\s+/[sf].*\*",
]
_DESTRUCTIVE_COMMAND_PATTERNS = [
    r"\brm\s+-[a-z]*r\b", r"\brm\s+-[a-z]*f\b",
    r"\bgit\s+push\s+.*--force\b", r"\bgit\s+push\s+.*-f\b",
    r"\bgit\s+clean\s+-[a-z]*d\b",
    r"\bdel\b", r"\bremove-item\b", r"\btruncate\s+table\b",
    r"\bdocker\s+system\s+prune\b",
]


def classify_command_risk(command: str) -> str:
    lowered = command.lower()
    for pattern in _IRREVERSIBLE_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return RISK_IRREVERSIBLE
    for pattern in _DESTRUCTIVE_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return RISK_DESTRUCTIVE
    return RISK_CONFIRM  # arbitrary shell execution is never SAFE by default


def classify_tool_call(tool_name: str, args: dict[str, Any]) -> str:
    """Best-effort risk classification for a coding_tools.py call.
    Documented as best-effort/pattern-based, not exhaustive."""
    if tool_name in ("read_file", "list_directory", "search_code", "check_command", "check_build_log"):
        return RISK_SAFE

    if tool_name == "write_file":
        path = args.get("path", "")
        exists = False
        try:
            from herald.coding_tools import _safe_path
            exists = _safe_path(path, must_exist=False).exists()
        except Exception:
            pass
        return RISK_DESTRUCTIVE if exists else RISK_CONFIRM

    if tool_name == "edit_file":
        return RISK_CONFIRM

    if tool_name in ("run_command", "run_command_background"):
        return classify_command_risk(str(args.get("command", "")))

    if tool_name == "git_run":
        return classify_command_risk("git " + str(args.get("args", "")))

    if tool_name == "run_tests":
        return RISK_CONFIRM

    # Unknown tool: fail toward caution, not toward silently allowing it.
    return RISK_DESTRUCTIVE


_CEILING_ALLOWED_TOOLS = {
    CEILING_READ_ONLY: {"read_file", "list_directory", "search_code", "check_command", "check_build_log", "run_tests"},
    CEILING_WORKSPACE_WRITE: {
        "read_file", "list_directory", "search_code", "check_command", "check_build_log", "run_tests",
        "write_file", "edit_file",
    },
    CEILING_FULL_ACCESS: None,  # None = no restriction
}


def check_capability_ceiling(tool_name: str, ceiling: str) -> str | None:
    """Return an error string if `tool_name` exceeds `ceiling`, else None.
    This is an outright rejection, not a pending approval -- distinct from
    risk tiering below."""
    allowed = _CEILING_ALLOWED_TOOLS.get(ceiling)
    if allowed is None:
        return None
    if tool_name not in allowed:
        return (
            f"'{tool_name}' is not permitted under capability ceiling "
            f"'{ceiling}' (allowed: {sorted(allowed)})"
        )
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _summarize_args(args: dict[str, Any], limit: int = 500) -> str:
    text = json.dumps(args, default=str)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


class ApprovalStore:
    """SQLite-backed pending-approval queue and audit log."""

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
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    token TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    preview TEXT,
                    risk_tier TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT,
                    decision TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_name TEXT NOT NULL,
                    args_summary TEXT NOT NULL,
                    risk_tier TEXT NOT NULL,
                    capability_ceiling TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )

    def log(self, *, tool_name: str, args: dict[str, Any], risk_tier: str, ceiling: str, decision: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO audit_log (tool_name, args_summary, risk_tier, capability_ceiling, decision, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tool_name, _summarize_args(args), risk_tier, ceiling, decision, _now_iso()),
            )

    def create_pending(self, *, tool_name: str, args: dict[str, Any], risk_tier: str, preview: str = "") -> str:
        token = uuid.uuid4().hex
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO pending_approvals (token, tool_name, args_json, preview, risk_tier, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token, tool_name, json.dumps(args), preview, risk_tier, _now_iso()),
            )
        return token

    def get_pending(self, token: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM pending_approvals WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None

    def decide(self, token: str, decision: str) -> dict[str, Any] | None:
        row = self.get_pending(token)
        if row is None or row["decision"] is not None:
            return None
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE pending_approvals SET decided_at = ?, decision = ? WHERE token = ?",
                (_now_iso(), decision, token),
            )
        return row

    def list_pending(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM pending_approvals WHERE decision IS NULL ORDER BY requested_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]


_store = ApprovalStore()


def get_store() -> ApprovalStore:
    return _store


def gating_enabled() -> bool:
    return os.environ.get("HERALD_REQUIRE_APPROVAL", "").strip().lower() in ("1", "true", "yes")


def capability_ceiling() -> str:
    value = os.environ.get("HERALD_CAPABILITY_CEILING", CEILING_FULL_ACCESS).strip()
    return value if value in _CEILING_ALLOWED_TOOLS else CEILING_FULL_ACCESS


class CeilingExceeded(Exception):
    pass


class ApprovalPending(Exception):
    def __init__(self, token: str, message: str) -> None:
        super().__init__(message)
        self.token = token


def gate_call(tool_name: str, args: dict[str, Any], *, preview: str = "") -> None:
    """Call at the top of a gated coding-tool function. Raises
    CeilingExceeded if the ceiling forbids the call outright, or
    ApprovalPending if it's DESTRUCTIVE/IRREVERSIBLE and gating is on.
    No-ops (returns normally) for everything else, including when gating is
    disabled -- matching the "opt-in, default off" posture."""
    ceiling = capability_ceiling()
    ceiling_error = check_capability_ceiling(tool_name, ceiling)
    if ceiling_error:
        _store.log(tool_name=tool_name, args=args, risk_tier="N/A", ceiling=ceiling, decision="rejected_ceiling")
        raise CeilingExceeded(ceiling_error)

    risk_tier = classify_tool_call(tool_name, args)

    if not gating_enabled():
        _store.log(tool_name=tool_name, args=args, risk_tier=risk_tier, ceiling=ceiling, decision="auto_allowed_gating_off")
        return

    if risk_tier in (RISK_SAFE, RISK_CONFIRM):
        _store.log(tool_name=tool_name, args=args, risk_tier=risk_tier, ceiling=ceiling, decision="auto_allowed")
        return

    token = _store.create_pending(tool_name=tool_name, args=args, risk_tier=risk_tier, preview=preview)
    _store.log(tool_name=tool_name, args=args, risk_tier=risk_tier, ceiling=ceiling, decision="pending")
    try:
        from herald.router import event_bus
        event_bus.emit_nowait(
            "approval.requested", importance=0.7,
            payload={"token": token, "tool": tool_name, "risk_tier": risk_tier, "summary": _summarize_args(args, 200)},
            source="approval_gate",
        )
    except Exception:
        pass
    raise ApprovalPending(token, f"'{tool_name}' is {risk_tier} and requires approval (token={token})")


def approve(token: str, executor: Callable[[dict[str, Any]], Any]) -> Any:
    """Approve a pending call and actually run it via `executor(args)`."""
    row = _store.decide(token, "approved")
    if row is None:
        raise KeyError(f"no pending approval for token {token!r} (already decided or unknown)")
    args = json.loads(row["args_json"])
    result = executor(args)
    try:
        from herald.router import event_bus
        event_bus.emit_nowait(
            "approval.decided", importance=0.4,
            payload={"token": token, "tool": row["tool_name"], "decision": "approved"},
            source="approval_gate",
        )
    except Exception:
        pass
    return result


def deny(token: str) -> None:
    row = _store.decide(token, "denied")
    if row is None:
        raise KeyError(f"no pending approval for token {token!r} (already decided or unknown)")
    try:
        from herald.router import event_bus
        event_bus.emit_nowait(
            "approval.decided", importance=0.4,
            payload={"token": token, "tool": row["tool_name"], "decision": "denied"},
            source="approval_gate",
        )
    except Exception:
        pass
