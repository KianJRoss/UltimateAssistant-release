"""Universal Model Connectors for Herald.

Enables 1-line connection and auto-discovery for:
- Ollama (Local & Remote/Tailscale)
- LM Studio (Local & Remote/Tailscale)
- OpenRouter Hub
- Groq / DeepSeek / Mistral
- Any OpenAI-Compatible endpoint (vLLM, LiteLLM, Ollama, TGI)
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from herald.router.registry import Registry

logger = logging.getLogger("herald.connectors")


def _sanitize_name(runtime: str, model_key: str) -> str:
    return f"{runtime}-{model_key}".replace("/", "-").replace(":", "-")


def connect_ollama(
    url: str = "http://localhost:11434",
    *,
    pool_name: str = "local-coder",
    priority: int = 20,
) -> dict[str, Any]:
    """Auto-discover all downloaded Ollama models and register them into Herald."""
    base_url = url.rstrip("/")
    api_url = f"{base_url}/api/tags"
    reg = Registry()
    registered = []

    try:
        resp = httpx.get(api_url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("models", [])
        for m in models:
            model_key = m.get("name")
            if not model_key:
                continue
            name = _sanitize_name("ollama", model_key)
            reg.register(
                backend_type="local_model",
                name=name,
                config={"base_url": f"{base_url}/v1", "model": model_key, "runtime": "ollama"},
                capabilities={"code": "coder" in model_key.lower(), "fast": True, "local": True},
                priority=priority,
                enabled=True,
                pool_name=pool_name,
            )
            registered.append(name)
        return {"status": "ok", "runtime": "ollama", "url": base_url, "registered": registered, "count": len(registered)}
    except Exception as exc:
        return {"status": "error", "error": f"Failed to discover Ollama at {url}: {exc}"}


def connect_lmstudio(
    url: str = "http://localhost:1234/v1",
    *,
    pool_name: str = "local-coder",
    priority: int = 15,
) -> dict[str, Any]:
    """Auto-discover currently loaded models in LM Studio and register them into Herald."""
    base_url = url.rstrip("/")
    reg = Registry()
    registered = []

    try:
        resp = httpx.get(f"{base_url}/models", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", [])
        for m in models:
            model_key = m.get("id")
            if not model_key:
                continue
            name = _sanitize_name("lmstudio", model_key)
            reg.register(
                backend_type="local_model",
                name=name,
                config={"base_url": base_url, "model": model_key, "runtime": "lmstudio"},
                capabilities={"code": "coder" in model_key.lower() or "qwen" in model_key.lower(), "reasoning": True, "local": True},
                priority=priority,
                enabled=True,
                pool_name=pool_name,
            )
            registered.append(name)
        return {"status": "ok", "runtime": "lmstudio", "url": base_url, "registered": registered, "count": len(registered)}
    except Exception as exc:
        return {"status": "error", "error": f"Failed to discover LM Studio at {url}: {exc}"}


def connect_openrouter(
    secret_ref: str,
    *,
    models: list[str] | None = None,
    pool_name: str = "openrouter-pool",
    priority: int = 40,
) -> dict[str, Any]:
    """Connect OpenRouter without persisting the key in the router database."""
    from herald.router.account_registry import validate_secret_ref
    validate_secret_ref(secret_ref)
    default_models = [
        "anthropic/claude-3.5-sonnet",
        "meta-llama/llama-3.3-70b-instruct",
        "deepseek/deepseek-r1",
        "google/gemini-2.0-flash-exp:free",
    ]
    target_models = models or default_models
    reg = Registry()
    registered = []

    for m in target_models:
        name = f"openrouter-{_sanitize_name('', m).lstrip('-')}"
        reg.register(
            backend_type="api_key",
            name=name,
            config={
                "base_url": "https://openrouter.ai/api/v1",
                "secret_ref": secret_ref,
                "model_name": m,
                "headers": {"HTTP-Referer": "https://github.com/herald", "X-Title": "Herald AI Harness"},
            },
            capabilities={"reasoning": "r1" in m.lower(), "code": "sonnet" in m.lower() or "llama" in m.lower(), "fast": "flash" in m.lower()},
            priority=priority,
            enabled=True,
            pool_name=pool_name,
        )
        registered.append(name)

    return {"status": "ok", "provider": "OpenRouter", "registered": registered, "count": len(registered)}


def connect_openai_compatible(
    name: str,
    base_url: str,
    *,
    secret_ref: str | None = None,
    model: str = "default",
    pool_name: str | None = None,
    capabilities: dict[str, Any] | None = None,
    priority: int = 50,
) -> dict[str, Any]:
    """Connect any OpenAI-compatible API endpoint (Groq, Together, DeepSeek, vLLM, LiteLLM)."""
    reg = Registry()
    config = {"base_url": base_url.rstrip("/"), "model": model}
    if secret_ref:
        from herald.router.account_registry import validate_secret_ref
        validate_secret_ref(secret_ref)
        config["secret_ref"] = secret_ref

    reg.register(
        backend_type="api_key" if secret_ref else "local_model",
        name=name,
        config=config,
        capabilities=capabilities or {"fast": True, "tools": True},
        priority=priority,
        enabled=True,
        pool_name=pool_name or "custom-pool",
    )
    return {"status": "ok", "backend": name, "base_url": base_url, "model": model, "pool": pool_name or "custom-pool"}
