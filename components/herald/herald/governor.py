"""Local model concurrency governor.

Enforces hard limits on simultaneous local model calls so multiple projects
hitting the router concurrently don't thrash VRAM or corrupt the GPU context.

Design
------
* One threading.Semaphore per runtime (LM Studio, Ollama) — they each have
  their own GPU context and can only safely serve one request at a time.
* Calls QUEUE, not reject — a second call blocks until the first finishes,
  with a configurable timeout (default 5 min). This matches real usage:
  a call that arrives while the GPU is busy should wait, not fail.
* VRAM budget awareness — each local_model backend can declare its VRAM
  footprint in capabilities {"vram_gb": N, "runtime": "lmstudio"}. The
  governor checks the declared budget before allowing a load.
* Idle tracking — records the last-call time per backend so an auto-unload
  daemon (future) knows what's been sitting idle.
* Queue depth telemetry — exposed at GET /control/local/governor so the
  dashboard can show "2 calls waiting for lmstudio slot".

Environment variables
---------------------
HERALD_MAX_LOCAL_CONCURRENT   Max parallel calls per runtime (default 1)
HERALD_LOCAL_TIMEOUT_SEC      Max seconds to wait for a slot (default 300)
HERALD_VRAM_BUDGET_GB         Total VRAM budget in GB (default 24)
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any


MAX_CONCURRENT = int(os.environ.get("HERALD_MAX_LOCAL_CONCURRENT", "1"))
TIMEOUT_SEC = float(os.environ.get("HERALD_LOCAL_TIMEOUT_SEC", "300"))
VRAM_BUDGET_GB = float(os.environ.get("HERALD_VRAM_BUDGET_GB", "24"))

# Runtimes that need governing. Any local_model backend whose config has
# "runtime" in this set gets queued through the governor.
GOVERNED_RUNTIMES = {"lmstudio", "ollama"}


@dataclass
class RuntimeStats:
    runtime: str
    active_calls: int = 0
    queued_calls: int = 0
    total_calls: int = 0
    total_wait_ms: float = 0.0
    last_call_at: float = 0.0
    active_backend: str | None = None


class LocalModelGovernor:
    """Process-global concurrency governor for local model, G4F, and CLI runtimes.

    Serializes calls through per-resource semaphores so concurrent threads,
    subagents, or parallel harness calls never race or corrupt backend sessions.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.Semaphore] = {
            rt: threading.Semaphore(MAX_CONCURRENT) for rt in GOVERNED_RUNTIMES
        }
        self._stats: dict[str, RuntimeStats] = {
            rt: RuntimeStats(runtime=rt) for rt in GOVERNED_RUNTIMES
        }
        self._idle_timestamps: dict[str, float] = {}

    def _get_semaphore(self, resource_key: str, max_concurrent: int = 1) -> threading.Semaphore:
        with self._lock:
            if resource_key not in self._semaphores:
                self._semaphores[resource_key] = threading.Semaphore(max_concurrent)
                self._stats[resource_key] = RuntimeStats(runtime=resource_key)
            return self._semaphores[resource_key]

    def acquire(self, runtime: str, backend_name: str, timeout: float = TIMEOUT_SEC) -> bool:
        """Block until a slot is available or timeout expires."""
        sem = self._get_semaphore(runtime)

        wait_start = time.monotonic()
        with self._lock:
            if runtime in self._stats:
                self._stats[runtime].queued_calls += 1

        acquired = sem.acquire(timeout=timeout)
        wait_ms = (time.monotonic() - wait_start) * 1000

        with self._lock:
            if runtime in self._stats:
                s = self._stats[runtime]
                s.queued_calls = max(0, s.queued_calls - 1)
                if acquired:
                    s.active_calls += 1
                    s.total_calls += 1
                    s.total_wait_ms += wait_ms
                    s.last_call_at = time.time()
                    s.active_backend = backend_name
            self._idle_timestamps[backend_name] = time.time()

        return acquired

    def release(self, runtime: str, backend_name: str) -> None:
        """Release the resource slot after a call completes."""
        sem = self._semaphores.get(runtime)
        if sem is None:
            return
        with self._lock:
            if runtime in self._stats:
                s = self._stats[runtime]
                s.active_calls = max(0, s.active_calls - 1)
                s.last_call_at = time.time()
                s.active_backend = None
            self._idle_timestamps[backend_name] = time.time()
        sem.release()


    # ------------------------------------------------------------------
    # VRAM budget check
    # ------------------------------------------------------------------

    def check_vram_budget(
        self,
        requested_gb: float,
        currently_loaded_gb: float = 0.0,
    ) -> tuple[bool, str]:
        """Check if loading a model fits within the VRAM budget.

        Args:
            requested_gb:       VRAM needed by the model to load.
            currently_loaded_gb: VRAM already occupied by loaded models.

        Returns:
            (ok, reason) — ok=True if safe to load.
        """
        if requested_gb <= 0:
            return True, "no vram declared — assuming fits"
        available = VRAM_BUDGET_GB - currently_loaded_gb
        if requested_gb > available:
            return False, (
                f"model needs {requested_gb:.1f} GB VRAM but only "
                f"{available:.1f} GB available ({VRAM_BUDGET_GB:.1f} GB total, "
                f"{currently_loaded_gb:.1f} GB in use)"
            )
        return True, f"{available:.1f} GB available — fits"

    # ------------------------------------------------------------------
    # Stats / telemetry — exposed via GET /control/local/governor
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "config": {
                    "max_concurrent_per_runtime": MAX_CONCURRENT,
                    "timeout_sec": TIMEOUT_SEC,
                    "vram_budget_gb": VRAM_BUDGET_GB,
                },
                "runtimes": {
                    rt: {
                        "active_calls": s.active_calls,
                        "queued_calls": s.queued_calls,
                        "total_calls": s.total_calls,
                        "avg_wait_ms": (
                            s.total_wait_ms / s.total_calls
                            if s.total_calls else 0
                        ),
                        "active_backend": s.active_backend,
                        "last_call_at": s.last_call_at or None,
                        "idle_seconds": (
                            time.time() - s.last_call_at
                            if s.last_call_at else None
                        ),
                    }
                    for rt, s in self._stats.items()
                },
            }

    def idle_backends(self, idle_threshold_sec: float = 600.0) -> list[str]:
        """Return backend names that have been idle longer than the threshold.

        Used by a future auto-unload daemon to know what's safe to unload.
        """
        now = time.time()
        with self._lock:
            return [
                name for name, ts in self._idle_timestamps.items()
                if now - ts >= idle_threshold_sec
            ]


# ---------------------------------------------------------------------------
# Module-level singleton — imported by server.py
# ---------------------------------------------------------------------------
governor = LocalModelGovernor()
