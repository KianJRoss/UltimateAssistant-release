"""Programmable cron for Herald -- roadmap #3.

Two trigger types:
  - time-based: a standard cron expression, checked once a minute
  - event-based: fires when a matching event_bus event lands, optionally
    filtered on payload fields

Each trigger fires either a `Project.part().agentic(prompt)` call or a named
`FlowSpec` (stored inline as JSON on the schedule row -- there's no separate
flow registry in the codebase yet, so the spec travels with the schedule).

Deliberately NOT built in this pass (flagged, not forgotten):
  - worktree isolation per run (roadmap note -- relevant once concurrent
    scheduled runs are common; skip until then)
  - router.yaml declarative schedule loading (CLI/API registration only, for
    now)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any

from croniter import croniter

from herald.router import event_bus

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = database_path(
    "schedules.db",
    env_var="HERALD_SCHEDULES_DB",
    legacy_path=Path(__file__).resolve().parent / "schedules.db",
)
POLL_INTERVAL_SECONDS = 60

# These rows remain useful as named worker-role definitions and may still be
# invoked manually, but their own cron expressions must not compete with the
# Admin coordinator.  The Admin chooses their missions and launches isolated
# swarm workers; independent cron firing was producing unassigned agents that
# replied "what should I do?" and could edit the shared primary worktree.
ADMIN_MANAGED_SCHEDULES: frozenset[str] = frozenset({
    "phase1-supervisor", "phase1-codex-loop",
    "sdk-research-loop", "improvement-research-loop",
    "herald-dev-core", "herald-dev-packaging", "herald-dev-loop",
    "herald-review-loop", "herald-docgen-loop", "herald-cleanup-loop",
})

# action_type='python' targets must be explicitly allowlisted -- schedules
# are creatable over HTTP with no auth (personal tailnet convention), so an
# arbitrary importable dotted-path would be an arbitrary-code-execution
# primitive. "module.func" -> the actual callable, imported lazily.
PYTHON_ACTION_ALLOWLIST: frozenset[str] = frozenset({
    "herald.router.codex_supervisor.check_and_maybe_nudge",
    *({"herald.router.admin_loop.run_admin_cycle"} if os.environ.get("HERALD_DEV_MODE") == "1" else set()),
})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Schedule:
    id: int
    name: str
    trigger_type: str  # "cron" | "event"
    cron_expression: str | None
    event_type: str | None
    event_filter: dict[str, Any] | None
    action_type: str  # "agentic" | "flow"
    project: str | None
    part: str | None
    prompt: str | None
    flow_spec_json: str | None
    enabled: bool
    created_at: str
    last_fired_at: str | None
    last_status: str | None
    model: str | None = None
    agentic: bool = True
    python_target: str | None = None
    python_kwargs: dict[str, Any] | None = None


class ScheduleStore:
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
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('cron', 'event')),
                    cron_expression TEXT,
                    event_type TEXT,
                    event_filter_json TEXT,
                    action_type TEXT NOT NULL CHECK(action_type IN ('agentic', 'flow', 'python')),
                    project TEXT,
                    part TEXT,
                    prompt TEXT,
                    flow_spec_json TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_fired_at TEXT,
                    last_status TEXT
                )
                """
            )
            # `model` pins the agentic action to a specific Herald backend
            # name (e.g. "codex-backup"), bypassing normal routing-policy
            # selection -- needed for supervised worker loops where the
            # schedule must always drive the same backend, not whatever
            # the router would pick automatically. Added via ALTER since
            # CREATE TABLE IF NOT EXISTS won't retrofit existing databases.
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
            if "model" not in existing_cols:
                conn.execute("ALTER TABLE schedules ADD COLUMN model TEXT")
            if "agentic" not in existing_cols:
                # Default 1 (True) to preserve existing behavior for schedules
                # created before this column existed. Kept for completeness,
                # but NOTE: Part.chat() (agentic=False) does NOT get real tool
                # execution here -- confirmed empirically, it just returns a
                # conversational reply without running any tools, even when
                # instructed to. Only Part.agentic() (True) actually executes
                # tool calls server-side. For a genuinely cheap, no-LLM-call
                # check, use action_type='python' instead (see below) --
                # that's what this column's original "cheap check" use case
                # actually needed, not a False agentic flag.
                conn.execute("ALTER TABLE schedules ADD COLUMN agentic INTEGER NOT NULL DEFAULT 1")
            if "python_target" not in existing_cols:
                # Dotted path to a Python callable (module.func), invoked
                # in-process with the schedule's kwargs from python_kwargs_json
                # for action_type='python' -- a genuinely cheap, LLM-free
                # check (milliseconds, no network call) that only escalates
                # to an expensive agentic/flow schedule when it decides a
                # nudge is actually warranted. Added for the same reason
                # `model`/`agentic` were: retrofit an existing live table.
                conn.execute("ALTER TABLE schedules ADD COLUMN python_target TEXT")
                conn.execute("ALTER TABLE schedules ADD COLUMN python_kwargs_json TEXT")
            # SQLite CHECK constraints can't be altered in place -- if this
            # table was created before 'python' was a valid action_type, the
            # CHECK constraint from the original CREATE TABLE still rejects
            # it even after the above ALTERs. Rebuild the table if so.
            create_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedules'"
            ).fetchone()["sql"]
            if "'python'" not in create_sql:
                conn.execute("ALTER TABLE schedules RENAME TO schedules_old")
                conn.execute(
                    """
                    CREATE TABLE schedules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        trigger_type TEXT NOT NULL CHECK(trigger_type IN ('cron', 'event')),
                        cron_expression TEXT,
                        event_type TEXT,
                        event_filter_json TEXT,
                        action_type TEXT NOT NULL CHECK(action_type IN ('agentic', 'flow', 'python')),
                        project TEXT,
                        part TEXT,
                        prompt TEXT,
                        flow_spec_json TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        last_fired_at TEXT,
                        last_status TEXT,
                        model TEXT,
                        agentic INTEGER NOT NULL DEFAULT 1,
                        python_target TEXT,
                        python_kwargs_json TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO schedules SELECT id, name, trigger_type, cron_expression, event_type, "
                    "event_filter_json, action_type, project, part, prompt, flow_spec_json, enabled, "
                    "created_at, last_fired_at, last_status, model, agentic, python_target, python_kwargs_json "
                    "FROM schedules_old"
                )
                conn.execute("DROP TABLE schedules_old")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER NOT NULL,
                    schedule_name TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT,
                    trigger_context TEXT
                )
                """
            )
            run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(schedule_runs)")}
            if "trace_json" not in run_cols:
                # Full orchestration_trace (per-branch/model/tool-call steps
                # from the agentic recursive/parallel delegation graph) for
                # agentic runs -- previously only the final text summary was
                # kept, discarding what backends/tools actually ran.
                conn.execute("ALTER TABLE schedule_runs ADD COLUMN trace_json TEXT")

    def add(
        self,
        name: str,
        *,
        trigger_type: str,
        action_type: str,
        cron_expression: str | None = None,
        event_type: str | None = None,
        event_filter: dict[str, Any] | None = None,
        project: str | None = None,
        part: str | None = None,
        prompt: str | None = None,
        flow_spec_json: str | None = None,
        enabled: bool = True,
        model: str | None = None,
        agentic: bool = True,
        python_target: str | None = None,
        python_kwargs: dict[str, Any] | None = None,
    ) -> int:
        if trigger_type not in ("cron", "event"):
            raise ValueError(f"trigger_type must be 'cron' or 'event', got {trigger_type!r}")
        if trigger_type == "cron" and not cron_expression:
            raise ValueError("cron_expression required for trigger_type='cron'")
        if trigger_type == "cron":
            croniter(cron_expression)  # raises if invalid
        if trigger_type == "event" and not event_type:
            raise ValueError("event_type required for trigger_type='event'")
        if trigger_type == "event" and event_type not in event_bus.EVENT_TYPES:
            raise ValueError(f"unknown event_type: {event_type!r}")
        if action_type not in ("agentic", "flow", "python"):
            raise ValueError(f"action_type must be 'agentic', 'flow', or 'python', got {action_type!r}")
        if action_type == "agentic" and not (project and part and prompt):
            raise ValueError("agentic action requires project, part, and prompt")
        if action_type == "flow" and not flow_spec_json:
            raise ValueError("flow action requires flow_spec_json")
        if action_type == "python":
            if not python_target:
                raise ValueError("python action requires python_target")
            if python_target not in PYTHON_ACTION_ALLOWLIST:
                raise ValueError(
                    f"python_target {python_target!r} is not in the allowlist "
                    f"({sorted(PYTHON_ACTION_ALLOWLIST)}) -- arbitrary imports aren't "
                    "permitted, add the target to PYTHON_ACTION_ALLOWLIST in scheduler.py first"
                )

        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO schedules
                    (name, trigger_type, cron_expression, event_type, event_filter_json,
                     action_type, project, part, prompt, flow_spec_json, enabled, created_at, model, agentic,
                     python_target, python_kwargs_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name, trigger_type, cron_expression, event_type,
                    json.dumps(event_filter) if event_filter else None,
                    action_type, project, part, prompt, flow_spec_json,
                    int(enabled), _now_iso(), model, int(agentic),
                    python_target, json.dumps(python_kwargs) if python_kwargs else None,
                ),
            )
            return int(cur.lastrowid)

    def _row_to_schedule(self, row: sqlite3.Row) -> Schedule:
        return Schedule(
            id=row["id"], name=row["name"], trigger_type=row["trigger_type"],
            cron_expression=row["cron_expression"], event_type=row["event_type"],
            event_filter=json.loads(row["event_filter_json"]) if row["event_filter_json"] else None,
            action_type=row["action_type"], project=row["project"], part=row["part"],
            prompt=row["prompt"], flow_spec_json=row["flow_spec_json"],
            enabled=bool(row["enabled"]), created_at=row["created_at"],
            last_fired_at=row["last_fired_at"], last_status=row["last_status"],
            model=row["model"] if "model" in row.keys() else None,
            agentic=bool(row["agentic"]) if "agentic" in row.keys() else True,
            python_target=row["python_target"] if "python_target" in row.keys() else None,
            python_kwargs=json.loads(row["python_kwargs_json"]) if row["python_kwargs_json"] else None,
        )

    def list_all(self, *, enabled_only: bool = False) -> list[Schedule]:
        query = "SELECT * FROM schedules"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY name ASC"
        with closing(self._connect()) as conn:
            rows = conn.execute(query).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def get(self, name: str) -> Schedule | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM schedules WHERE name = ?", (name,)).fetchone()
        return self._row_to_schedule(row) if row else None

    def set_enabled(self, name: str, enabled: bool) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE schedules SET enabled = ? WHERE name = ?", (int(enabled), name))

    def set_cron_expression(self, name: str, cron_expression: str) -> None:
        """Change an existing cron schedule without replacing its durable run history."""
        croniter(cron_expression)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE schedules SET cron_expression = ? WHERE name = ? AND trigger_type = 'cron'",
                (cron_expression, name),
            )

    def disable_admin_managed_schedules(self) -> int:
        """Persistently disable legacy cron workers now owned by the Admin."""
        placeholders = ",".join("?" for _ in ADMIN_MANAGED_SCHEDULES)
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                f"UPDATE schedules SET enabled=0 WHERE enabled=1 AND name IN ({placeholders})",
                tuple(ADMIN_MANAGED_SCHEDULES),
            )
        return int(cur.rowcount)

    def remove(self, name: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM schedules WHERE name = ?", (name,))

    def record_fire(
        self, schedule_id: int, status: str, *,
        summary: str | None = None, trigger_context: dict[str, Any] | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        now = _now_iso()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE schedules SET last_fired_at = ?, last_status = ? WHERE id = ?",
                (now, status, schedule_id),
            )
            row = conn.execute("SELECT name FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            conn.execute(
                "INSERT INTO schedule_runs (schedule_id, schedule_name, fired_at, status, summary, trigger_context, trace_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    schedule_id, row["name"] if row else "unknown", now, status,
                    summary, json.dumps(trigger_context) if trigger_context else None,
                    json.dumps(trace) if trace else None,
                ),
            )

    def list_runs(self, name: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM schedule_runs"
        params: tuple = ()
        if name:
            query += " WHERE schedule_name = ?"
            params = (name,)
        query += " ORDER BY fired_at DESC LIMIT ?"
        params = (*params, limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if d.get("trigger_context"):
                d["trigger_context"] = json.loads(d["trigger_context"])
            if d.get("trace_json"):
                d["trace"] = json.loads(d.pop("trace_json"))
            else:
                d["trace"] = None
                d.pop("trace_json", None)
            results.append(d)
        return results


@dataclass
class ActionResult:
    summary: str | None
    trace: list[dict[str, Any]] | None = None


def _run_action(schedule: Schedule, *, prompt_override: str | None = None) -> ActionResult:
    """Fire a schedule's action. Synchronous -- run in a thread from the
    async loop so a slow agentic call doesn't block the scheduler tick.
    Returns an ActionResult: a text summary of what happened (for the
    run-history log), plus, for agentic actions, the full orchestration
    trace (per-branch/model/tool-call steps from the recursive/parallel
    delegation graph) so the run log can show more than just the final
    reply -- what backends/tools actually ran, not just what came back."""
    if schedule.action_type == "agentic":
        # `herald.project` is ambiguous -- herald/__init__.py defines a
        # `project()` function, but herald/project.py is ALSO a submodule.
        # Whichever gets imported last anywhere in the process wins on the
        # `herald` package's `project` attribute (a classic Python package
        # vs. same-named-member shadowing gotcha), so `import herald;
        # herald.project(...)` is not reliable -- it silently breaks the
        # moment anything else in the process does `import herald.project`
        # as a submodule import. Import the class directly instead.
        from herald.client import RouterClient
        from herald.project import Project
        proj = Project(schedule.project, client=RouterClient())
        part = proj.part(schedule.part)
        if schedule.agentic:
            prompt = prompt_override or schedule.prompt
            if schedule.trigger_type == "cron":
                # Cron-fired agentic schedules are otherwise fully stateless:
                # every invocation re-derives its plan from scratch with no
                # memory of what the previous run decided or half-finished,
                # which for a long-running "work through this checklist"
                # loop means it can spend an entire invocation's budget
                # re-investigating something it already investigated and
                # planned to implement last time, without ever committing.
                # Feed the last completed run's own final text back in as
                # continuity context so it can pick up where it left off
                # instead of re-starting cold every cycle.
                try:
                    prior_runs = ScheduleStore().list_runs(schedule.name, limit=10)
                except Exception:  # noqa: BLE001 -- continuity is best-effort
                    prior_runs = []
                prior_note = next(
                    (r.get("summary", "").strip() for r in prior_runs if (r.get("summary") or "").strip()),
                    "",
                )
                if prior_note:
                    prompt = (
                        f"{prompt}\n\n[Continuity note -- your own final message from the "
                        f"previous invocation of this loop, {len(prior_note)} chars, below. "
                        "This is a memory aid to avoid repeating work, NOT a verified fact -- "
                        "it may be incomplete, or even wrong (a past run may have claimed "
                        "something was done or complete without it actually being true). The "
                        "phase files ARE the ground truth: always check them yourself before "
                        "trusting any claim in this note, especially a claim that an item or "
                        "phase is already complete. If the note already investigated the "
                        "current item and named a concrete plan that the phase files confirm "
                        "is still needed, don't re-read the same files to re-derive that plan "
                        "-- go straight to making the edit (write_file/edit_file) it planned, "
                        "then check the item off. If it only lists tool calls with no plan, "
                        "or the phase files show the item already checked off, move forward.]\n"
                        f"{prior_note[:3000]}"
                    )
            full = part.agentic_with_trace(prompt, model=schedule.model)
            if isinstance(full, dict) and full.get("error"):
                # RouterClient._post()/_get() swallow transport-level failures
                # (timeouts, connection errors) into {"error": "..."} instead
                # of raising, so this must be checked explicitly -- otherwise
                # a genuine failure (e.g. the client-side 1800s timeout firing)
                # silently reads as empty successful content below, and the
                # run gets recorded "ok" with a blank summary forever.
                raise RuntimeError(f"agentic call failed: {full['error']}")
            content = full.get("choices", [{}])[0].get("message", {}).get("content", "")
            content = content if isinstance(content, str) else str(content)
            trace = full.get("orchestration_trace")
            if not content.strip() and trace:
                # Some models finish an iteration with real, successful tool
                # calls but no closing narration -- the run genuinely did
                # something, but a blank summary is both unreadable in the
                # log AND useless as this schedule's own continuity note for
                # its next invocation (see above). Synthesize a fallback from
                # the trace so there is always something to hand forward.
                def _tool_call_line(a: dict[str, Any]) -> str:
                    name = a.get("name")
                    result = a.get("result") or {}
                    if not result.get("ok"):
                        return f"- {name}: failed ({result.get('error', 'unknown error')})"
                    # Pull whatever text content the tool actually returned so
                    # the next invocation's continuity note carries real
                    # findings forward, not just "read_multiple_files(ok)" --
                    # a bare tool-name log is useless for resuming a broad
                    # investigative task like a multi-file audit.
                    payload = result.get("result") or {}
                    text_parts = [
                        block.get("text", "") for block in (payload.get("content") or [])
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    snippet = " ".join(text_parts).strip().replace("\n", " ")[:300]
                    args_note = f" args={a.get('arguments')}" if a.get("arguments") else ""
                    return f"- {name}{args_note}: {snippet or '(no text content)'}"

                tool_lines = [
                    _tool_call_line(a)
                    for step in trace for a in step.get("actions", []) if a.get("kind") == "tool"
                ]
                content = (
                    "(model produced no closing text this run -- it read/explored but did not "
                    "state a conclusion or decision) What it actually found:\n"
                    + ("\n".join(tool_lines) if tool_lines else "(no tool calls)")
                )
            return ActionResult(summary=content, trace=trace)
        else:
            response = part.chat(schedule.prompt, model=schedule.model)
            return ActionResult(summary=response if isinstance(response, str) else str(response))
    elif schedule.action_type == "flow":
        from herald.flow import FlowSpec, FlowRunner
        from herald import _get_client

        spec_dict = json.loads(schedule.flow_spec_json)
        spec = FlowSpec.from_dict(spec_dict)
        client = _get_client()
        project_name = schedule.project or "default"

        def call_agent(agent, prompt, mode):
            reply = client.chat_scoped(prompt, project=project_name, part=agent.name, model=None)
            return {"content": reply}

        runner = FlowRunner(call_agent)
        result = runner.run(spec, input_text=schedule.prompt or "")
        return ActionResult(summary=str(result.get("final")) if isinstance(result, dict) else None)
    elif schedule.action_type == "python":
        import importlib

        if schedule.python_target not in PYTHON_ACTION_ALLOWLIST:
            raise ValueError(f"python_target {schedule.python_target!r} is not allowlisted")
        module_path, func_name = schedule.python_target.rsplit(".", 1)
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        result = func(**(schedule.python_kwargs or {}))
        return ActionResult(summary=result if isinstance(result, str) else str(result))
    else:
        raise ValueError(f"unknown action_type: {schedule.action_type}")


# Module-level (not per-instance) since `Scheduler(store)` is re-instantiated
# fresh on every call site (codex_supervisor, the HTTP /run endpoint, the
# cron loop) rather than reused as a singleton -- per-instance in-flight
# tracking would always start empty and never see another instance's run.
# This is what actually let stuck native CLI sessions multiply: `_fire()`
# blocks for the whole run, but the supervisor's own cron nudge is *also* a
# blocking `_fire()` call inside its own background thread, so while a
# worker run is genuinely still in flight (last_fired_at only updates on
# completion, see record_fire below), every 2-minute supervisor tick saw
# the same stale timestamp, concluded "stalled", and launched *another*
# concurrent `_fire()` on the same schedule -- several real CLI processes
# piling up on one logical task instead of one running to completion.
_RUNNING_SCHEDULE_IDS: set[int] = set()
_RUNNING_LOCK = threading.Lock()


def is_running(schedule_id: int) -> bool:
    with _RUNNING_LOCK:
        return schedule_id in _RUNNING_SCHEDULE_IDS


class Scheduler:
    def __init__(self, store: ScheduleStore | None = None) -> None:
        self.store = store or ScheduleStore()
        self._task: asyncio.Task | None = None
        self._event_sink_registered = False
        # last-checked minute per cron schedule, to avoid double-firing
        # within the same minute if the poll loop runs slightly early/late
        self._last_cron_minute: dict[int, str] = {}

    def _fire(self, schedule: Schedule, *, trigger_context: dict[str, Any] | None = None) -> None:
        with _RUNNING_LOCK:
            if schedule.id in _RUNNING_SCHEDULE_IDS:
                logger.info("scheduler: skipping %s because it is already running", schedule.name)
                return
            _RUNNING_SCHEDULE_IDS.add(schedule.id)
        logger.info("scheduler: firing %s (%s/%s)", schedule.name, schedule.trigger_type, schedule.action_type)
        try:
            prompt_override = (trigger_context or {}).get("prompt_override")
            result = _run_action(schedule, prompt_override=prompt_override)
            self.store.record_fire(
                schedule.id, "ok", summary=result.summary, trigger_context=trigger_context, trace=result.trace,
            )
            event_bus.emit_nowait(
                "schedule.fired", importance=0.3,
                payload={"name": schedule.name, "trigger_context": trigger_context or {}},
                source="scheduler",
            )
        except Exception as exc:  # noqa: BLE001 -- one bad schedule must not kill the loop
            logger.exception("scheduler: schedule %s failed", schedule.name)
            self.store.record_fire(schedule.id, "failed", summary=str(exc), trigger_context=trigger_context)
            event_bus.emit_nowait(
                "schedule.failed", importance=0.6,
                payload={"name": schedule.name, "error": str(exc)},
                source="scheduler",
            )
        finally:
            with _RUNNING_LOCK:
                _RUNNING_SCHEDULE_IDS.discard(schedule.id)

    def _is_cron_due(self, schedule: Schedule, now: datetime) -> bool:
        this_minute = now.strftime("%Y-%m-%dT%H:%M")
        if self._last_cron_minute.get(schedule.id) == this_minute:
            return False
        itr = croniter(schedule.cron_expression, now)
        prev_fire = itr.get_prev(datetime)
        # Due if the most recent scheduled fire time is within the current minute.
        due = prev_fire.strftime("%Y-%m-%dT%H:%M") == this_minute
        if due:
            self._last_cron_minute[schedule.id] = this_minute
        return due

    def _fire_in_background(self, schedule: Schedule, *, trigger_context: dict[str, Any] | None = None) -> None:
        """Schedule `_fire` on a worker thread and forget it -- `_fire` does
        a real, potentially very long (agentic calls can run up to 30 min)
        blocking network/subprocess call. Calling it directly from the
        async scheduler loop -- as this codebase did until this fix, despite
        `_run_action`'s own docstring claiming otherwise -- freezes the
        entire router process (every HTTP endpoint, the SSE stream, every
        other schedule) for the duration of that one call."""
        asyncio.create_task(asyncio.to_thread(self._fire, schedule, trigger_context=trigger_context))

    def _check_cron_schedules(self) -> None:
        now = datetime.now(UTC)
        for schedule in self.store.list_all(enabled_only=True):
            if schedule.trigger_type != "cron":
                continue
            if schedule.name in ADMIN_MANAGED_SCHEDULES:
                continue
            if self._is_cron_due(schedule, now):
                self._fire_in_background(schedule)

    def _matches_filter(self, schedule: Schedule, event: event_bus.Event) -> bool:
        if not schedule.event_filter:
            return True
        return all(event.payload.get(k) == v for k, v in schedule.event_filter.items())

    async def _on_event(self, event: event_bus.Event) -> None:
        for schedule in self.store.list_all(enabled_only=True):
            if schedule.trigger_type != "event":
                continue
            if schedule.event_type != event.event_type:
                continue
            if not self._matches_filter(schedule, event):
                continue
            self._fire_in_background(schedule, trigger_context=event.to_dict())

    async def _worker(self) -> None:
        while True:
            try:
                self._check_cron_schedules()
            except Exception:
                logger.exception("scheduler: cron check failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def start(self) -> None:
        if not self._event_sink_registered:
            event_bus.register_sink(
                self._on_event, event_types=None, min_importance=0.0, respect_quiet_mode=False,
            )
            self._event_sink_registered = True
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._worker())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._event_sink_registered:
            event_bus.unregister_sink(self._on_event)
            self._event_sink_registered = False


_scheduler = Scheduler()


def start() -> None:
    _scheduler.start()


async def stop() -> None:
    await _scheduler.stop()


def get_store() -> ScheduleStore:
    return _scheduler.store
