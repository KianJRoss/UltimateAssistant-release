"""One way in and one view out of the user's commitments, for the tools, capture and the clock.

What goes where (one home per kind):
- task: a one-off thing to do, optionally due (homework, rent, laundry tonight) -> the assistant's
  task store (tasks.sqlite3, also used by the task tools);
- appointment: something at a time (dentist Thursday 3pm) -> life_inbox;
- outing: the user is away, maybe with an expected return -> life_inbox away window;
- chore: repeats every N days -> routine_store via life_inbox;
- hobby: an optional next step for a hobby -> life_inbox.

Times come in as ISO 8601 with an offset (or "Z"); they're stored in UTC.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

KINDS = ("task", "appointment", "outing", "chore", "hobby")
DEPENDENCIES = re.compile(r"\[Dependencies: prep=([0-9.]+)m, travel=([0-9.]+)m\]")


def dependency_minutes(notes: str | None) -> tuple[float, float]:
    """(prep, travel) minutes from the tag commitment_orchestration puts on an appointment's notes."""
    match = DEPENDENCIES.search(notes or "")
    return (float(match.group(1)), float(match.group(2))) if match else (0.0, 0.0)


def _utc(value: str | None, field: str) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} needs a time zone offset (e.g. 2026-10-23T15:00:00-04:00)")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def request_id(*parts: Any) -> str:
    """The same item from the same message is recorded once, however often it is seen."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def tasks_db():
    """The one tasks database: the task tools, this recorder, the planner and the today view all use it."""
    import os
    from pathlib import Path
    from ..settings import settings
    return Path(os.environ.get("ULTIMATE_ASSISTANT_TASKS_DB", str(settings.tasks_db)))


def _stores():
    from ..task_store import TaskStore
    from .life_inbox import LifeInbox
    return TaskStore(tasks_db()), LifeInbox()


def record(item: dict[str, Any], *, source: str = "conversation", evidence: str = "") -> dict[str, Any]:
    """Store one commitment; returns {"kind", "id", "title", "when"}."""
    kind = item.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    title = str(item.get("title") or "").strip()[:200]
    if not title:
        raise ValueError("a commitment needs a title")
    notes = str(item.get("notes") or "")[:2000]
    key = request_id(kind, title.casefold(), _utc(item.get("starts_at"), "starts_at"), _utc(item.get("due_at"), "due_at"))
    tasks, inbox = _stores()
    if kind == "task":
        due = _utc(item.get("due_at"), "due_at")
        saved = tasks.add_task(title, details=notes, due_at=due, source=source, evidence=evidence[:500],
                               estimated_minutes=item.get("estimated_minutes"), deduplicate=True)
        return {"kind": kind, "id": saved["id"], "title": title, "when": due}
    if kind == "appointment":
        starts = _utc(item.get("starts_at"), "starts_at")
        if not starts:
            raise ValueError("an appointment needs starts_at")
        saved = inbox.capture_appointment(title, starts, ends_at=_utc(item.get("ends_at"), "ends_at"),
                                          location=str(item.get("location") or ""), notes=notes, request_id=key)
        return {"kind": kind, "id": saved["id"], "title": title, "when": starts}
    if kind == "outing":
        starts = _utc(item.get("starts_at"), "starts_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        saved = inbox.capture_away_window(destination=str(item.get("location") or title), starts_at=starts,
                                          expected_return=_utc(item.get("expected_return"), "expected_return"),
                                          notes=notes, request_id=key)
        return {"kind": kind, "id": saved["id"], "title": title, "when": saved.get("scheduled_end") or starts}
    if kind == "chore":
        interval = int(item.get("interval_days") or 7)
        due = _utc(item.get("due_at"), "due_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        saved = inbox.capture_chore(title, interval, int(item.get("estimated_minutes") or 15), due,
                                    notes=notes, request_id=key)
        return {"kind": kind, "id": saved["id"], "title": title, "when": due}
    saved = inbox.capture_hobby_action(title, str(item.get("next_action") or notes or title),
                                       target_date=item.get("due_at"), notes=notes, request_id=key)
    return {"kind": kind, "id": saved["id"], "title": title, "when": item.get("due_at")}


def upcoming(*, days: float = 7, now: datetime | None = None, include_overdue: bool = True) -> list[dict[str, Any]]:
    """Everything open that is due or happens before now + days (and what is overdue), soonest first.
    Each: {"kind", "id", "title", "at", "status", "revision"?, "location"?, "lead_minutes"?} where
    lead_minutes is an appointment's prep plus travel time."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    tasks, inbox = _stores()
    out = []

    def keep(at: str | None) -> bool:
        if not at:
            return False
        moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
        return moment <= horizon and (include_overdue or moment >= now)

    for task in tasks.list_tasks(limit=200):
        if task.get("status") in {"open", "in_progress"} and keep(task.get("due_at")):
            out.append({"kind": "task", "id": task["id"], "title": task["title"], "at": task["due_at"],
                        "status": task["status"]})
    snapshot = inbox.snapshot_items(now=now, limit=1000)
    for item in snapshot.get("items", []):
        if item.get("status") not in {"active", "open", "scheduled"}:
            continue
        kind = {"appointment": "appointment", "away_window": "outing", "hobby_action": "hobby"}.get(item["kind"])
        if not kind:
            continue
        at = item.get("scheduled_end") if kind == "outing" else item.get("scheduled_start")
        if kind == "outing" and item.get("return_status") == "returned":
            continue
        if keep(at):
            entry = {"kind": kind, "id": item["id"], "title": item["title"], "at": at, "status": item["status"],
                     "revision": item.get("revision"), "location": item.get("location") or ""}
            if kind == "appointment" and sum(dependency_minutes(item.get("notes"))):
                entry["lead_minutes"] = sum(dependency_minutes(item.get("notes")))
            out.append(entry)
    from .routine_store import RoutineStore
    for chore in RoutineStore().snapshot(now=now, limit=500).get("items", []):
        if chore.get("active") and keep(chore.get("next_due_at")):
            out.append({"kind": "chore", "id": chore["id"], "title": chore["title"], "at": chore["next_due_at"],
                        "status": "due", "revision": chore.get("revision")})
    return sorted(out, key=lambda entry: entry["at"])


def outings(*, include_returned: bool = False) -> list[dict[str, Any]]:
    """The user's outings (the life inbox's away windows): where, when they left, when they expected to
    be back and when they actually came back. Not yet returned only, unless include_returned."""
    _, inbox = _stores()
    out = []
    for item in inbox.snapshot_items(kind="away_window", limit=1000).get("items", []):
        returned = (item.get("details") or {}).get("actual_return")
        if item.get("return_status") == "returned" and not include_returned:
            continue
        out.append({"id": item["id"], "title": item["title"], "destination": item.get("location") or "",
                    "starts_at": item.get("scheduled_start"), "expected_return": item.get("scheduled_end"),
                    "returned_at": returned, "return_status": item.get("return_status"),
                    "notes": (item.get("details") or {}).get("notes", "")})
    return out


def busy(start: datetime, end: datetime, *, default_minutes: int = 60) -> list[dict[str, Any]]:
    """What occupies the user's time between start and end: appointments (start to end, or
    default_minutes without an end) and outings (leaving to expected return). Each:
    {"kind", "id", "title", "start", "end"} in UTC, soonest first."""
    _, inbox = _stores()
    out = []
    for item in inbox.snapshot_items(now=start, limit=1000).get("items", []):
        kind = {"appointment": "appointment", "away_window": "outing"}.get(item["kind"])
        if not kind or item.get("status") not in {"active", "open", "scheduled"} or not item.get("scheduled_start"):
            continue
        if kind == "outing" and item.get("return_status") == "returned":
            continue
        a = datetime.fromisoformat(item["scheduled_start"].replace("Z", "+00:00"))
        b = (datetime.fromisoformat(item["scheduled_end"].replace("Z", "+00:00")) if item.get("scheduled_end")
             else a + timedelta(minutes=default_minutes))
        if a < end and b > start:
            out.append({"kind": kind, "id": item["id"], "title": item["title"], "start": a, "end": b})
    return sorted(out, key=lambda entry: entry["start"])


def free_time(start: datetime, end: datetime, *, minimum_minutes: int = 30, buffer_minutes: int = 0) -> list[dict[str, Any]]:
    """Gaps of at least minimum_minutes between start and end, keeping buffer_minutes around
    everything busy(). Each: {"start", "end", "minutes"}."""
    pad = timedelta(minutes=buffer_minutes)
    cursor, slots = start, []
    for entry in busy(start - pad, end + pad) + [{"start": end + pad, "end": end + pad}]:
        gap_end = min(end, entry["start"] - pad)
        if (gap_end - cursor).total_seconds() >= minimum_minutes * 60:
            slots.append({"start": cursor, "end": gap_end, "minutes": int((gap_end - cursor).total_seconds() // 60)})
        cursor = max(cursor, entry["end"] + pad)
    return slots


def finish(kind: str, item_id: str, *, status: str = "completed") -> dict[str, Any]:
    """Mark an item done (or cancelled); outings record the return."""
    tasks, inbox = _stores()
    key = request_id("finish", kind, item_id, status, datetime.now(timezone.utc).date())
    if kind == "task":
        return tasks.update_task(item_id, status="done" if status == "completed" else status)
    if kind == "chore":
        from .routine_store import RoutineStore
        store = RoutineStore()
        chore = next(c for c in store.snapshot(limit=1000)["items"] if c["id"] == item_id)
        return store.complete(item_id, chore["revision"], request_id=key)
    current = next(i for i in inbox.snapshot_items(limit=1000)["items"] if i["id"] == item_id)
    if kind == "outing":
        return inbox.record_return(item_id, revision=current["revision"], request_id=key)
    return inbox.update_item_status(item_id, status=status, revision=current["revision"], request_id=key)
