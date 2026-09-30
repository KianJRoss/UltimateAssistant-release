"""General-purpose parallel task execution over a Herald router."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class SwarmResult:
    index: int
    task: str
    content: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "task": self.task, "content": self.content, "error": self.error}


def run_swarm(client: Any, tasks: Iterable[str], *, model: str | None = None,
              max_workers: int = 4, instructions: str = "", **chat_options: Any) -> list[dict[str, Any]]:
    """Run independent text tasks concurrently and return results in input order."""
    work = [str(task) for task in tasks]
    if not work:
        return []
    if not 1 <= max_workers <= 64:
        raise ValueError("max_workers must be between 1 and 64")

    def execute(index: int, task: str) -> SwarmResult:
        prompt = f"{instructions.strip()}\n\n{task}".strip()
        try:
            content = client.chat(prompt, model=model, **chat_options)
            error = content[8:] if isinstance(content, str) and content.startswith("[error] ") else None
            return SwarmResult(index, task, content, error)
        except Exception as exc:  # isolate worker failures
            return SwarmResult(index, task, "", str(exc))

    results: list[SwarmResult] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(work))) as executor:
        futures = [executor.submit(execute, index, task) for index, task in enumerate(work)]
        results.extend(future.result() for future in as_completed(futures))
    return [result.to_dict() for result in sorted(results, key=lambda item: item.index)]
