"""Routing behavior profiles shared by the API, CLI, SDK, and flow engine."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class RoutingPolicy:
    name: str
    description: str
    automatic_order: tuple[str, ...]
    delegate_types: frozenset[str]
    free_only: bool = False
    compact_tool_catalog: bool = False
    tool_bridge: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["automatic_order"] = list(self.automatic_order)
        value["delegate_types"] = sorted(self.delegate_types)
        return value


POLICIES: dict[str, RoutingPolicy] = {
    "efficiency": RoutingPolicy(
        name="efficiency",
        description=(
            "Use free browser-session (g4f) and local models automatically -- both are "
            "genuinely unmetered, unlike free-tier api_key backends which still burn a daily "
            "quota. Keep paid APIs and subscription CLIs out of autonomous delegation unless "
            "explicitly selected."
        ),
        automatic_order=("browser_session", "local_model", "api_key", "cli"),
        delegate_types=frozenset({"api_key", "local_model", "browser_session"}),
        free_only=True,
        compact_tool_catalog=True,
        tool_bridge="fast",
    ),
    "balanced": RoutingPolicy(
        name="balanced",
        description="Prefer configured priorities and allow every healthy backend type.",
        automatic_order=("api_key", "local_model", "cli", "browser_session"),
        delegate_types=frozenset({"api_key", "local_model", "cli", "browser_session"}),
        tool_bridge="fast",
    ),
    "quality": RoutingPolicy(
        name="quality",
        description="Prefer subscription CLIs and paid APIs, then local and browser sessions.",
        automatic_order=("cli", "api_key", "local_model", "browser_session"),
        delegate_types=frozenset({"cli", "api_key", "local_model", "browser_session"}),
        tool_bridge="fast",
    ),
    "paired-efficiency": RoutingPolicy(
        name="paired-efficiency",
        description="Pair unmetered ChatGPT Plus (g4f) / local models as the strategic Brain with Gemini Flash as the fast Tool Bridge.",
        automatic_order=("browser_session", "local_model", "api_key", "cli"),
        delegate_types=frozenset({"api_key", "local_model", "browser_session"}),
        free_only=True,
        compact_tool_catalog=True,
        tool_bridge="gemini-3.7-flash",
    ),
    "paired-local": RoutingPolicy(
        name="paired-local",
        description="Pair high-capacity local GPU models (Qwen 2.5 Coder 32B) with fast local micro-models / Gemini Flash as Tool Bridge.",
        automatic_order=("local_model", "api_key"),
        delegate_types=frozenset({"local_model", "api_key"}),
        tool_bridge="fast",
    ),
    "paired-quality": RoutingPolicy(
        name="paired-quality",
        description="Pair flagship subscription CLIs (Claude 3.7 / Codex) with Gemini Flash as the rapid Tool Bridge.",
        automatic_order=("cli", "api_key", "local_model", "browser_session"),
        delegate_types=frozenset({"cli", "api_key", "local_model", "browser_session"}),
        tool_bridge="gemini-3.7-flash",
    ),
    "architect-editor": RoutingPolicy(
        name="architect-editor",
        description=(
            "Use a flagship CLI/API model as the architect for reasoning and planning, "
            "then Gemini Flash as the fast editor/tool bridge that applies the work."
        ),
        automatic_order=("cli", "api_key", "local_model", "browser_session"),
        delegate_types=frozenset({"cli", "api_key", "local_model", "browser_session"}),
        tool_bridge="gemini-3.7-flash",
    ),
    "local": RoutingPolicy(
        name="local",
        description="Use only locally hosted models for automatic routing and delegation.",
        automatic_order=("local_model",),
        delegate_types=frozenset({"local_model"}),
        tool_bridge="fast",
    ),
}


def classify_task_complexity(prompt: str) -> str:
    """Classify user prompt into complexity tiers: trivial, local, large_context, complex, or standard."""
    lower = prompt.lower().strip()
    words = lower.split()

    if len(words) <= 6 and any(k in lower for k in ("what time", "git status", "date", "whoami", "pwd", "version")):
        return "trivial"

    if any(k in lower for k in ("entire repo", "whole repository", "all files in", "codebase map", "full scan", "analyze all")):
        return "large_context"

    if any(k in lower for k in ("local only", "offline", "private key", ".env", "don't send to cloud", "on this machine")):
        return "local"

    if any(k in lower for k in ("investigate", "diagnose", "refactor", "architect", "why did", "debug", "ssh", "service", "systemctl", "trading", "flowfuse")):
        return "complex"

    return "standard"


def get_policy(name: str | None) -> RoutingPolicy:
    selected = (name or "balanced").lower()
    if selected in POLICIES:
        return POLICIES[selected]

    # Not a built-in -- check user-defined policies registered from a
    # project's router.yaml (persisted server-side, see custom_policies.py).
    try:
        from herald.router.custom_policies import get_store
        record = get_store().get(selected)
    except Exception:
        record = None
    if record is not None:
        return RoutingPolicy(
            name=record["name"], description=record["description"],
            automatic_order=record["automatic_order"], delegate_types=record["delegate_types"],
            free_only=record["free_only"], compact_tool_catalog=record["compact_tool_catalog"],
            tool_bridge=record["tool_bridge"],
        )

    raise ValueError(f"unknown routing mode '{selected}'; choose from {', '.join(POLICIES)}")


def is_free_backend(backend: Any) -> bool:
    return bool(
        backend.backend_type in {"browser_session", "local_model"}
        or backend.capabilities.get("free") is True
        or backend.cost_per_1k_tokens == 0
    )


def eligible_backends(backends: Iterable[Any], policy: RoutingPolicy) -> list[Any]:
    return [
        backend for backend in backends
        if backend.enabled and not backend.circuit_open
        and backend.backend_type in policy.delegate_types
        and (not policy.free_only or is_free_backend(backend))
    ]


# Backend-type preference by complexity tier, used only to reorder WITHIN a
# policy's already-eligible backend types (see automatic_model's `prompt`
# param) -- never used to add/remove eligibility, so an explicit policy's
# delegate_types/free_only constraints are always honored regardless of
# complexity. Complexity that isn't in this map (e.g. "local", which the
# classifier already routes toward local-only policies elsewhere) leaves
# the policy's own order untouched.
_COMPLEXITY_TYPE_BIAS: dict[str, tuple[str, ...]] = {
    "trivial": ("browser_session", "local_model", "api_key", "cli"),
    "standard": ("browser_session", "local_model", "api_key", "cli"),
    "complex": ("cli", "api_key", "local_model", "browser_session"),
    "large_context": ("cli", "api_key", "local_model", "browser_session"),
}


def _effective_order(policy: RoutingPolicy, prompt: str) -> tuple[str, ...]:
    """Reorder policy.automatic_order by complexity bias, restricted to the
    types the policy already allows -- a tie-break layer, not an override."""
    complexity = classify_task_complexity(prompt)
    bias = _COMPLEXITY_TYPE_BIAS.get(complexity)
    if not bias:
        return policy.automatic_order
    allowed = set(policy.automatic_order)
    reordered = tuple(t for t in bias if t in allowed)
    remainder = tuple(t for t in policy.automatic_order if t not in reordered)
    return reordered + remainder


def automatic_model(backends: Iterable[Any], policy: RoutingPolicy, prompt: str = "") -> str:
    """`prompt`, when given, makes automatic routing complexity-aware: the
    backend-type order is re-biased toward free/fast types for trivial/
    standard prompts and toward cli/api_key (quality) types for complex/
    large_context prompts. This only reorders among the types the policy
    already permits -- an explicitly requested policy's eligibility rules
    (delegate_types, free_only) are never widened or narrowed by prompt
    content. Omitting `prompt` (the default) reproduces prior behavior
    exactly."""
    available = eligible_backends(backends, policy)
    order = _effective_order(policy, prompt) if prompt else policy.automatic_order
    type_rank = {backend_type: index for index, backend_type in enumerate(order)}
    available.sort(key=lambda backend: (
        type_rank.get(backend.backend_type, len(type_rank)), backend.priority, backend.name,
    ))
    if not available:
        raise ValueError(f"no healthy backend satisfies the '{policy.name}' routing policy")
    # Prefer the logical failover pool when one exists.
    return available[0].pool_name or available[0].name


def model_catalog(backends: Iterable[Any], policy: RoutingPolicy, *, exclude: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for backend in eligible_backends(backends, policy):
        for name, kind in ((backend.name, backend.backend_type), (backend.pool_name, "pool")):
            if name and name != exclude and name not in seen:
                rows.append({"name": name, "type": kind})
                seen.add(name)
    return rows


def compact_tools(tools: list[dict[str, Any]], policy: RoutingPolicy) -> list[dict[str, Any]]:
    if not policy.compact_tool_catalog:
        return tools
    compacted = []
    for tool in tools:
        schema = tool.get("input_schema") or {}
        properties = {
            name: {"type": value.get("type", "any")}
            for name, value in (schema.get("properties") or {}).items()
            if isinstance(value, dict)
        }
        compacted.append({
            **tool,
            "description": str(tool.get("description", ""))[:160],
            "input_schema": {
                "type": "object", "properties": properties,
                "required": schema.get("required", []),
            },
        })
    return compacted
