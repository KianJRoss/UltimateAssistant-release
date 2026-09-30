"""Idle-model auto-unload daemon -- roadmap #6.

`governor.LocalModelGovernor.idle_backends()` already tracks idle time per
backend and documents an "auto-unload daemon (future)" in its own comments.
This module is that daemon: periodically checks idle_backends(), resolves
each idle backend to its runtime/node/model via the registry, and unloads it
through node_control.py's lmstudio_unload/ollama_unload.

Per-model idle threshold is configurable via router.yaml's `local_models`
block (registered into IdleConfigStore over HTTP, same pattern as
custom_policies.py); a threshold of 0/null disables auto-unload for that
model. Falls back to DEFAULT_IDLE_MINUTES when unset.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = database_path(
    "idle_unload_config.db",
    env_var="HERALD_IDLE_UNLOAD_DB",
    legacy_path=Path(__file__).resolve().parent / "idle_unload_config.db",
)
POLL_INTERVAL_SECONDS = 300
DEFAULT_IDLE_MINUTES = 30


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class IdleConfigStore:
    """Per-model idle_unload_minutes overrides. NULL/0 = disabled for that model."""

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
                CREATE TABLE IF NOT EXISTS idle_unload_config (
                    model_name TEXT PRIMARY KEY,
                    idle_unload_minutes REAL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def upsert(self, model_name: str, idle_unload_minutes: float | None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO idle_unload_config (model_name, idle_unload_minutes, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(model_name) DO UPDATE SET idle_unload_minutes = excluded.idle_unload_minutes, "
                "updated_at = excluded.updated_at",
                (model_name, idle_unload_minutes, _now_iso()),
            )

    def get_threshold_minutes(self, model_name: str) -> float | None:
        """Returns the configured threshold in minutes, or DEFAULT_IDLE_MINUTES
        if unconfigured, or None if explicitly disabled (0/null) for this model."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT idle_unload_minutes FROM idle_unload_config WHERE model_name = ?", (model_name,)
            ).fetchone()
        if row is None:
            return DEFAULT_IDLE_MINUTES
        value = row["idle_unload_minutes"]
        if value is None or value <= 0:
            return None
        return value

    def all_config(self) -> dict[str, float | None]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT model_name, idle_unload_minutes FROM idle_unload_config").fetchall()
        return {r["model_name"]: r["idle_unload_minutes"] for r in rows}


_store = IdleConfigStore()


def get_store() -> IdleConfigStore:
    return _store


def _resolve_backend_runtime(backend_name: str) -> dict[str, Any] | None:
    """Look up an idle backend's runtime/node/model from the registry, so we
    know which node_control unload function to call."""
    from herald.router.registry import Registry
    registry = Registry()
    backend = registry.get(backend_name)
    if backend is None or backend.backend_type != "local_model":
        return None
    config = backend.config
    runtime = config.get("runtime")
    if runtime not in ("lmstudio", "ollama"):
        return None
    return {"runtime": runtime, "node": config.get("node", "local"), "model": config.get("model", backend_name)}


def check_and_unload(idle_backend_names: list[str], *, threshold_check_sec: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Given a list of backend names governor.idle_backends() reports as idle
    (at governor's own coarse threshold), decide per-model whether enough
    idle time has actually passed per IdleConfigStore's configured minutes,
    and unload the ones that qualify. Returns a list of unload results.

    `threshold_check_sec` is an injection point for testing: normally this
    function re-derives per-model idle seconds from governor state, but a
    test can pass a constructed {backend_name: idle_seconds} map directly.
    """
    from herald import governor as governor_module

    unloaded: list[dict[str, Any]] = []
    for name in idle_backend_names:
        info = _resolve_backend_runtime(name)
        if info is None:
            continue

        threshold_minutes = _store.get_threshold_minutes(info["model"])
        if threshold_minutes is None:
            continue  # disabled for this model

        if threshold_check_sec is not None:
            idle_sec = threshold_check_sec.get(name, 0.0)
        else:
            # governor.idle_backends() already filtered at its own coarse
            # threshold; re-derive the actual idle seconds for this specific
            # backend from its internal timestamp so we can compare against
            # the model's own (possibly stricter) configured threshold.
            with governor_module.governor._lock:
                ts = governor_module.governor._idle_timestamps.get(name)
            if ts is None:
                continue
            import time
            idle_sec = time.time() - ts

        if idle_sec < threshold_minutes * 60:
            continue

        result = _unload_backend(info)
        result["backend"] = name
        unloaded.append(result)

    return unloaded


def _unload_backend(info: dict[str, Any]) -> dict[str, Any]:
    from herald.router import node_control
    from herald.router import event_bus

    if info["runtime"] == "lmstudio":
        result = node_control.lmstudio_unload(info["model"], node=info["node"])
    else:
        result = node_control.ollama_unload(info["node"], info["model"])

    ok = bool(result.get("ok", False))
    event_bus.emit_nowait(
        "model.auto_unloaded", importance=0.35,
        payload={"model": info["model"], "runtime": info["runtime"], "node": info["node"], "ok": ok},
        source="idle_unload",
    )
    if not ok:
        logger.warning("idle_unload: failed to unload %s on %s: %s", info["model"], info["runtime"], result.get("error"))
    return {"ok": ok, "model": info["model"], "runtime": info["runtime"]}


def _check_once() -> None:
    from herald import governor as governor_module
    try:
        idle = governor_module.governor.idle_backends()
        if idle:
            check_and_unload(idle)
    except Exception:
        logger.exception("idle_unload: check failed")


async def _worker() -> None:
    while True:
        _check_once()
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


_task: asyncio.Task | None = None


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.ensure_future(_worker())


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
