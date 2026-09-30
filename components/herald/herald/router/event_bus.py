"""Herald's state-change event bus.

Extends `herald.dev.hook` (pre/post inference interceptors) rather than
replacing it. That mechanism only fires around inference calls, runs
synchronously in-process, and has no delivery mechanism outside the calling
process. This module adds:

  - a fixed vocabulary of state-change event types (backend health, quota,
    node health, sync status) that aren't inference-shaped
  - an async, queued dispatcher so emitting an event never blocks the caller
  - pluggable sinks (notification delivery, logging, future consumers)
  - a continuous importance score per event, plus a standalone quiet-mode
    toggle -- two orthogonal gates, not one severity enum. Pattern confirmed
    from the user's own OpenClaw/satellite-bridge system: importance is
    "how much does this matter", quiet mode is "am I accepting interruptions
    at all right now" -- deliberately independent.

Usage:
    from herald.router.event_bus import emit, register_sink, set_quiet_mode

    await emit("quota.reset", importance=0.6, payload={"profile": "codex-primary"})

    async def my_sink(event: Event) -> None:
        ...
    register_sink(my_sink, event_types=["quota.reset", "backend.circuit_closed"])
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Fixed vocabulary. Extend here, not ad hoc at call sites, so consumers can
# rely on a closed set of event types.
EVENT_TYPES = frozenset({
    "backend.circuit_opened",
    "backend.circuit_closed",
    "backend.offline",
    "backend.recovered",
    "quota.threshold_crossed",
    "quota.reset",
    "node.offline",
    "node.online",
    "sync.conflict",
    "sync.failed",
    "schedule.fired",
    "schedule.failed",
    "approval.requested",
    "approval.decided",
    "capability.gap_detected",
    "capability.proposal_ready",
    "model.auto_unloaded",
    "agent.step",
})

# Below this importance, an event never escalates to a notification sink
# even with quiet mode off -- keeps trivial/no-op state churn out of sinks
# entirely rather than relying on every sink to filter it itself.
DEFAULT_MIN_NOTIFY_IMPORTANCE = 0.3

# In quiet mode, only events at or above this importance are allowed through
# to notification-class sinks at all. Sinks that opt out of quiet-mode
# gating (e.g. the log sink, which should always record everything) pass
# `respect_quiet_mode=False` when registering.
QUIET_MODE_THRESHOLD = 0.85


@dataclass(frozen=True)
class Event:
    event_type: str
    importance: float  # 0.0-1.0, continuous
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "importance": self.importance,
            "payload": self.payload,
            "source": self.source,
            "timestamp": self.timestamp,
        }


Sink = Callable[[Event], Awaitable[None]]


@dataclass
class _SinkRegistration:
    fn: Sink
    event_types: frozenset[str] | None  # None = all types
    min_importance: float
    respect_quiet_mode: bool


class EventBus:
    """Async, queued dispatcher. Emitting never blocks the caller -- events
    are pushed onto an in-process asyncio queue and a background worker
    delivers them to matching sinks."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._sinks: list[_SinkRegistration] = []
        self._quiet_mode: bool = False
        self._worker_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def register_sink(
        self,
        fn: Sink,
        *,
        event_types: list[str] | None = None,
        min_importance: float = DEFAULT_MIN_NOTIFY_IMPORTANCE,
        respect_quiet_mode: bool = True,
    ) -> None:
        if event_types:
            unknown = set(event_types) - EVENT_TYPES
            if unknown:
                raise ValueError(f"Unknown event type(s): {sorted(unknown)}")
        self._sinks.append(
            _SinkRegistration(
                fn=fn,
                event_types=frozenset(event_types) if event_types else None,
                min_importance=min_importance,
                respect_quiet_mode=respect_quiet_mode,
            )
        )

    def unregister_sink(self, fn: Sink) -> None:
        self._sinks = [s for s in self._sinks if s.fn is not fn]

    def set_quiet_mode(self, enabled: bool) -> None:
        self._quiet_mode = enabled

    @property
    def quiet_mode(self) -> bool:
        return self._quiet_mode

    async def emit(
        self,
        event_type: str,
        *,
        importance: float,
        payload: dict[str, Any] | None = None,
        source: str = "unknown",
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type!r}")
        if not 0.0 <= importance <= 1.0:
            raise ValueError(f"importance must be in [0.0, 1.0], got {importance}")
        event = Event(event_type=event_type, importance=importance, payload=payload or {}, source=source)
        await self._queue.put(event)

    def emit_nowait(
        self,
        event_type: str,
        *,
        importance: float,
        payload: dict[str, Any] | None = None,
        source: str = "unknown",
    ) -> None:
        """Sync-context convenience wrapper for call sites that aren't
        already in an async function (e.g. registry.py's record_failure)."""
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type!r}")
        event = Event(event_type=event_type, importance=importance, payload=payload or {}, source=source)
        try:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
            else:
                self._queue.put_nowait(event)
        except RuntimeError:
            # No running loop in this thread; drop rather than crash the
            # caller -- state-change notification is best-effort, never a
            # reason to break the operation that triggered it.
            logger.warning("event_bus: no event loop available, dropping %s", event_type)

    def _should_deliver(self, reg: _SinkRegistration, event: Event) -> bool:
        if reg.event_types is not None and event.event_type not in reg.event_types:
            return False
        if event.importance < reg.min_importance:
            return False
        if reg.respect_quiet_mode and self._quiet_mode and event.importance < QUIET_MODE_THRESHOLD:
            return False
        return True

    async def _worker(self) -> None:
        while True:
            event = await self._queue.get()
            for reg in list(self._sinks):
                if not self._should_deliver(reg, event):
                    continue
                try:
                    await reg.fn(event)
                except Exception:
                    logger.exception("event_bus: sink raised for event %s", event.event_type)
            self._queue.task_done()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.ensure_future(self._worker())

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        self._loop = None


# Module-level singleton -- one bus per router process, matching how
# registry.py/quota_tracker.py are already accessed as module-level state.
_bus = EventBus()


def start() -> None:
    _bus.start()


async def stop() -> None:
    await _bus.stop()


async def emit(event_type: str, *, importance: float, payload: dict[str, Any] | None = None, source: str = "unknown") -> None:
    await _bus.emit(event_type, importance=importance, payload=payload, source=source)


def emit_nowait(event_type: str, *, importance: float, payload: dict[str, Any] | None = None, source: str = "unknown") -> None:
    _bus.emit_nowait(event_type, importance=importance, payload=payload, source=source)


def register_sink(
    fn: Sink,
    *,
    event_types: list[str] | None = None,
    min_importance: float = DEFAULT_MIN_NOTIFY_IMPORTANCE,
    respect_quiet_mode: bool = True,
) -> None:
    _bus.register_sink(fn, event_types=event_types, min_importance=min_importance, respect_quiet_mode=respect_quiet_mode)


def unregister_sink(fn: Sink) -> None:
    _bus.unregister_sink(fn)


def set_quiet_mode(enabled: bool) -> None:
    _bus.set_quiet_mode(enabled)


def quiet_mode() -> bool:
    return _bus.quiet_mode


# Request-scoped tagging for deep, in-process-boundary event sources (e.g.
# adapters.py's call_cli(), which shells out to a real CLI subprocess and
# has no direct access to the originating request's project/part). Lives
# here rather than in server.py or adapters.py specifically so both can
# import it without a circular dependency (server.py already imports from
# adapters.py). Same set/reset-with-token pattern as server.py's existing
# `_run_progress` ContextVar for the same reason: ContextVars propagate
# correctly through a plain synchronous call chain (which this is -- no
# thread hop between the request handler and call_cli), but must be reset
# after use so a reused threadpool thread doesn't leak scope into an
# unrelated later request.
_event_scope: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "herald_event_scope", default=None
)


def set_scope(scope: dict[str, Any] | None) -> contextvars.Token:
    return _event_scope.set(scope)


def reset_scope(token: contextvars.Token) -> None:
    _event_scope.reset(token)


def get_scope() -> dict[str, Any]:
    return _event_scope.get() or {}


async def _log_sink(event: Event) -> None:
    logger.info("event: %s importance=%.2f source=%s payload=%s", event.event_type, event.importance, event.source, event.payload)


# Always-on log sink, ignores quiet mode -- every event gets recorded even
# when notification delivery is suppressed.
register_sink(_log_sink, min_importance=0.0, respect_quiet_mode=False)
