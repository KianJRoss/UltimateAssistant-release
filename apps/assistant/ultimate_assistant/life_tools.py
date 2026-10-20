"""What the user has to do and when: every agent can see it, add to it and close things.

Kinds and where they live are described in life/record.py. Times go in as ISO 8601 with an
offset; they come back in the user's own time zone.
"""
from __future__ import annotations

import json
from datetime import datetime

from mcp.server.fastmcp import FastMCP

from .life import record as life

mcp = FastMCP("ultimate-assistant-life")


def _local(at: str | None) -> str:
    if not at:
        return ""
    from .conversation_flow import user_timezone
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(user_timezone()).strftime("%a %b %d %I:%M %p")


@mcp.tool()
def life_upcoming(days: float = 7) -> str:
    """What's coming up (and what's overdue) in the next `days`: tasks with due dates, appointments,
    outings with an expected return, chores that are due. Read this before planning the user's time
    or answering "what do I have"."""
    items = life.upcoming(days=days)
    return json.dumps({"items": [{**item, "local_time": _local(item["at"])} for item in items]}, ensure_ascii=False)


@mcp.tool()
def life_remember(kind: str, title: str, starts_at: str = "", ends_at: str = "", due_at: str = "",
                  expected_return: str = "", interval_days: int = 0, estimated_minutes: int = 0,
                  location: str = "", notes: str = "") -> str:
    """Record something the user has to do or be at, as soon as they mention it (no need to ask).
    kind: task (one-off, due_at optional), appointment (starts_at), outing (away now or from
    starts_at, expected_return), chore (repeats every interval_days, due_at = next time), hobby.
    Times: ISO 8601 with offset, e.g. 2026-10-23T15:00:00-04:00."""
    saved = life.record({"kind": kind, "title": title, "starts_at": starts_at or None, "ends_at": ends_at or None,
                         "due_at": due_at or None, "expected_return": expected_return or None,
                         "interval_days": interval_days or None, "estimated_minutes": estimated_minutes or None,
                         "location": location, "notes": notes}, source="assistant")
    return json.dumps({**saved, "local_time": _local(saved.get("when"))}, ensure_ascii=False)


def _window(start: str, end: str) -> tuple[datetime, datetime]:
    a, b = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (start, end))
    if a.tzinfo is None or b.tzinfo is None or a >= b:
        raise ValueError("start and end: ISO 8601 with an offset, end after start")
    return a, b


@mcp.tool()
def life_schedule(start: str, end: str) -> str:
    """What occupies the user's time between start and end (appointments, time blocks, outings),
    with any that overlap each other. To block out time (class, shift, study, travel), record an
    appointment with starts_at and ends_at."""
    entries = life.busy(*_window(start, end))
    conflicts = [{"first": a["id"], "second": b["id"]} for i, a in enumerate(entries) for b in entries[i + 1:]
                 if b["start"] < a["end"]]
    shown = [{**e, "start": _local(e["start"].isoformat()), "end": _local(e["end"].isoformat())} for e in entries]
    return json.dumps({"busy": shown, "conflicts": conflicts}, ensure_ascii=False)


@mcp.tool()
def life_free_time(start: str, end: str, minimum_minutes: int = 30, buffer_minutes: int = 0) -> str:
    """Free stretches of at least minimum_minutes between start and end (give the user's waking or
    available hours), keeping buffer_minutes around commitments. Finds time; doesn't book it."""
    if not 1 <= minimum_minutes <= 1440 or not 0 <= buffer_minutes <= 240:
        raise ValueError("minimum_minutes 1-1440, buffer_minutes 0-240")
    slots = life.free_time(*_window(start, end), minimum_minutes=minimum_minutes, buffer_minutes=buffer_minutes)
    return json.dumps({"free": [{"start": _local(s["start"].isoformat()), "end": _local(s["end"].isoformat()),
                                 "minutes": s["minutes"]} for s in slots]}, ensure_ascii=False)


@mcp.tool()
def life_done(kind: str, item_id: str, cancelled: bool = False) -> str:
    """Close an item from life_upcoming: done (a chore schedules its next time), cancelled, or for an
    outing, that the user is back."""
    result = life.finish(kind, item_id, status="cancelled" if cancelled else "completed")
    return json.dumps({"kind": kind, "id": item_id, "result": result}, default=str, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
