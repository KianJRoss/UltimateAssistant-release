from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .settings import settings
from .task_store import TaskStore


mcp = FastMCP("ultimate-assistant-tasks")


def _store() -> TaskStore:
    path = Path(os.environ.get("ULTIMATE_ASSISTANT_TASKS_DB", str(settings.tasks_db)))
    return TaskStore(path)


@mcp.tool()
def list_assistant_tasks(include_done: bool = False, limit: int = 30) -> str:
    """List the user's locally stored college, work, and personal commitments."""
    tasks = _store().list_tasks(include_done=include_done, limit=max(1, min(limit, 100)))
    return json.dumps({"tasks": tasks}, ensure_ascii=False)


@mcp.tool()
def add_assistant_task(
    title: str,
    domain: str = "personal",
    due_at: str = "",
    details: str = "",
    evidence: str = "",
    kind: str = "commitment",
    estimated_minutes: int = 0,
    related_task_id: str = "",
) -> str:
    """Save an explicitly requested commitment to the local assistant task list."""
    title = title.strip()
    if not title or len(title) > 300:
        return json.dumps({"error": "Task title must be 1–300 characters."})
    if domain not in {"academic", "work", "personal"}:
        return json.dumps({"error": "Domain must be academic, work, or personal."})
    if kind not in {"commitment", "study_recovery"}:
        return json.dumps({"error": "Kind must be commitment or study_recovery."})
    if estimated_minutes < 0 or estimated_minutes > 1440:
        return json.dumps({"error": "Estimated minutes must be between 0 and 1440."})
    if due_at.strip():
        try:
            due_at = datetime.fromisoformat(due_at).isoformat()
        except ValueError:
            return json.dumps({"error": "Due date must be an ISO date or datetime."})
    task = _store().add_task(
        title, details=details[:2000], domain=domain,
        due_at=due_at.strip() or None, kind=kind,
        estimated_minutes=estimated_minutes or None,
        related_task_id=related_task_id.strip() or None,
        source="assistant", evidence=evidence,
    )
    return json.dumps({"task": task}, ensure_ascii=False)


@mcp.tool()
def update_assistant_task(
    task_id: str, status: str = "", due_at: str = "", estimated_minutes: int = 0,
) -> str:
    """Update a task's status, due date, or estimated duration."""
    if status and status not in {"open", "in_progress", "done"}:
        return json.dumps({"error": "Status must be open, in_progress, or done."})
    if estimated_minutes < 0 or estimated_minutes > 1440:
        return json.dumps({"error": "Estimated minutes must be between 0 and 1440."})
    if due_at.strip():
        try:
            due_at = datetime.fromisoformat(due_at).isoformat()
        except ValueError:
            return json.dumps({"error": "Due date must be an ISO date or datetime."})
    task = _store().update_task(
        task_id, status=status or None, due_at=due_at.strip() or None,
        update_due=bool(due_at.strip()), estimated_minutes=estimated_minutes or None,
        update_estimate=estimated_minutes > 0,
    )
    return json.dumps({"task": task} if task else {"error": "Task not found."}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
