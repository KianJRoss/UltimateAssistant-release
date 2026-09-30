"""Periodic poller that diffs cli_usage's native-subscription quota output
over time and emits event-bus events on state changes.

cli_usage.all_usage() (backing `herald usage`'s "Native Subscription Quotas"
table -- Claude/Codex/Antigravity) is a pure on-demand computation with no
notion of "changed since last check". This module adds that by polling it
and comparing remaining_percent per (cli, limit_id) against the previous
snapshot.
"""
from __future__ import annotations

import asyncio
import logging

from herald.router import event_bus
from herald.router.cli_usage import all_usage

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 300
# At or below this remaining percent, cross the "getting low" threshold once.
LOW_QUOTA_THRESHOLD_PERCENT = 15

_last_remaining: dict[str, float] = {}
_task: asyncio.Task | None = None


def _limit_key(cli: str, limit: dict) -> str:
    return f"{cli}:{limit.get('limit_id') or limit.get('name')}"


def _check_once() -> None:
    try:
        rows = all_usage(refresh=True)
    except Exception:
        logger.exception("quota_watcher: all_usage failed")
        return

    for row in rows:
        cli = row.get("cli", "unknown")
        for limit in row.get("limits", []):
            remaining = limit.get("remaining_percent")
            if remaining is None:
                continue
            key = _limit_key(cli, limit)
            previous = _last_remaining.get(key)

            if previous is not None:
                # Reset: remaining jumped back up significantly from near-zero.
                if previous <= 2 and remaining >= 50:
                    event_bus.emit_nowait(
                        "quota.reset", importance=0.6,
                        payload={"cli": cli, "limit": limit.get("name"), "remaining_percent": remaining},
                        source="quota_watcher",
                    )
                # Threshold crossed downward into "getting low".
                elif previous > LOW_QUOTA_THRESHOLD_PERCENT >= remaining:
                    event_bus.emit_nowait(
                        "quota.threshold_crossed", importance=0.5,
                        payload={"cli": cli, "limit": limit.get("name"), "remaining_percent": remaining},
                        source="quota_watcher",
                    )

            _last_remaining[key] = remaining


async def _worker() -> None:
    while True:
        _check_once()
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


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
