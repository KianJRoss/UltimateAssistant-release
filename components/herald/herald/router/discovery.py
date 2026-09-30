"""Registers every downloaded local model (LM Studio + Ollama, on whatever
node hosts them) as a `local_model` backend -- turns node_control.py's raw
discovery data into actual registry rows, so a newly-downloaded model shows
up as a callable backend without hand-writing a registration script.

Naming: model keys from LM Studio ("qwen/qwen3-8b") and Ollama ("qwen3:8b")
contain "/" and ":", which the router's delegation regex
(orchestrate.CALL_PATTERN) and the `DELETE /backends/{name}` path param
can't address -- so registered backend names are sanitized (both chars
replaced with "-"), with the real, unsanitized model key kept in `config`
for the actual API call.

Enabled state mirrors whatever node_control's discovery found loaded at
that moment -- a downloaded-but-unloaded model registers disabled rather
than enabled-but-guaranteed-to-fail-and-trip-its-breaker. Loading/unloading
a model (via the existing /control/local/load endpoints) should flip this
via set_local_model_loaded() below, not require rerunning discovery.
"""
from __future__ import annotations

from typing import Any

from herald.router import node_control
from herald.router.registry import Registry

def _get_node_base_urls(node: str) -> dict[str, str]:
    from herald.tailscale import get_device
    device = get_device(node)
    host = "127.0.0.1"
    if device and device.get("ips"):
        host = device["ips"][0]
    elif device and device.get("hostname"):
        host = device["hostname"]
    elif node not in ("local", "localhost", "127.0.0.1"):
        host = node

    return {
        "lmstudio": f"http://{host}:1234/v1",
        "ollama": f"http://{host}:11434/v1",
    }


def _sanitize_name(runtime: str, model_key: str) -> str:
    return f"{runtime}-{model_key}".replace("/", "-").replace(":", "-")


def discover_and_register(registry: Registry, node: str = "local") -> dict[str, Any]:
    base_urls = _get_node_base_urls(node)

    registered: list[dict[str, Any]] = []

    for entry in node_control.lmstudio_discover(node):
        name = _sanitize_name("lmstudio", entry["model_key"])
        registry.register(
            backend_type="local_model", name=name,
            config={"base_url": base_urls["lmstudio"], "model": entry["model_key"], "node": node, "runtime": "lmstudio"},
            enabled=entry["loaded"],
        )
        registered.append({"name": name, "runtime": "lmstudio", "model_key": entry["model_key"], "enabled": entry["loaded"]})

    for entry in node_control.ollama_discover(node):
        name = _sanitize_name("ollama", entry["model_key"])
        registry.register(
            backend_type="local_model", name=name,
            config={"base_url": base_urls["ollama"], "model": entry["model_key"], "node": node, "runtime": "ollama"},
            enabled=entry["loaded"],
        )
        registered.append({"name": name, "runtime": "ollama", "model_key": entry["model_key"], "enabled": entry["loaded"]})

    return {"node": node, "registered": registered, "count": len(registered)}


def set_local_model_loaded(registry: Registry, node: str, runtime: str, model_key: str, loaded: bool) -> None:
    """Flip a discovered local model's enabled state after an explicit
    load/unload action -- avoids a full rediscovery round-trip just to
    reflect a state change the caller already knows happened."""
    name = _sanitize_name(runtime, model_key)
    backend = registry.get(name)
    if backend is not None:
        registry.register(
            backend_type=backend.backend_type, name=name, config=backend.config,
            capabilities=backend.capabilities, cost_per_1k_tokens=backend.cost_per_1k_tokens,
            priority=backend.priority, enabled=loaded, pool_name=backend.pool_name,
        )
