"""Herald — unified AI infrastructure library.

The simplest possible entry point. Import herald and call:

    import herald
    herald.chat("do this task")

Herald auto-detects a running router (HERALD_URL env var, or localhost:8790).
If none is running and you call herald.start() first, it starts one in-process.

All router-internal imports are lazy so `import herald` stays fast and works
even in a minimal environment that never starts the Router.

For project-scoped calls with tool isolation:

    herald.project("my-project").part("my-agent").chat("do this")

For full control:

    from herald import Router, Project, Chain, Backend
"""
from __future__ import annotations

import os
import time
import asyncio
from typing import Any

# Lazy imports — don't pull in FastAPI/httpx at import time for projects
# that just want the simple API. Only loaded on first actual call.
_client: "herald.client.RouterClient | None" = None


def _get_client() -> "herald.client.RouterClient":
    global _client
    if _client is None:
        from herald.client import RouterClient
        url = os.environ.get("HERALD_URL", "http://127.0.0.1:8790")
        _client = RouterClient(url)
    if not _client.is_alive():
        if _client.base_url not in {"http://localhost:8790", "http://127.0.0.1:8790"}:
            from herald.errors import RouterUnavailableError
            raise RouterUnavailableError(
                f"Herald router is not reachable at {_client.base_url}; start the remote service or correct HERALD_URL"
            )
        from herald.router import start_server
        start_server(background=True)
        for _ in range(30):
            if _client.is_alive():
                break
            time.sleep(0.2)
        else:
            from herald.errors import RouterUnavailableError
            raise RouterUnavailableError("Herald could not start the local router on port 8790")
    return _client


def connect(url: str) -> None:
    """Point herald at a specific router URL.

    Call this before any other herald function if your router isn't on
    the default localhost:8790.

        herald.connect("http://router.example:8790")
    """
    global _client
    from herald.client import RouterClient
    _client = RouterClient(url)


def start(port: int = 8790, background: bool = True) -> None:
    """Start the herald router.

    If background=True (default), starts as a background subprocess and
    returns immediately. If background=False, blocks (useful for scripts
    where herald IS the server).

        herald.start()           # background, port 8790
        herald.start(port=9000)  # custom port
    """
    from herald.router import start_server
    start_server(port=port, background=background)


def chat(
    prompt: str,
    *,
    context: Any = None,
    model: str | None = None,
    project: str | None = None,
    part: str | None = None,
    session: str | None = None,
    persona: str | None = None,
    **kwargs: Any,
) -> str:
    """Send a prompt and get a reply. The simplest herald call.

    Args:
        prompt:  What to ask / what to do.
        context: Live data, API response, metrics, or document to inject as context.
        model:   Specific backend name. If omitted, router picks best available.
        project: Project scope (optional). If set, part is also required.
        part:    Part scope within project. Limits tools to that part's set.
        session: Name of a persistent, backend-agnostic memory session. Reusing
                 the same name replays recent turns into the prompt on every
                 call, so conversational continuity survives Herald routing a
                 later call to a different backend than the one that answered
                 earlier turns (a subscription CLI hitting quota, a browser
                 session's circuit breaker tripping, etc). Backed by
                 herald.router.agent_sessions.AgentSessionStore -- encrypted,
                 on-disk, and independent of any single backend's own
                 transcript. Omit for one-off, stateless calls.
        persona: Name of a steering persona (see herald.steering) to voice the
                 reply as -- e.g. "herald" for the default conversational,
                 low-jargon identity used by `herald talk` and the mobile app.
                 Omit for the plain default steering.

    Returns:
        The model's text response.
    """
    if context:
        import json
        if isinstance(context, (dict, list)):
            context_str = json.dumps(context, indent=2)
        else:
            context_str = str(context)
        prompt = f"[Context Information]\n{context_str}\n\n[User Instruction]\n{prompt}"

    prompt = run_hooks("pre_chat", prompt, model=model, project=project, part=part, **kwargs)
    client = _get_client()
    if session:
        mode = kwargs.pop("mode", "efficiency")
        agentic = kwargs.pop("agentic", False)
        instructions = kwargs.pop("instructions", "")
        if persona:
            instructions = f"{steering.get(persona)}\n\n{instructions}".strip()
        opened = client.open_agent_session(
            name=session, model=model or "auto", mode=mode,
            instructions=instructions, project=project, part=part, agentic=agentic,
        )
        session_id = (opened.get("session") or {}).get("id")
        if not session_id:
            res = f"[error] failed to open session '{session}': {opened.get('error') or opened}"
        else:
            reply = client.send_agent_message(session_id, prompt)
            res = reply.get("content") if "content" in reply else f"[error] {reply.get('error') or reply}"
    else:
        voiced_prompt = f"[Voice]\n{steering.get(persona)}\n\n[Input]\n{prompt}" if persona else prompt
        if project and part:
            res = client.chat_scoped(
                voiced_prompt, project=project, part=part, model=model, **kwargs,
            )
        else:
            res = client.chat(voiced_prompt, model=model, **kwargs)
    return run_hooks("post_chat", res, model=model, project=project, part=part, **kwargs)




# ``ask`` is the plain-language spelling used in beginner package examples.
ask = chat


async def aask(prompt: str, **kwargs: Any) -> str:
    """Async form of :func:`ask` for web applications and task loops."""
    return await asyncio.to_thread(chat, prompt, **kwargs)


def quick(prompt: str) -> str:
    """Fast, cheap chat — routes to the fastest available backend.

    Use for classification, yes/no questions, short lookups. Not for
    complex reasoning or code generation.
    """
    client = _get_client()
    return client.chat(prompt, model=None, prefer_fast=True)


def code(prompt: str, *, model: str | None = None, **kwargs: Any) -> str:
    """Route a code-focused prompt to the best code model available.

    Prefers Claude CLI or Codex CLI over general-purpose models.
    """
    client = _get_client()
    return client.chat(prompt, model=model, task_type="code", **kwargs)


async def acode(prompt: str, *, model: str | None = None, **kwargs: Any) -> str:
    return await asyncio.to_thread(code, prompt, model=model, **kwargs)


def status() -> dict[str, Any]:
    """Get the router's current health status.

    Returns backend health, auth state, VRAM usage, and quota info.
    """
    client = _get_client()
    return client.status()


def swarm(tasks: list[str], *, model: str | None = None, max_workers: int = 4,
          instructions: str = "", **kwargs: Any) -> list[dict[str, Any]]:
    """Run a general-purpose parallel batch using Herald routing."""
    return _get_client().swarm(tasks, model=model, max_workers=max_workers,
                               instructions=instructions, **kwargs)


def project(name: str) -> "herald.project.Project":
    """Get a Project object for scoped, tool-isolated calls.

        proj = herald.project("college-assistant")
        agent = proj.part("study-agent")
        agent.chat("what assignments are due?")
    """
    from herald.project import Project
    return Project(name, client=_get_client())


def cli_account(name: str) -> "herald.client.CLIAccount":
    """Get a callable handle to a named, router-owned CLI account."""
    return _get_client().cli_account(name)


# Re-export key classes for Layer 3 (full control) usage:
#   from herald import Router, Project, Chain, Backend
from herald.client import RouterClient as Router
from herald.project import Project
from herald.chain import Chain
from herald.harness import Harness
from herald.flow import Flow, FlowSpec
from herald.agent import Decision, StatefulAgent
from herald.loop import Loop, LoopResult, LoopStep, run_loop, until_contains
from herald.errors import (
    CLIAccountInvocationError, DecisionValidationError, HeraldError, InvalidResponseError,
    RouterUnavailableError, SessionBusyError,
)


def open_project(
    root: str = ".", *, part: str | None = None, manifest: str = "router.yaml",
    create: bool = False, name: str | None = None, auto_start: bool = True,
) -> Harness:
    """Open and register a project manifest as a scoped software harness."""
    return Harness.open(
        root, part=part, manifest=manifest, create=create, name=name,
        auto_start=auto_start,
    )


from herald.steering import (
    SteeringRegistry, compose_system_prompt, get_prompt, set_persona, steering,
)
from herald.dev import (
    tool, register_backend, register_persona, hook, list_custom_tools,
    execute_custom_tool, run_hooks,
)
from herald.connectors import (
    connect_ollama, connect_lmstudio, connect_openrouter, connect_openai_compatible,
)
from herald.ingest import fetch, fetch_json
from herald.openapi_tools import tools_from_openapi
from herald.helpers import (
    aclassify, aextract, asummarize, classify, extract, summarize,
    aconfirm, ascore, acompare, aroute, amoderate, aredact, atranslate,
    confirm, score, compare, route, moderate, redact, translate,
)
from herald.client import InvocationResult


def __getattr__(name: str):
    """Lazy attribute resolution for router-internal classes.
    Allows `from herald import Backend` without eagerly loading the router
    module (which may not exist yet during migration)."""
    if name == "Backend":
        try:
            from herald.router.registry import Backend
            return Backend
        except ModuleNotFoundError:
            class Backend:  # type: ignore[no-redef]
                def __init__(self, *a, **kw):
                    raise RuntimeError(
                        "herald.router.registry not found. This usually means "
                        "the herald-ai package is incompletely installed; try "
                        "reinstalling with `pip install --force-reinstall herald-ai`."
                    )
            return Backend
    raise AttributeError(f"module 'herald' has no attribute {name!r}")


__all__ = [
    "connect", "start", "chat", "ask", "aask", "quick", "code", "acode", "status", "swarm", "project", "cli_account",
    "summarize", "asummarize", "classify", "aclassify", "extract", "aextract",
    "confirm", "aconfirm", "score", "ascore", "compare", "acompare",
    "route", "aroute", "moderate", "amoderate", "redact", "aredact",
    "translate", "atranslate",
    "InvocationResult",
    "Router", "Project", "Chain", "Backend", "Harness", "open_project", "Flow", "FlowSpec",
    "Decision", "StatefulAgent", "Loop", "LoopResult", "LoopStep", "run_loop", "until_contains",
    "SteeringRegistry", "steering", "get_prompt", "set_persona", "compose_system_prompt",
    "tool", "register_backend", "register_persona", "hook", "list_custom_tools", "execute_custom_tool",
    "connect_ollama", "connect_lmstudio", "connect_openrouter", "connect_openai_compatible",
    "fetch", "fetch_json", "tools_from_openapi",
    "HeraldError", "RouterUnavailableError", "CLIAccountInvocationError", "InvalidResponseError",
    "DecisionValidationError", "SessionBusyError",
]
