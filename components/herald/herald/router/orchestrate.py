"""Generalized layered+parallel orchestration -- the same concept as PAL's
clink branching (nesting depth + branch IDs + parallel-sibling tracking),
built once at the router level instead of per-backend-type, so any backend
(CLI, API-key, or local model -- NOT browser_session, see below) can consult
another registered backend as part of answering a request.

Design choice: prompt-based tool calling, not each provider's native
function-calling schema. None of the CLI/api_key/local_model adapters pass a
`tools` parameter today, and a prompt-based convention works identically
across all of them (and is what makes this possible at all for CLI tools,
which have no function-calling API to hook into in the first place) --
one mechanism, not three.

browser_session (g4f) backends are deliberately excluded from ever receiving
the "you can call other backends" instruction: they participate fully as
call TARGETS (any backend can consult one), but never as callers themselves.
g4f replays ChatGPT's consumer web endpoint, which has no function-calling
concept at all -- there would be nothing to hook a real implementation into
even if we wanted one.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from herald.router.registry import Backend, Registry

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_PARALLEL_SIBLINGS = 4
DEFAULT_MAX_ITERATIONS = 6

CALL_PATTERN = re.compile(r"^CALL_BACKEND:\s*([\w.\-]+)\s*\|\s*(.+)$", re.MULTILINE)

AGENTIC_ELIGIBLE_TYPES = {"cli", "api_key", "local_model"}


@dataclass
class OrchestrationContext:
    branch_id: str
    depth: int
    max_depth: int = DEFAULT_MAX_DEPTH
    max_parallel_siblings: int = DEFAULT_MAX_PARALLEL_SIBLINGS
    parent_branch_id: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)

    def child(self) -> "OrchestrationContext":
        sibling = uuid.uuid4().hex[:12]
        new_branch_id = f"{self.branch_id}.{sibling}"
        return OrchestrationContext(
            branch_id=new_branch_id,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            max_parallel_siblings=self.max_parallel_siblings,
            parent_branch_id=self.branch_id,
            trace=self.trace,
        )


def _tool_instructions(registry: Registry, exclude_name: str) -> str:
    callable_backends = [
        b for b in registry.list_all(enabled_only=True)
        if b.backend_type in AGENTIC_ELIGIBLE_TYPES and b.name != exclude_name and not b.circuit_open
    ]
    if not callable_backends:
        return ""
    lines = "\n".join(f"  - {b.name} ({b.backend_type})" for b in callable_backends)
    return (
        "\n\nYou may consult another available model/tool if it would help answer this. "
        f"Available:\n{lines}\n"
        "To consult one, respond with EXACTLY one line in this format and nothing else "
        "on that line: CALL_BACKEND: <name> | <prompt for that backend>\n"
        "You may do this at most once per response. If you don't need to consult anything, "
        "just answer normally."
    )


def run_agentic(
    registry: Registry,
    execute_single_fn,
    model_name: str,
    prompt: str,
    *,
    context: OrchestrationContext | None = None,
) -> dict[str, Any]:
    """execute_single_fn(model_name, prompt) -> str is the plain, non-agentic
    single-backend call (main.py's _execute) -- reused here rather than
    duplicated, so a nested call goes through the exact same circuit-breaker
    and adapter dispatch path as a top-level one."""
    if context is None:
        context = OrchestrationContext(branch_id=uuid.uuid4().hex[:12], depth=0)

    backend = registry.get(model_name)
    if backend is None:
        candidates = registry.list_pool(model_name)
        backend = candidates[0] if candidates else None
    is_eligible = backend is not None and backend.backend_type in AGENTIC_ELIGIBLE_TYPES
    can_delegate = is_eligible and context.depth + 1 < context.max_depth

    conversation = prompt
    for iteration in range(DEFAULT_MAX_ITERATIONS):
        tool_text = _tool_instructions(registry, model_name) if can_delegate else ""
        response = execute_single_fn(
            model_name, conversation + tool_text, branch_id=context.branch_id, depth=context.depth,
        )

        match = CALL_PATTERN.search(response)
        if not match or not can_delegate:
            context.trace.append({
                "branch_id": context.branch_id, "depth": context.depth,
                "model": model_name, "iteration": iteration, "final": True,
            })
            return {"content": response.strip(), "trace": context.trace}

        target_name, target_prompt = match.group(1).strip(), match.group(2).strip()
        context.trace.append({
            "branch_id": context.branch_id, "depth": context.depth,
            "model": model_name, "iteration": iteration,
            "delegated_to": target_name, "delegated_prompt": target_prompt,
        })

        child_ctx = context.child()
        nested = run_agentic(registry, execute_single_fn, target_name, target_prompt, context=child_ctx)

        conversation = (
            f"{prompt}\n\n"
            f"[You consulted {target_name}, which replied: {nested['content']}]\n\n"
            f"Now give your final answer."
        )
        can_delegate = False  # at most one delegation per response, per the instructions given

    context.trace.append({
        "branch_id": context.branch_id, "depth": context.depth,
        "model": model_name, "final": True, "reason": "max_iterations_reached",
    })
    return {"content": conversation.strip(), "trace": context.trace}
