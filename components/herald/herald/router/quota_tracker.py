"""Configured quota and local-capacity summaries for Router backends."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from .telemetry import DB_PATH as TELEMETRY_DB_PATH

def _connect_telemetry() -> sqlite3.Connection | None:
    if not TELEMETRY_DB_PATH.exists():
        return None
    conn = sqlite3.connect(TELEMETRY_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _backend_value(backend: Any, name: str, default: Any = None) -> Any:
    return backend.get(name, default) if isinstance(backend, dict) else getattr(backend, name, default)


def _backend_config(backend: Any) -> dict[str, Any]:
    config = _backend_value(backend, "config", {})
    return config if isinstance(config, dict) else {}


def get_all_quotas(backends: list[Any] | None = None) -> list[dict[str, Any]]:
    """Return limits explicitly configured for the supplied Router backends.

    Provider limits are not inferred from a brand or plan name. Local models
    are reported as unmetered provider capacity, using the user's registered
    backend/model names rather than machine-specific built-in labels.
    """
    quotas: list[dict[str, Any]] = []
    if not backends:
        return quotas

    conn = _connect_telemetry()
    if conn is None:
        return quotas

    with closing(conn):
        for backend in backends:
            if not _backend_value(backend, "enabled", True):
                continue
            name = str(_backend_value(backend, "name", "backend"))
            backend_type = str(_backend_value(backend, "backend_type", ""))
            pool = _backend_value(backend, "pool_name") or name
            config = _backend_config(backend)
            quota = config.get("quota") if isinstance(config.get("quota"), dict) else {}
            is_local = backend_type == "local_model"
            if not is_local and not quota:
                continue

            hours = float(quota["window_hours"]) if quota.get("window_hours") else None
            if hours:
                row = conn.execute(
                    """SELECT COUNT(*) AS calls FROM call_log
                       WHERE backend_name = ?
                         AND timestamp >= datetime('now', ?)""",
                    (name, f"-{hours:g} hours"),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COUNT(*) AS calls FROM call_log
                       WHERE backend_name = ? AND date(timestamp) = date('now')""",
                    (name,),
                ).fetchone()
            used = int(row["calls"] if row else 0)

            if is_local and not quota:
                quotas.append({
                    "provider": name,
                    "pool": pool,
                    "tier": config.get("model") or config.get("model_name") or "Local model",
                    "window": "Unmetered local capacity",
                    "used_count": used,
                    "total_limit": None,
                    "remaining_count": None,
                    "used_percent": None,
                    "remaining_percent": None,
                    "resets": "Not applicable",
                    "unit": "inferences",
                    "status": "available",
                })
                continue

            limit = int(quota.get("limit", 0))
            if limit <= 0:
                continue
            remaining = max(0, limit - used)
            used_percent = min(100, int(round((used / limit) * 100)))
            quotas.append({
                "provider": quota.get("label") or name,
                "pool": pool,
                "tier": quota.get("tier") or "Configured limit",
                "window": quota.get("window") or (f"Rolling {hours:g} hours" if hours else "Daily"),
                "used_count": used,
                "total_limit": limit,
                "remaining_count": remaining,
                "used_percent": used_percent,
                "remaining_percent": 100 - used_percent,
                "resets": quota.get("resets") or ("Rolling" if hours else "Daily"),
                "unit": quota.get("unit") or "requests",
                "status": "ok" if remaining else "exhausted",
            })

    return quotas
