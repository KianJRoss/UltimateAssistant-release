"""Notification delivery sink for Herald's event bus.

First delivery channel: a Discord webhook, configured via
`HERALD_NOTIFY_WEBHOOK_URL`. Formats an `Event` into a short human-readable
message and POSTs it. If the env var isn't set, logs one warning at import
time and every call becomes a no-op -- never crashes the event bus worker.
"""
from __future__ import annotations

import logging
import os

import httpx

from herald.router.event_bus import Event

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("HERALD_NOTIFY_WEBHOOK_URL", "").strip()

if not WEBHOOK_URL:
    logger.warning(
        "notifications: HERALD_NOTIFY_WEBHOOK_URL not set -- notification "
        "delivery is disabled (events still land in the log sink)."
    )

_EVENT_LABELS = {
    "backend.circuit_opened": "Backend degraded",
    "backend.circuit_closed": "Backend recovered",
    "backend.offline": "Backend offline",
    "backend.recovered": "Backend recovered",
    "quota.threshold_crossed": "Quota getting low",
    "quota.reset": "Quota reset",
    "node.offline": "Node offline",
    "node.online": "Node online",
    "sync.conflict": "State sync conflict",
    "sync.failed": "State sync failed",
    "schedule.fired": "Scheduled run started",
    "schedule.failed": "Scheduled run failed",
    "approval.requested": "Approval requested",
    "approval.decided": "Approval decided",
    "capability.gap_detected": "Capability gap detected",
    "capability.proposal_ready": "Capability proposal ready for review",
    "model.auto_unloaded": "Local model auto-unloaded",
}


def format_message(event: Event) -> str:
    """Turn an Event into a short, human-readable notification line."""
    label = _EVENT_LABELS.get(event.event_type, event.event_type)
    payload = event.payload or {}

    if event.event_type == "quota.reset":
        cli = payload.get("cli", payload.get("profile", "unknown"))
        remaining = payload.get("remaining_percent")
        suffix = f" ({remaining}% remaining)" if remaining is not None else ""
        return f"{label}: {cli} is back{suffix}"

    if event.event_type == "quota.threshold_crossed":
        cli = payload.get("cli", payload.get("profile", "unknown"))
        remaining = payload.get("remaining_percent")
        suffix = f" ({remaining}% remaining)" if remaining is not None else ""
        return f"{label}: {cli}{suffix}"

    if event.event_type in ("backend.circuit_closed", "backend.recovered"):
        backend = payload.get("backend", "unknown")
        return f"{label}: {backend}"

    if event.event_type in ("backend.circuit_opened", "backend.offline"):
        backend = payload.get("backend", "unknown")
        return f"{label}: {backend}"

    if event.event_type in ("node.offline", "node.online"):
        node = payload.get("node", "unknown")
        return f"{label}: {node}"

    if event.event_type in ("sync.conflict", "sync.failed"):
        detail = payload.get("detail") or payload.get("reason") or ""
        return f"{label}{': ' + detail if detail else ''}"

    if event.event_type in ("schedule.fired", "schedule.failed"):
        name = payload.get("name") or payload.get("schedule_id") or "unnamed"
        detail = payload.get("error", "")
        suffix = f" -- {detail}" if detail else ""
        return f"{label}: {name}{suffix}"

    if event.event_type in ("approval.requested", "approval.decided"):
        summary = payload.get("summary") or payload.get("action") or ""
        return f"{label}{': ' + summary if summary else ''}"

    if event.event_type == "capability.gap_detected":
        gap_type = payload.get("gap_type", "unknown")
        details = payload.get("details") or {}
        tool = details.get("tool_name", "")
        return f"{label}: {gap_type}{' (' + tool + ')' if tool else ''}"

    if event.event_type == "capability.proposal_ready":
        pid = payload.get("proposal_id", "?")
        risk = payload.get("risk_level", "?")
        sandbox = "passed" if payload.get("sandbox_ok") else "failed"
        return f"{label}: #{pid} risk={risk} sandbox={sandbox}"

    if event.event_type == "model.auto_unloaded":
        model = payload.get("model", "unknown")
        runtime = payload.get("runtime", "")
        ok = payload.get("ok", True)
        status = "" if ok else " (unload call failed)"
        return f"{label}: {model} ({runtime}){status}"

    # Fallback for any event type without a specific formatter.
    return f"{label} ({event.source}): {payload}"


async def discord_sink(event: Event) -> None:
    """Event-bus sink: format and deliver an event to the configured
    Discord webhook. No-ops silently if no webhook URL is configured."""
    if not WEBHOOK_URL:
        return

    message = format_message(event)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(WEBHOOK_URL, json={"content": message})
            response.raise_for_status()
    except Exception:
        logger.exception("notifications: failed to deliver event %s to webhook", event.event_type)
