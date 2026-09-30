"""Developer Extensibility & Modification Suite for Herald.

Enables seamless programmatic customization of Herald:
1. `@herald.tool`: Turn any Python function into an AI-callable tool with automatic JSON Schema generation.
2. `herald.register_backend`: Programmatically add custom models, APIs, and CLI profiles.
3. `herald.register_persona`: Define custom steering personas and prompt modifiers.
4. `herald.hook`: Attach pre/post inference interceptors and telemetry hooks.
"""
from __future__ import annotations

import inspect
import json
import logging
from functools import wraps
from typing import Any, Callable, get_type_hints

logger = logging.getLogger("herald.dev")

# In-Memory Custom Developer Tool Registry
_CUSTOM_TOOLS: dict[str, dict[str, Any]] = {}
_HOOKS: dict[str, list[Callable[..., Any]]] = {
    "pre_chat": [],
    "post_chat": [],
    "on_error": [],
}


def _python_type_to_json_type(py_type: Any) -> str:
    if py_type in (int, float):
        return "number" if py_type is float else "integer"
    if py_type is bool:
        return "boolean"
    if py_type is list:
        return "array"
    if py_type is dict:
        return "object"
    return "string"


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Callable[..., Any]:
    """Decorator to expose any Python function as a Herald agent tool.

    Usage:
        @herald.tool(description="Calculate mortgage monthly payments")
        def calculate_mortgage(principal: float, rate_annual_pct: float, years: int) -> float:
            r = (rate_annual_pct / 100) / 12
            n = years * 12
            return principal * (r * (1 + r)**n) / ((1 + r)**n - 1)
    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        doc = description or (fn.__doc__ or "").strip() or f"Execute {tool_name}"

        # Introspect function signature and generate JSON schema
        sig = inspect.signature(fn)
        type_hints = get_type_hints(fn)

        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue
            param_type = type_hints.get(param_name, str)
            json_type = _python_type_to_json_type(param_type)
            properties[param_name] = {
                "type": json_type,
                "description": f"Parameter {param_name}",
            }
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        schema = {
            "name": tool_name,
            "description": doc,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

        _CUSTOM_TOOLS[tool_name] = {
            "name": tool_name,
            "schema": schema,
            "func": fn,
        }

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        wrapper.__herald_tool__ = schema  # type: ignore[attr-defined]
        return wrapper

    if func is not None:
        return decorator(func)
    return decorator


def list_custom_tools() -> list[dict[str, Any]]:
    """List all registered custom developer tools."""
    return [t["schema"] for t in _CUSTOM_TOOLS.values()]


def execute_custom_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Execute a registered custom developer tool by name."""
    if name not in _CUSTOM_TOOLS:
        raise KeyError(f"Custom tool '{name}' not found in Herald registry")
    fn = _CUSTOM_TOOLS[name]["func"]
    return fn(**arguments)


def register_backend(
    name: str,
    *,
    backend_type: str = "api_key",
    config: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    priority: int = 100,
    pool_name: str | None = None,
    cost_per_1k_tokens: float = 0.0,
    enabled: bool = True,
) -> dict[str, Any]:
    """Programmatically register a custom backend into Herald's router database.

    Usage:
        herald.register_backend(
            "my-custom-llm",
            backend_type="api_key",
            config={"secret_ref": "env:OPENAI_API_KEY", "base_url": "https://api.openai.com/v1", "model_name": "gpt-4o"},
            pool_name="free-cloud",
            priority=10,
        )
    """
    from herald.router.registry import Registry
    reg = Registry()
    reg.register(
        backend_type=backend_type,
        name=name,
        config=config or {},
        capabilities=capabilities or {"fast": True, "tools": True},
        cost_per_1k_tokens=cost_per_1k_tokens,
        priority=priority,
        enabled=enabled,
        pool_name=pool_name,
    )
    return {"status": "ok", "backend": name, "pool": pool_name}


def register_persona(
    name: str,
    system_prompt: str,
    description: str | None = None,
) -> None:
    """Programmatically register a custom steering persona into Herald.

    Usage:
        herald.register_persona(
            "sql_expert",
            system_prompt="You are a PostgreSQL DBA. Output only valid SQL scripts inside markdown code blocks.",
        )
    """
    from herald.steering import steering
    steering.register(name, system_prompt)



def hook(event_name: str) -> Callable[..., Any]:
    """Decorator to register a pre/post hook for Herald inferences.

    Usage:
        @herald.hook("pre_chat")
        def audit_prompt(prompt, **kwargs):
            print(f"[AUDIT] Sending prompt: {prompt[:50]}...")
            return prompt
    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if event_name not in _HOOKS:
            _HOOKS[event_name] = []
        _HOOKS[event_name].append(fn)
        return fn
    return decorator


def run_hooks(event_name: str, value: Any, **kwargs: Any) -> Any:
    """Execute all registered hooks for a given lifecycle event."""
    current = value
    for h in _HOOKS.get(event_name, []):
        try:
            res = h(current, **kwargs)
            if res is not None:
                current = res
        except Exception as exc:
            logger.warning(f"Hook '{event_name}' error in {h.__name__}: {exc}")
    return current
