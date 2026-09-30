"""Pure-Python (no LLM call) supervisor check for the Phase 1 Codex loop.

Earlier design used an LLM-driven supervisor schedule that shelled out to
the `herald` CLI multiple times per invocation -- each tool call is a full
model round-trip plus a fresh Python process cold-start, which measured out
to 60-120+ seconds per check even on a "fast" model with a healthy backend.
That's not actually cheap, and defeats the point of a frequent check.

This does the same "is the worker stalled, and if so nudge it" decision
directly in-process: reading the task checklist and the worker schedule's
own run history is a few milliseconds, no network call at all unless a
nudge is actually warranted.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

STALL_MINUTES = 5


def _unchecked_count(task_file: Path) -> int:
    if not task_file.exists():
        return 0
    text = task_file.read_text(encoding="utf-8")
    return len(re.findall(r"^-\s\[\s\]", text, flags=re.MULTILINE))


def check_and_maybe_nudge(
    *, worker_schedule_name: str, task_files: list[str] | str, stall_minutes: int = STALL_MINUTES,
) -> str:
    """Nudge the worker if it's been stalled longer than `stall_minutes`.

    `task_files` is informational only (used for the summary text) -- it
    does NOT gate whether a nudge happens. The worker's own prompt is
    responsible for the "all checklists done -> switch to continuous
    research mode" transition, so from the supervisor's point of view the
    worker always has *something* to do and should keep being nudged
    forever on a stall, not stop once every checklist file empties out.
    """
    from herald.router.scheduler import ScheduleStore, Scheduler, is_running

    files = [task_files] if isinstance(task_files, str) else task_files
    remaining = sum(_unchecked_count(Path(f)) for f in files)
    phase_note = f"{remaining} checklist item(s) remain across tracked phases" if remaining else "all tracked phase checklists complete -- continuous research mode"

    store = ScheduleStore()
    worker = store.get(worker_schedule_name)
    if worker is None:
        return f"{worker_schedule_name}: no such schedule, cannot nudge."

    if is_running(worker.id):
        # last_fired_at only updates when a run COMPLETES, not when it
        # starts -- without this check, a worker genuinely still running
        # (slow but alive) looks identical to one that's dead, and every
        # 2-minute supervisor tick would launch another concurrent `_fire()`
        # on top of it instead of just waiting. This was the actual cause
        # of multiple real CLI processes piling up on one logical task.
        return f"{worker_schedule_name}: currently running, not nudging (would pile up a duplicate). {phase_note}."

    stalled = True
    if worker.last_fired_at:
        last = datetime.fromisoformat(worker.last_fired_at)
        elapsed_minutes = (datetime.now(UTC) - last).total_seconds() / 60
        stalled = elapsed_minutes >= stall_minutes

    if not stalled:
        return f"{worker_schedule_name}: recently active ({worker.last_fired_at}), no nudge needed. {phase_note}."

    Scheduler(store)._fire(worker)
    updated = store.get(worker_schedule_name)
    return (
        f"{worker_schedule_name}: was stalled (last fired {worker.last_fired_at or 'never'}), "
        f"nudged. New status: {updated.last_status}. {phase_note}."
    )
