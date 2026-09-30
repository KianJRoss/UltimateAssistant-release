"""Light logging + usage tracking, ingested here and displayed once the UI
exists. One SQLite DB, two tables: every call (with token usage and
"thinking" content where a backend actually exposes it), and browser-session
health (since g4f accounts have no token-usage concept at all -- what
matters for them is whether the saved session still works)."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any

from herald.router.sanitization import sanitize_error

DB_PATH = database_path(
    "telemetry.db",
    env_var="HERALD_TELEMETRY_DB",
    legacy_path=Path(__file__).resolve().parent / "telemetry.db",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                backend_name TEXT NOT NULL,
                backend_type TEXT NOT NULL,
                prompt_preview TEXT,
                response_preview TEXT,
                thinking TEXT,
                thinking_tokens INTEGER,
                success INTEGER NOT NULL,
                error TEXT,
                duration_ms INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                branch_id TEXT,
                depth INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_log_backend ON call_log(backend_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_log_timestamp ON call_log(timestamp)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS browser_session_health (
                backend_name TEXT PRIMARY KEY,
                last_success_at TEXT,
                last_failure_at TEXT,
                last_error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                total_calls INTEGER NOT NULL DEFAULT 0
            )
            """
        )


def _preview(text: str | None, limit: int = 300) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


def log_call(
    *,
    backend_name: str,
    backend_type: str,
    prompt: str,
    success: bool,
    duration_ms: int,
    content: str | None = None,
    thinking: str | None = None,
    thinking_tokens: int | None = None,
    error: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    branch_id: str | None = None,
    depth: int | None = None,
) -> None:
    init_schema()
    error = sanitize_error(error) if error else None
    with closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO call_log
                (timestamp, backend_name, backend_type, prompt_preview, response_preview,
                 thinking, thinking_tokens, success, error, duration_ms,
                 input_tokens, output_tokens, cost_usd, branch_id, depth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_iso(), backend_name, backend_type, _preview(prompt), _preview(content),
                thinking, thinking_tokens, int(success), error, duration_ms,
                input_tokens, output_tokens, cost_usd, branch_id, depth,
            ),
        )
        if backend_type == "browser_session":
            _update_browser_session_health(conn, backend_name, success, error)


def _update_browser_session_health(conn: sqlite3.Connection, backend_name: str, success: bool, error: str | None) -> None:
    now = _now_iso()
    row = conn.execute(
        "SELECT consecutive_failures, total_calls FROM browser_session_health WHERE backend_name = ?",
        (backend_name,),
    ).fetchone()
    total_calls = (row["total_calls"] if row else 0) + 1
    if success:
        conn.execute(
            """
            INSERT INTO browser_session_health (backend_name, last_success_at, consecutive_failures, total_calls)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(backend_name) DO UPDATE SET
                last_success_at = excluded.last_success_at,
                consecutive_failures = 0,
                total_calls = excluded.total_calls
            """,
            (backend_name, now, total_calls),
        )
    else:
        consecutive_failures = (row["consecutive_failures"] if row else 0) + 1
        conn.execute(
            """
            INSERT INTO browser_session_health
                (backend_name, last_failure_at, last_error, consecutive_failures, total_calls)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(backend_name) DO UPDATE SET
                last_failure_at = excluded.last_failure_at,
                last_error = excluded.last_error,
                consecutive_failures = excluded.consecutive_failures,
                total_calls = excluded.total_calls
            """,
            (backend_name, now, error, consecutive_failures, total_calls),
        )


def recent_calls(limit: int = 50) -> list[dict[str, Any]]:
    init_schema()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM call_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def recent_burn_rate(backend_name: str, window_hours: int = 6) -> dict[str, Any] | None:
    """Real consumption rate for one backend over the last `window_hours`,
    for usage-budget forecasting (herald/router/usage_budget.py) -- neither
    `recent_calls()` (last N calls, no time window) nor `usage_summary()`
    (lifetime totals, no time window) can answer "how fast is this account
    actually being consumed right now." Returns None if there's no activity
    in the window (nothing to project from), not zero -- a caller
    forecasting risk should treat "no data" and "confirmed zero rate"
    differently."""
    init_schema()
    since = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) as calls,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(cost_usd) as cost_usd
            FROM call_log
            WHERE backend_name = ? AND timestamp > ?
            """,
            (backend_name, since),
        ).fetchone()
    if not row or not row["calls"]:
        return None
    total_tokens = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
    return {
        "window_hours": window_hours,
        "calls": row["calls"],
        "calls_per_hour": row["calls"] / window_hours,
        "tokens_per_hour": total_tokens / window_hours,
        "cost_per_hour": (row["cost_usd"] or 0.0) / window_hours,
    }


def usage_summary() -> dict[str, Any]:
    """Per-backend totals -- calls, tokens, cost, success rate. This is the
    data the UI's Usage page reads from."""
    init_schema()
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT
                backend_name, backend_type,
                COUNT(*) as total_calls,
                SUM(success) as successful_calls,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens,
                SUM(cost_usd) as total_cost_usd,
                AVG(duration_ms) as avg_duration_ms
            FROM call_log
            GROUP BY backend_name, backend_type
            ORDER BY total_calls DESC
            """
        ).fetchall()
        sessions = conn.execute("SELECT * FROM browser_session_health").fetchall()
    return {
        "by_backend": [dict(r) for r in rows],
        "browser_sessions": [dict(r) for r in sessions],
    }
