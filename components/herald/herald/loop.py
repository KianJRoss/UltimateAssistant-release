"""General-purpose iterative agent loop for applications built on Herald.

This is a public-API building block: "keep working on a task, one step at a
time, until some condition says stop." It is deliberately generic -- unlike
herald/router/admin_loop.py (which encodes one specific standing task for
Herald's own development), this module knows nothing about what the loop is
for. It just drives a StatefulAgent (or any callable) through repeated
prompts, checks a stop condition after each step, and surfaces retries,
backoff, and history so callers can build polling workers, review queues,
or "keep refining until good enough" agents without writing that plumbing
themselves.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from herald.errors import HeraldError, SessionBusyError


@dataclass(frozen=True)
class LoopStep:
    """One completed iteration."""

    index: int
    prompt: str
    output: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "prompt": self.prompt, "output": self.output, "error": self.error}


@dataclass(frozen=True)
class LoopResult:
    """The full outcome of a Loop.run() call."""

    steps: list[LoopStep]
    stopped_reason: str
    """One of: 'stop_condition', 'max_iterations', 'error'."""

    @property
    def last_output(self) -> str:
        return self.steps[-1].output if self.steps else ""

    @property
    def iterations(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped_reason": self.stopped_reason,
            "iterations": self.iterations,
            "last_output": self.last_output,
            "steps": [step.to_dict() for step in self.steps],
        }


# A stop condition receives the LoopStep just produced and the full history
# so far, and returns True when the loop should end.
StopCondition = Callable[[LoopStep, list[LoopStep]], bool]

# The next prompt to send, given the history so far. Called before every
# step including the first (with an empty history).
PromptBuilder = Callable[[list[LoopStep]], str]


class Loop:
    """Drives one callable (typically a StatefulAgent.ask) through repeated
    steps until a stop condition is met, a step budget is exhausted, or an
    unrecoverable error occurs.

    Works with anything callable as `call(prompt: str) -> str`, so it wraps
    a StatefulAgent, a Harness.ask, or a bare function equally well:

        agent = harness.agent("Worker", memory="ingest-queue")
        loop = Loop(agent.ask, prompt="Process the next queued item.",
                    stop_condition=lambda step, history: "DONE" in step.output,
                    max_iterations=25)
        result = loop.run()
    """

    def __init__(
        self,
        call: Callable[[str], str],
        *,
        prompt: str | PromptBuilder,
        stop_condition: StopCondition | None = None,
        max_iterations: int = 50,
        on_step: Callable[[LoopStep], None] | None = None,
        sleep_seconds: float = 0.0,
        retry_on_error: int = 2,
        retry_backoff_seconds: float = 5.0,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if retry_on_error < 0:
            raise ValueError("retry_on_error must be zero or positive")
        self._call = call
        self._prompt = prompt
        self._stop_condition = stop_condition
        self._max_iterations = max_iterations
        self._on_step = on_step
        self._sleep_seconds = max(0.0, sleep_seconds)
        self._retry_on_error = retry_on_error
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    def _next_prompt(self, history: list[LoopStep]) -> str:
        if callable(self._prompt):
            return self._prompt(history)
        return self._prompt

    def _call_with_retry(self, prompt: str) -> tuple[str, str | None]:
        attempts = self._retry_on_error + 1
        last_error: str | None = None
        for attempt in range(attempts):
            try:
                return self._call(prompt), None
            except SessionBusyError:
                # A session mid-request rejects overlapping calls by design;
                # this is not a fault worth burning a retry budget on.
                raise
            except HeraldError as exc:
                last_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - isolate caller-supplied `call`
                last_error = str(exc)
            if attempt < attempts - 1 and self._retry_backoff_seconds:
                time.sleep(self._retry_backoff_seconds)
        return "", last_error

    def run(self) -> LoopResult:
        history: list[LoopStep] = []
        for index in range(self._max_iterations):
            prompt = self._next_prompt(history)
            output, error = self._call_with_retry(prompt)
            step = LoopStep(index=index, prompt=prompt, output=output, error=error)
            history.append(step)
            if self._on_step is not None:
                self._on_step(step)
            if error is not None:
                return LoopResult(steps=history, stopped_reason="error")
            if self._stop_condition is not None and self._stop_condition(step, history):
                return LoopResult(steps=history, stopped_reason="stop_condition")
            if index < self._max_iterations - 1 and self._sleep_seconds:
                time.sleep(self._sleep_seconds)
        return LoopResult(steps=history, stopped_reason="max_iterations")


def run_loop(
    call: Callable[[str], str],
    *,
    prompt: str | PromptBuilder,
    stop_condition: StopCondition | None = None,
    max_iterations: int = 50,
    on_step: Callable[[LoopStep], None] | None = None,
    sleep_seconds: float = 0.0,
    retry_on_error: int = 2,
    retry_backoff_seconds: float = 5.0,
) -> LoopResult:
    """Functional shorthand for Loop(...).run()."""
    return Loop(
        call, prompt=prompt, stop_condition=stop_condition, max_iterations=max_iterations,
        on_step=on_step, sleep_seconds=sleep_seconds, retry_on_error=retry_on_error,
        retry_backoff_seconds=retry_backoff_seconds,
    ).run()


def until_contains(*markers: str) -> StopCondition:
    """Common stop condition: stop once the model's output contains any marker.

        Loop(agent.ask, prompt="...", stop_condition=until_contains("DONE", "COMPLETE"))
    """
    needles = [marker.casefold() for marker in markers]

    def _check(step: LoopStep, history: list[LoopStep]) -> bool:
        haystack = step.output.casefold()
        return any(needle in haystack for needle in needles)

    return _check
