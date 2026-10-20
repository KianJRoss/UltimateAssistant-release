"""Orchestration adapter over LifeInbox, RoutineStore, TaskUnitStore, and Planning authorities.

Pure orchestration adapter over authorities; does not replace existing stores.
Connects chores, appointments/calendar dependencies, outings/return estimates,
open-time suggestions, and hobby/project next actions without autonomous substantive
project advancement.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from typing import Any

from .life_inbox import LifeInbox
from .routine_store import RoutineStore
from .task_units import TaskUnitStore
from .planning_service import Planner
from .planning_state import PlanningStore
from .progress_state import ProgressStore
from .commitment_engine import build_daily_plan
from .deadline_policy import rank_commitments


from .work_sessions import WorkSessions


class CommitmentOrchestrator:
    """Unified adapter coordinating life commitments, routines, estimates, and planning."""

    def __init__(
        self,
        *,
        life_inbox: LifeInbox | None = None,
        routine_store: RoutineStore | None = None,
        task_unit_store: TaskUnitStore | None = None,
        planning_store: PlanningStore | None = None,
        progress_store: ProgressStore | None = None,
        work_sessions: WorkSessions | None = None,
        planner: Planner | None = None,
        clock=None,
    ):
        self.life_inbox = life_inbox or LifeInbox()
        self.routine_store = routine_store or (
            self.life_inbox._get_routine_store() if hasattr(self.life_inbox, "_get_routine_store") else RoutineStore()
        )
        self.task_unit_store = task_unit_store or (
            self.life_inbox._get_unit_store() if hasattr(self.life_inbox, "_get_unit_store") else TaskUnitStore()
        )
        self.planning_store = planning_store or PlanningStore()
        self.progress_store = progress_store or ProgressStore()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.work_sessions = work_sessions or WorkSessions(self.planning_store.home / 'work-sessions', clock=self.clock)
        self.planner = planner or Planner(
            store=self.planning_store,
            clock=self.clock,
            sessions=self.work_sessions,
            progress=self.progress_store,
            units=self.task_unit_store,
        )

    # 1. Chores and Routine Management
    def record_chore(
        self,
        title: str,
        interval_days: int,
        estimated_minutes: float,
        next_due_at: str,
        *,
        notes: str = "",
        request_id: str,
    ) -> dict:
        return self.life_inbox.capture_chore(
            title=title,
            interval_days=interval_days,
            estimated_minutes=estimated_minutes,
            next_due_at=next_due_at,
            notes=notes,
            request_id=request_id,
        )

    def complete_chore(
        self,
        routine_id: str,
        *,
        revision: int,
        request_id: str,
        completed_at: str | None = None,
    ) -> dict:
        return self.life_inbox.complete_chore(
            routine_id=routine_id,
            revision=revision,
            request_id=request_id,
            completed_at=completed_at,
        )

    # 2. Appointments with Preparation and Travel Dependencies
    def record_appointment(
        self,
        title: str,
        starts_at: str,
        *,
        ends_at: str | None = None,
        location: str = "",
        notes: str = "",
        prep_minutes: float = 0,
        travel_minutes: float = 0,
        request_id: str,
    ) -> dict:
        if isinstance(prep_minutes, bool) or not isinstance(prep_minutes, (int, float)) or prep_minutes < 0:
            raise ValueError("prep_minutes must be a non-negative finite number")
        if isinstance(travel_minutes, bool) or not isinstance(travel_minutes, (int, float)) or travel_minutes < 0:
            raise ValueError("travel_minutes must be a non-negative finite number")

        notes_combined = notes
        metadata = {}
        if prep_minutes > 0:
            metadata["prep_minutes"] = prep_minutes
        if travel_minutes > 0:
            metadata["travel_minutes"] = travel_minutes
        if metadata:
            prefix = f"[Dependencies: prep={prep_minutes}m, travel={travel_minutes}m]"
            notes_combined = f"{prefix} {notes}".strip() if notes else prefix

        return self.life_inbox.capture_appointment(
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            location=location,
            notes=notes_combined,
            request_id=request_id,
        )

    # 3. Outings and Return Estimates
    def record_outing(
        self,
        starts_at: str,
        *,
        destination: str = "",
        expected_return: str | None = None,
        notes: str = "",
        request_id: str,
    ) -> dict:
        return self.life_inbox.capture_away_window(
            destination=destination,
            starts_at=starts_at,
            expected_return=expected_return,
            notes=notes,
            request_id=request_id,
        )

    def record_return(
        self,
        item_id: str,
        *,
        revision: int,
        returned_at: str | None = None,
        request_id: str,
    ) -> dict:
        return self.life_inbox.record_return(
            item_id=item_id,
            revision=revision,
            returned_at=returned_at,
            request_id=request_id,
        )

    # 4. Hobby / Project Next Actions (strictly non-substantive autonomous advancement)
    def record_hobby_action(
        self,
        hobby_name: str,
        next_action: str,
        *,
        estimated_minutes: int | float | None = None,
        context: str | None = None,
        target_date: str | None = None,
        notes: str = "",
        request_id: str,
    ) -> dict:
        """Capture an optional next action for a hobby or personal project.

        Note: Standing authority organizes and prepares next actions, but does
        not autonomously advance or execute substantive project work.
        """
        return self.life_inbox.capture_hobby_action(
            hobby_name=hobby_name,
            next_action=next_action,
            estimated_minutes=estimated_minutes,
            context=context,
            target_date=target_date,
            notes=notes,
            request_id=request_id,
        )

    def execute_project_advancement(self, *args, **kwargs):
        """Explicitly prohibited without user request/delegation."""
        raise PermissionError(
            "Autonomous substantive advancement of personal projects is prohibited "
            "without explicit user delegation."
        )

    # 5. Fitting Open-Time Suggestions
    def suggest_open_time_activities(
        self,
        *,
        now: str | datetime,
        available_minutes: float,
        context: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Suggest fitting activities (chores, hobbies, reviews) for genuinely open time.

        Does not invent obligations or schedule unwanted work.
        """
        if isinstance(available_minutes, bool) or not isinstance(available_minutes, (int, float)) or available_minutes <= 0:
            raise ValueError("available_minutes must be a positive number")

        suggestions = []
        snap = self.life_inbox.snapshot(now=now, limit=100)

        # Chores that are due or upcoming and fit within available minutes
        chores = snap.get("chores", {}).get("items", [])
        for chore in chores:
            est = chore.get("estimated_minutes", 0)
            if ((not context or context == "home") and est <= available_minutes
                    and chore.get("due_status") in ("due", "upcoming")):
                suggestions.append({
                    "kind": "chore",
                    "title": chore.get("title"),
                    "estimated_minutes": est,
                    "id": chore.get("id"),
                    "reason": "Recurring chore due soon",
                    "context": "home",
                })

        # Optional hobby actions are offered only when an explicit estimate exists;
        # an unknown-size project action cannot honestly be said to fit a window.
        items = snap.get("items", [])
        for item in items:
            if item.get("kind") == "hobby_action" and item.get("status") == "active":
                details = item.get("details", {})
                estimate = details.get("estimated_minutes")
                item_context = details.get("context")
                if (
                    isinstance(estimate, bool)
                    or not isinstance(estimate, (int, float))
                    or not math.isfinite(estimate)
                    or estimate <= 0
                    or estimate > available_minutes
                ):
                    continue
                if context and item_context and item_context != context:
                    continue
                suggestions.append({
                    "kind": "hobby_action",
                    "title": item.get("title"),
                    "hobby_name": details.get("hobby_name"),
                    "next_action": details.get("next_action"),
                    "id": item.get("id"),
                    "estimated_minutes": estimate,
                    "context": item_context,
                    "reason": "Prepared next action for personal hobby",
                })

        # Existing task-unit definitions carry bounded effort and durable identity.
        for step in self.get_prepared_next_steps():
            if (step.get("kind") == "task_unit"
                    and step.get("estimated_minutes", available_minutes + 1) <= available_minutes):
                suggestions.append({**step, "reason": "Prepared task unit fits the available time"})

        return suggestions[:limit]

    # 6. Unified Commitment Snapshot & Planning Integration
    def snapshot(self, *, now=None, limit: int = 100) -> dict:
        """Produce a complete unified commitment snapshot."""
        return self.life_inbox.snapshot(now=now, limit=limit)

    def plan_commitments(
        self,
        commitments: list[dict],
        *,
        now: str | datetime,
        work_windows: list[dict],
        outings: list[dict] | None = None,
        learning: dict | None = None,
        timezone_name: str = "America/Indianapolis",
        focus_minutes: int = 25,
        break_minutes: int = 5,
    ) -> dict:
        """Pure advisory plan dispatch using commitment engine."""
        if outings is None:
            snap = self.life_inbox.snapshot_items(kind="away_window", limit=100)
            outings = [
                {
                    "starts_at": item.get("scheduled_start"),
                    "expected_return": item.get("scheduled_end"),
                    "returned_at": item.get("details", {}).get("actual_return"),
                }
                for item in snap.get("items", [])
            ]
        return build_daily_plan(
            commitments,
            now=now,
            work_windows=work_windows,
            outings=outings,
            learning=learning,
            timezone_name=timezone_name,
            focus_minutes=focus_minutes,
            break_minutes=break_minutes,
        )

    # 7. Task-Unit Weighting & Progress
    def define_task_units(
        self,
        activity_id: str,
        units: list[dict],
        *,
        request_id: str,
        expected_revision: int | None = None,
        provenance: str = "",
    ) -> dict:
        """Define granular estimated units for an activity via TaskUnitStore."""
        return self.task_unit_store.define(
            activity_id=activity_id,
            units=units,
            request_id=request_id,
            expected_revision=expected_revision,
            provenance=provenance,
        )

    def record_progress(
        self,
        activity_id: str,
        *,
        note: str,
        completed_unit_ids: list[str] | None = None,
        remaining_minutes: float | None = None,
        request_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict:
        """Record explicit progress evidence via ProgressStore."""
        return self.progress_store.record(
            activity_id=activity_id,
            note=note,
            completed_unit_ids=completed_unit_ids,
            remaining_minutes=remaining_minutes,
            request_id=request_id,
            observed_at=observed_at,
        )

    def get_weighted_progress(self, activity_id: str | None = None) -> dict:
        """Compute weighted progress from immutable units and explicit progress evidence."""
        definitions = self.task_unit_store.snapshot(activity_id=activity_id)
        records = self.progress_store.list_records(activity_id=activity_id)

        completed_by_act: dict[str, set[str]] = {}
        for r in records:
            act = r.get("activity_id")
            for uid in r.get("completed_unit_ids", []):
                completed_by_act.setdefault(act, set()).add(uid)

        activities_progress = {}
        total_estimated = 0.0
        total_completed = 0.0

        for definition in definitions:
            act = definition.get("activity_id")
            units = definition.get("units", [])
            completed_set = completed_by_act.get(act, set())

            act_total = sum(u.get("estimated_minutes", 0.0) for u in units)
            act_completed = sum(
                u.get("estimated_minutes", 0.0)
                for u in units
                if u.get("id") in completed_set
            )
            act_remaining = max(0.0, act_total - act_completed)
            pct = (act_completed / act_total * 100.0) if act_total > 0 else 0.0

            total_estimated += act_total
            total_completed += act_completed

            activities_progress[act] = {
                "activity_id": act,
                "revision": definition.get("revision"),
                "total_estimated_minutes": act_total,
                "completed_minutes": act_completed,
                "remaining_minutes": act_remaining,
                "percent_complete": pct,
                "completed_units_count": len([u for u in units if u.get("id") in completed_set]),
                "total_units_count": len(units),
            }

        overall_pct = (total_completed / total_estimated * 100.0) if total_estimated > 0 else 0.0
        return {
            "activities": activities_progress,
            "overall": {
                "total_estimated_minutes": total_estimated,
                "completed_minutes": total_completed,
                "remaining_minutes": max(0.0, total_estimated - total_completed),
                "percent_complete": overall_pct,
            },
        }

    # 8. Adaptive Breaks & Fatigue Protection
    def calculate_adaptive_break(
        self,
        focus_elapsed_minutes: float,
        *,
        consecutive_blocks: int = 1,
        base_break_minutes: float = 5.0,
    ) -> float:
        """Calculate adaptive break duration scaling with focus duration and consecutive sessions."""
        if isinstance(focus_elapsed_minutes, bool) or not isinstance(focus_elapsed_minutes, (int, float)) or focus_elapsed_minutes <= 0:
            raise ValueError("focus_elapsed_minutes must be a positive number")
        if isinstance(consecutive_blocks, bool) or not isinstance(consecutive_blocks, int) or consecutive_blocks < 1:
            raise ValueError("consecutive_blocks must be an integer >= 1")
        if isinstance(base_break_minutes, bool) or not isinstance(base_break_minutes, (int, float)) or base_break_minutes <= 0:
            raise ValueError("base_break_minutes must be a positive number")

        scaled_break = base_break_minutes * (focus_elapsed_minutes / 25.0)
        if consecutive_blocks > 1:
            multiplier = 1.0 + 0.25 * (consecutive_blocks - 1)
            scaled_break *= multiplier

        return round(min(45.0, max(base_break_minutes, scaled_break)), 1)

    # 9. Deadline Escalation & Evaluation
    def evaluate_deadline_policy(
        self,
        commitments: list[dict],
        *,
        now: str | datetime | None = None,
        timezone_name: str = "America/Indianapolis",
    ) -> dict:
        """Evaluate deadlines, ranking them and determining escalation levels."""
        if now is None:
            now = self.clock()
        ranked = rank_commitments(commitments, now=now, timezone_name=timezone_name)
        escalations = []
        critical_count = 0
        warning_count = 0

        for item in ranked:
            deadline = item.get("deadline", {})
            relation = deadline.get("relation")
            classification = deadline.get("classification")

            if relation == "overdue":
                escalation = "critical"
                critical_count += 1
            elif relation == "current":
                escalation = "warning"
                warning_count += 1
            elif relation == "upcoming" and classification == "date_only":
                escalation = "upcoming"
            else:
                escalation = "normal"

            escalations.append({
                "record": item.get("record"),
                "deadline": deadline,
                "effort": item.get("effort"),
                "escalation": escalation,
            })

        return {
            "ranked": escalations,
            "summary": {
                "total": len(escalations),
                "critical": critical_count,
                "warning": warning_count,
            },
        }

    # 10. Prepared Next Steps
    def get_prepared_next_steps(self, activity_id: str | None = None) -> list[dict]:
        """Determine concrete, prepared next steps for commitments and hobbies."""
        steps = []

        definitions = self.task_unit_store.snapshot(activity_id=activity_id)
        records = self.progress_store.list_records(activity_id=activity_id)
        completed_by_act: dict[str, set[str]] = {}
        for r in records:
            act = r.get("activity_id")
            for uid in r.get("completed_unit_ids", []):
                completed_by_act.setdefault(act, set()).add(uid)

        for definition in definitions:
            act = definition.get("activity_id")
            completed_set = completed_by_act.get(act, set())
            for unit in definition.get("units", []):
                if unit.get("id") not in completed_set:
                    steps.append({
                        "kind": "task_unit",
                        "activity_id": act,
                        "unit_id": unit.get("id"),
                        "label": unit.get("label"),
                        "estimated_minutes": unit.get("estimated_minutes"),
                        "next_action": unit.get("label"),
                    })
                    break

        if activity_id is None:
            snap = self.life_inbox.snapshot_items(kind="hobby_action", limit=50)
            for item in snap.get("items", []):
                if item.get("status") == "active":
                    details = item.get("details", {})
                    steps.append({
                        "kind": "hobby_action",
                        "activity_id": item.get("id"),
                        "hobby_name": details.get("hobby_name"),
                        "next_action": details.get("next_action"),
                    })

        return steps

    # 11. Durable Proactive Coordinator
    def select_permitted_preparation(
        self,
        *,
        commitments: list[dict],
        now: str | datetime | None = None,
        work_windows: list[dict] | None = None,
        outings: list[dict] | None = None,
        learning_evidence: dict | None = None,
        timezone_name: str = "America/Indianapolis",
        focus_minutes: int = 25,
        break_minutes: int = 5,
        decision_id: str | None = None,
        request_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        """Select permitted preparation from deadlines, availability, effort, active sessions, and learning evidence.

        Enforcement remains inactive (enforcement_active=False) while user-disabled modes remain disabled.
        Persists decision and input fingerprint; rejects stale input revision.
        """
        now_dt = now if isinstance(now, datetime) else (datetime.fromisoformat(now.replace("Z", "+00:00")) if now else self.clock())
        if now_dt.utcoffset() is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_iso = now_dt.isoformat()

        # Check for active session
        active_session = self.work_sessions.current()
        if active_session and active_session.get("phase") in ("focus", "break"):
            # Check if active session is past its ends_at
            active_end = self.work_sessions.instant(active_session["ends_at"])
            if now_dt >= active_end:
                active_session = self.work_sessions.current()

        current_revision = active_session.get("revision", 0) if active_session else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise ValueError(
                f"Stale input revision: expected {expected_revision}, got current session revision {current_revision}"
            )

        # Work windows from preferences if not provided
        if work_windows is None:
            prefs = self.planning_store.preferences()
            work_windows = prefs.get("work_windows", []) if prefs else []

        # Evaluate deadlines
        deadline_eval = self.evaluate_deadline_policy(commitments, now=now_dt, timezone_name=timezone_name)

        # Reassess the retained schedule before the engine clips elapsed windows.
        progress_records = self.progress_store.list_records()
        retained = self.planning_store.latest_plan()
        historical_observations = self.planner._reassess_retained_plan(retained, now_dt, progress_records)

        # Plan commitments
        daily_plan = self.plan_commitments(
            commitments,
            now=now_iso,
            work_windows=work_windows,
            outings=outings,
            learning=learning_evidence,
            timezone_name=timezone_name,
            focus_minutes=focus_minutes,
            break_minutes=break_minutes,
        )

        # Recognize missed blocks
        missed = [record['payload'] for record in historical_observations
                  if record['payload'].get('status') == 'missed']

        # Gather next steps (preparation only, no project advancement)
        prepared_steps = self.get_prepared_next_steps()

        # Check prerequisite status for items requiring external connectors
        permitted_items = []
        suppressed_items = []
        for step in prepared_steps:
            act_id = step.get("activity_id", "")
            prereq = self.planning_store.get_prerequisite_status(act_id, now_ts=now_dt.timestamp())
            if not prereq.get("can_attempt", True):
                suppressed_items.append({
                    "step": step,
                    "reason": f"Prerequisite unavailable; backing off until {prereq.get('backoff_until')}",
                    "failure_count": prereq.get("failure_count"),
                })
            else:
                permitted_items.append(step)

        # Fingerprint inputs for immutability & stale revision check
        import hashlib
        fingerprint_payload = {
            "now_minute": now_iso[:16],
            "commitments": commitments,
            "work_windows": work_windows,
            "outings": outings or [],
            "learning_evidence": learning_evidence or {},
            "revision": current_revision,
            "active_session_id": active_session.get("id") if active_session else None,
            "active_session_phase": active_session.get("phase") if active_session else None,
        }
        fp = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True).encode()).hexdigest()

        decision = {
            "now": now_iso,
            "enforcement_active": False,
            "active_session": active_session,
            "deadline_evaluation": deadline_eval,
            "daily_plan": daily_plan,
            "missed_blocks": missed,
            "planning_observations": historical_observations,
            "permitted_preparations": permitted_items,
            "suppressed_preparations": suppressed_items,
            "learning_evidence": learning_evidence or {},
            "status": "ready",
        }

        # Persist decision
        import uuid
        dec_id = decision_id or f"dec-{uuid.uuid4().hex[:12]}"
        req_id = request_id or f"req-{dec_id}"
        stored = self.planning_store.record_decision(
            dec_id,
            decision,
            input_revision=current_revision,
            inputs_fingerprint=fp,
            request_id=req_id,
        )

        return {
            "decision_id": dec_id,
            "decision": decision,
            "stored_record": stored,
            "enforcement_active": False,
        }

    def recognize_missed_blocks(
        self,
        plan: dict,
        *,
        now: str | datetime | None = None,
        progress_evidence: list[dict] | None = None,
        session_evidence: list[dict] | None = None,
    ) -> list[dict]:
        """Recognize focus blocks scheduled in the past that were not observed or started."""
        now_dt = now if isinstance(now, datetime) else (datetime.fromisoformat(now.replace("Z", "+00:00")) if now else self.clock())
        if now_dt.utcoffset() is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        if progress_evidence is None:
            progress_evidence = self.progress_store.list_records()
        if session_evidence is None:
            with self.work_sessions.db() as db:
                session_evidence = [json.loads(row[0]) for row in
                                    db.execute('SELECT payload FROM sessions ORDER BY rowid')]
        missed = []
        blocks = plan.get("blocks", [])
        for block in blocks:
            if block.get("type") == "focus":
                start = self.work_sessions.instant(block["starts_at"])
                end = self.work_sessions.instant(block["ends_at"])
                activity_id = block.get("activity_id")
                observed_session = any(
                    session.get('activity_id') == activity_id and
                    start <= self.work_sessions.instant(session['starts_at']) <= end
                    for session in session_evidence
                )
                observed_progress = False
                for evidence in progress_evidence:
                    stamp = evidence.get('observed_at') or evidence.get('created_at')
                    if evidence.get('activity_id') == activity_id and stamp and self.work_sessions.instant(stamp) >= start:
                        observed_progress = True
                        break
                if end < now_dt and not (observed_session or observed_progress):
                    missed.append({
                        "block": block,
                        "scheduled_start": block["starts_at"],
                        "scheduled_end": block["ends_at"],
                        "minutes": block.get("minutes", 0),
                        "status": "missed",
                        "activity_id": activity_id,
                    })
        return missed

    def checkpoint_owned_work(
        self,
        job_or_session_id: str,
        stage: str,
        checkpoint_data: dict,
        *,
        checkpoint_id: str | None = None,
        session_revision: int | None = None,
    ) -> dict:
        """Durable checkpoint of owned work."""
        import uuid
        cid = checkpoint_id or f"chk-{uuid.uuid4().hex[:12]}"
        # If active session matches, checkpoint in session store too
        active = self.work_sessions.current()
        if active and active.get("id") == job_or_session_id:
            rev = session_revision if session_revision is not None else active.get("revision")
            self.work_sessions.checkpoint(job_or_session_id, rev, stage, checkpoint_data)

        return self.planning_store.record_checkpoint(
            cid, job_or_session_id, stage, checkpoint_data
        )

    def cancel_owned_work(
        self,
        job_or_session_id: str,
        *,
        session_revision: int | None = None,
        reason: str = "cancelled",
        progress: dict | None = None,
    ) -> dict:
        """Cancel owned active work without advancing project work."""
        active = self.work_sessions.current()
        if active and active.get("id") == job_or_session_id:
            rev = session_revision if session_revision is not None else active.get("revision")
            res = self.work_sessions.cancel(job_or_session_id, rev, reason=reason, progress=progress)
            return {"cancelled": True, "type": "work_session", "session": res}
        return {"cancelled": False, "type": "unknown", "message": f"No active work session {job_or_session_id}"}

    def record_prerequisite_failure(
        self,
        name: str,
        error: str,
        *,
        backoff_seconds: float = 60.0,
        now_ts: float | None = None,
    ) -> dict:
        """Record prerequisite failure with exponential backoff to avoid repeated retrying."""
        return self.planning_store.record_prerequisite_failure(
            name, error, backoff_seconds=backoff_seconds, now_ts=now_ts
        )

    def record_prerequisite_success(self, name: str) -> dict:
        """Reset prerequisite state on success."""
        return self.planning_store.record_prerequisite_success(name)

    def get_prerequisite_status(self, name: str, now_ts: float | None = None) -> dict:
        """Query prerequisite availability and backoff status."""
        return self.planning_store.get_prerequisite_status(name, now_ts=now_ts)

    def recover_coordinator_state(self) -> dict:
        """Recover coordinator decisions, active sessions, and progress after restart."""
        # 1. Recover un-persisted session progress via planner
        self.planner._recover_progress()

        # 2. Inspect active session
        active_session = self.work_sessions.current()

        # 3. Read latest decision
        decisions = self.planning_store.list_decisions(limit=1)
        latest_decision = decisions[0] if decisions else None

        # 4. Checkpoints
        checkpoints = []
        if active_session:
            checkpoints = self.planning_store.get_checkpoints(active_session["id"])

        return {
            "recovered": True,
            "active_session": active_session,
            "latest_decision": latest_decision,
            "active_checkpoints": checkpoints,
            "enforcement_active": False,
        }
