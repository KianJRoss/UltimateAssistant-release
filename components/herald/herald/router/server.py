"""Unified router -- one core execution path over every registered backend
(API-key models, CLI tools, local models on other nodes, browser-session
accounts), exposed through THREE different wire protocols so any existing
client library just points at this without adapting to a new custom shape:

  - OpenAI/ChatGPT-compatible  -- POST /v1/chat/completions, GET /v1/models
                                   (also what LM Studio itself speaks)
  - Anthropic-compatible       -- POST /v1/messages
  - Ollama-compatible          -- POST /api/chat, POST /api/generate, GET /api/tags

All three translate into the same registry lookup -> circuit-breaker check ->
adapter dispatch -> circuit-breaker update core (`_execute`), so a new
backend type or a routing-policy change only has to happen in one place.
"""
from __future__ import annotations

from herald.logging_config import configure_logging

configure_logging()

# `herald setup` writes chosen provider keys, HERALD_URL, and budget/update
# preferences to storage_paths.data_dir()/".env" -- load them here, before
# env_check's report below or any provider registry reads os.environ, so
# that file is actually the load-bearing config it claims to be. Confirmed
# live: nothing anywhere loaded this specific path before this change, so
# every setting `herald setup` collected was silently discarded on the next
# run. override=False: explicit environment variables set by the caller
# still win over this file, matching normal precedence expectations.
from dotenv import load_dotenv as _load_dotenv
from herald.router.storage_paths import data_dir as _data_dir

_load_dotenv(_data_dir() / ".env", override=False)

import asyncio
import base64
import ipaddress
import time
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import uuid
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from herald.router import cli_auth
from herald.router import cli_usage
from herald.router import discovery
from herald.router import node_control
from herald.router import telemetry
from herald.router import quota_tracker
from herald.router import quota_router
from herald.router import mobile_terminal
from herald.router import reload_control
from herald.router import voice as voice_io


from herald.router.adapters import ADAPTERS
from herald.router.harness_agent import (
    ExecutionBudget, HarnessLimits, parse_actions, run_harness_agent,
)
from herald.router.registry import Registry
from herald.router.account_registry import AccountRegistry, import_backends
from herald.router.tool_registry import ToolRegistry
from herald.router.bootstrap import bootstrap_registry
from herald.router.tool_executor import discover_tools, execute_tool
from herald.router.integration_discovery import (
    discover_cli_integrations, import_cli_integration, preview_cli_integration,
)
from herald.router.g4f_capture import G4FCaptureManager, find_accounts_file
from herald.router.flow_runs import FlowRunStore
from herald.router.agent_sessions import AgentSessionStore
from herald.router.agent_runs import AgentRunStore
from herald.router.events import EventBus
from herald.router.secret_vault import SecretVault
from herald.router.staging_zone import StagingStore
from herald.governor import governor, GOVERNED_RUNTIMES
from herald.flow import AgentSpec, FlowRunner, FlowSpec
from herald.routing_policy import (
    POLICIES, automatic_model, compact_tools, eligible_backends, get_policy, model_catalog,
)


from herald.router.env_check import check_required_env, hydrate_provider_keys_from_keyring

hydrate_provider_keys_from_keyring()
check_required_env()

app = FastAPI(title="Herald Router")

_DEFAULT_MAX_VOICE_UPLOAD_BYTES = 25 * 1024 * 1024
_VOICE_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _max_voice_upload_bytes() -> int:
    raw = os.environ.get("HERALD_MAX_VOICE_UPLOAD_BYTES", "").strip()
    if not raw:
        return _DEFAULT_MAX_VOICE_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("HERALD_MAX_VOICE_UPLOAD_BYTES must be an integer") from exc
    if value < 1:
        raise RuntimeError("HERALD_MAX_VOICE_UPLOAD_BYTES must be positive")
    return value


@app.middleware("http")
async def reject_foreign_origin(request: Request, call_next):
    """Reject any request whose Origin header doesn't match this router's own address.

    Closes the loopback gap that binding-based auth alone leaves open: a
    malicious page loaded from any other website, running in a browser on
    this same machine, can otherwise call http://127.0.0.1:8790 directly --
    binding to loopback only proves the caller is on this machine, not that
    it's actually Herald's own UI. Browsers always attach Origin on
    cross-origin fetch/XHR and JS cannot forge or suppress it, so this check
    can't be spoofed by page script. Non-browser callers (the CLI, curl,
    scripts) don't send Origin at all and are unaffected.
    """
    origin = request.headers.get("origin")
    if origin and request.url.path != "/devices/accept-grant":
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin.rstrip("/").lower() != expected_origin.rstrip("/").lower():
            response = JSONResponse(
                {"detail": "request Origin does not match this Herald router"},
                status_code=403,
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            return response
    return await call_next(request)


@app.middleware("http")
async def require_public_bind_token(request: Request, call_next):
    """Require a shared bearer token whenever the process is publicly bound.

    The `server_host` heuristic below only sees the LOCAL end of each
    accepted connection. That works for a directly-exposed `--host 0.0.0.0`
    bind, but it is blind to a reverse proxy in front of this process
    (Tailscale serve/funnel, Cloudflare Tunnel, nginx, ...): those all
    connect to the app over loopback as their last hop, so every request --
    including ones that arrived over the public internet through the proxy
    -- looks like a loopback-only connection here, and auth was silently
    never enforced (confirmed live via a Tailscale Funnel: unauthenticated
    requests got 200s). HERALD_FORCE_AUTH=1 is the explicit escape hatch for
    exactly that case: set it on any router process you know sits behind a
    reverse proxy, and this stops trusting the heuristic entirely.
    """
    server_host = str((request.scope.get("server") or ("", 0))[0])
    try:
        non_loopback = not ipaddress.ip_address(server_host).is_loopback
    except ValueError:
        non_loopback = server_host not in {"", "localhost", "testserver"}
    public_bind = (
        os.environ.get("HERALD_BIND_HOST") == "0.0.0.0"
        or os.environ.get("HERALD_FORCE_AUTH") == "1"
        or non_loopback
    )
    # /devices/accept-grant is authenticated by its own Ed25519 signature check
    # against a TOFU-pinned public key (see the handler) -- no bearer token
    # exists yet at the moment this endpoint is called, by design.
    if public_bind and request.url.path != "/devices/accept-grant":
        expected = os.environ.get("HERALD_API_KEY")
        supplied = request.headers.get("x-api-key")
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        authorized = bool(supplied) and (
            (expected and secrets.compare_digest(supplied, expected))
            or _mesh_trust().authenticate_token(supplied) is not None
        )
        if not authorized:
            response = JSONResponse(
                {"detail": "valid HERALD_API_KEY or paired-device bearer token required"},
                status_code=401,
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=(self)",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; media-src 'self' blob:; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    )
    return response
registry = Registry()
tool_registry = ToolRegistry()
bootstrap_registry(registry)
account_registry = AccountRegistry()
account_registry.remove_account("qwen-cli")
import_backends(account_registry, registry)
STATIC_DIR = Path(__file__).resolve().parent / "static"
_staging_store: StagingStore | None = None
_mesh_trust_instance: "MeshTrust | None" = None


def _staging() -> StagingStore:
    global _staging_store
    if _staging_store is None:
        _staging_store = StagingStore()
    return _staging_store


def _mesh_trust() -> "MeshTrust":
    global _mesh_trust_instance
    if _mesh_trust_instance is None:
        from herald.router.mesh_trust import MeshTrust
        _mesh_trust_instance = MeshTrust()
    return _mesh_trust_instance


@app.on_event("startup")
async def _start_event_bus() -> None:
    from herald.router import event_bus, quota_watcher, scheduler, idle_unload, mesh_discovery
    from herald.router.notifications import discord_sink
    event_bus.start()
    quota_watcher.start()
    event_bus.register_sink(discord_sink, event_types=None, min_importance=0.3)
    scheduler.start()
    idle_unload.start()
    if os.environ.get("HERALD_DISABLE_MESH_DISCOVERY") != "1":
        port = int(os.environ.get("HERALD_PORT", "8790"))
        mesh_discovery.start(port)


@app.on_event("shutdown")
async def _stop_event_bus() -> None:
    from herald.router import event_bus, quota_watcher, scheduler, idle_unload, mesh_discovery
    await mesh_discovery.stop()
    await idle_unload.stop()
    await scheduler.stop()
    await quota_watcher.stop()
    await event_bus.stop()


@app.get("/static/herald-ui.css")
def herald_ui_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "herald-ui.css", media_type="text/css")


@app.get("/devices/self")
def devices_self() -> dict[str, Any]:
    return {"device": _mesh_trust().self_identity().to_dict()}


@app.get("/devices/pending")
def devices_pending() -> dict[str, Any]:
    return {"devices": [d.to_dict() for d in _mesh_trust().pending()]}


@app.get("/devices")
def devices_trusted() -> dict[str, Any]:
    return {"devices": [d.to_dict() for d in _mesh_trust().trusted()]}


@app.post("/devices/{node_id}/revoke")
def devices_revoke(node_id: str) -> dict[str, Any]:
    trust = _mesh_trust()
    try:
        device = trust.revoke(node_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"device": device.to_dict()}


class DeviceApproveRequest(BaseModel):
    code: str


@app.post("/devices/approve")
async def devices_approve(payload: DeviceApproveRequest) -> dict[str, Any]:
    """Approve a pending device by its short code, then push it a bearer token.

    The push is a direct call to the device's own /devices/accept-grant, signed
    with this node's private key so the target can verify -- via the public key
    it already recorded during mDNS discovery -- that the grant really came from
    the node it saw, not an impersonator. This node trusting the device is a
    local decision; delivering the token lets the device actually use that trust.
    """
    trust = _mesh_trust()
    pending = trust.find_by_code(payload.code)
    if pending is None:
        raise HTTPException(404, f"no pending device with code '{payload.code}'")
    approved = trust.approve(pending.node_id)
    self_id = trust.self_identity()

    push_error = None
    if approved.address and approved.port:
        message = f"{self_id.node_id}:{approved.bearer_token}".encode()
        signature = trust.sign(message)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"http://{approved.address}:{approved.port}/devices/accept-grant",
                    json={
                        "approver_node_id": self_id.node_id,
                        "approver_public_key_pem": self_id.public_key_pem,
                        "bearer_token": approved.bearer_token,
                        "signature": base64.b64encode(signature).decode(),
                    },
                )
                if response.status_code >= 400:
                    push_error = f"device rejected the grant: HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            push_error = f"could not reach device to deliver the grant: {exc}"

    result = {"device": approved.to_dict(reveal_token=True)}
    if push_error:
        result["push_warning"] = push_error
    return result


class DeviceAcceptGrantRequest(BaseModel):
    approver_node_id: str
    approver_public_key_pem: str
    bearer_token: str
    signature: str


@app.post("/devices/accept-grant")
async def devices_accept_grant(payload: DeviceAcceptGrantRequest) -> dict[str, Any]:
    """Receive a signed trust grant pushed by an approving node.

    Deliberately exempt from the shared bearer-token middleware (see
    require_public_bind_token) since no token exists for this exchange yet --
    it is authenticated instead by an Ed25519 signature checked against the
    public key this node already recorded for the approver during mDNS
    discovery (TOFU pinning), so a request claiming a different key than what
    was actually observed on the network is rejected outright.
    """
    trust = _mesh_trust()
    known = trust.get(payload.approver_node_id)
    if known is None or known.public_key_pem != payload.approver_public_key_pem:
        raise HTTPException(403, "approver identity does not match what was seen via discovery")
    try:
        signature = base64.b64decode(payload.signature)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "malformed signature") from exc
    message = f"{payload.approver_node_id}:{payload.bearer_token}".encode()
    from herald.router.mesh_trust import MeshTrust
    if not MeshTrust.verify(payload.approver_public_key_pem, message, signature):
        raise HTTPException(403, "signature does not match the approver's known public key")
    trust.accept_remote_grant(
        approver_node_id=payload.approver_node_id,
        approver_public_key_pem=payload.approver_public_key_pem,
        my_bearer_token=payload.bearer_token,
    )
    return {"accepted": True}


@app.get("/ui")
def ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/ui/index.js")
def ui_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.js", media_type="application/javascript")


@app.get("/mobile")
def mobile_ui() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "mobile.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/mobile/terminal")
def mobile_terminal_ui() -> FileResponse:
    """Advanced, TTY-backed client for native CLI-only interactions."""
    return FileResponse(
        STATIC_DIR / "mobile_terminal.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


class MobileInputRequest(BaseModel):
    text: str
    submit: bool = True


class MobileKeyRequest(BaseModel):
    key: str


def _mobile_call(operation):
    try:
        return operation()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/mobile/api/sessions")
def mobile_sessions() -> dict[str, Any]:
    return {"sessions": mobile_terminal.list_sessions()}


@app.post("/mobile/api/sessions/{session_id}/start")
def mobile_session_start(session_id: str) -> dict[str, Any]:
    return _mobile_call(lambda: mobile_terminal.ensure_session(session_id))


@app.get("/mobile/api/sessions/{session_id}/screen")
def mobile_session_screen(session_id: str, lines: int = 240) -> dict[str, Any]:
    return _mobile_call(lambda: mobile_terminal.capture_screen(session_id, lines=lines))


@app.post("/mobile/api/sessions/{session_id}/input")
def mobile_session_input(session_id: str, request: MobileInputRequest) -> dict[str, Any]:
    return _mobile_call(lambda: mobile_terminal.send_text(session_id, request.text, submit=request.submit))


@app.post("/mobile/api/sessions/{session_id}/key")
def mobile_session_key(session_id: str, request: MobileKeyRequest) -> dict[str, Any]:
    return _mobile_call(lambda: mobile_terminal.send_key(session_id, request.key))


class ExecutionError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


_run_progress: ContextVar[Any] = ContextVar("herald_run_progress", default=None)


def _emit_run_progress(kind: str, message: str, **data: Any) -> None:
    callback = _run_progress.get()
    if callback:
        callback(kind, message, data)


def _attempt_backend(backend, prompt: str, *, branch_id: str | None, depth: int | None) -> dict[str, Any]:
    """Dispatch to one backend row's adapter and record the outcome.

    For local_model backends the governor serializes calls through a
    per-runtime semaphore — a second call to lmstudio/ollama waits in
    queue rather than racing and corrupting the GPU context.
    """
    adapter = ADAPTERS.get(backend.backend_type)
    if adapter is None:
        raise ExecutionError(500, f"no adapter for backend_type '{backend.backend_type}'")

    # --- Universal Governor: serialize calls per GPU context, G4F session, or CLI profile ---
    governed = False
    runtime = None
    if backend.backend_type == "local_model":
        runtime = backend.config.get("runtime", "local_model")
        governed = True
    elif backend.backend_type == "browser_session":
        runtime = f"browser_session:{backend.name}"
        governed = True
    elif backend.backend_type == "cli":
        cli_name = backend.config.get("cli_name", backend.name)
        runtime = f"cli:{cli_name}"
        governed = True

    if governed and runtime:
        # Default raised from 120s: a legitimately busy GPU/CLI/browser slot
        # (e.g. another call mid-build) can reasonably queue longer than that
        # on a solo-user setup with one backend per runtime. Falls through to
        # the next candidate on timeout either way (see call site below), so
        # raising this doesn't reduce the fallback-loop's own responsiveness.
        governor_timeout = float(backend.config.get("governor_timeout") or 600.0)
        acquired = governor.acquire(runtime, backend.name, timeout=governor_timeout)
        if not acquired:
            return {
                "ok": False,
                "error": f"Backend '{backend.name}' ({runtime}) timed out waiting for execution slot — another call is holding the session",
            }

    started = time.monotonic()
    _emit_run_progress(
        "backend_started", f"Trying {backend.name}",
        backend=backend.name, backend_type=backend.backend_type,
        branch_id=branch_id, depth=depth,
    )
    try:
        try:
            result = adapter(backend.config, prompt)
        except Exception as exc:  # noqa: BLE001 - an adapter bug must not abort the whole fallback loop
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if governed and runtime:
            governor.release(runtime, backend.name)


    duration_ms = int((time.monotonic() - started) * 1000)

    if not result.get("ok"):
        _emit_run_progress(
            "backend_failed", f"{backend.name} failed; Herald will try another route.",
            backend=backend.name, duration_ms=duration_ms,
            error=str(result.get("error") or "unknown failure")[:1000],
        )
        registry.record_failure(backend.name)
        telemetry.log_call(
            backend_name=backend.name, backend_type=backend.backend_type, prompt=prompt,
            success=False, duration_ms=duration_ms, error=result.get("error"),
            branch_id=branch_id, depth=depth,
        )
        return result

    registry.record_success(backend.name)
    _emit_run_progress(
        "backend_succeeded", f"{backend.name} responded successfully.",
        backend=backend.name, duration_ms=duration_ms,
    )
    result["backend"] = backend.name
    result["backend_type"] = backend.backend_type
    usage = result.get("usage") or {}
    telemetry.log_call(
        backend_name=backend.name, backend_type=backend.backend_type, prompt=prompt,
        success=True, duration_ms=duration_ms, content=result["content"],
        thinking=result.get("thinking"), thinking_tokens=result.get("thinking_tokens"),
        input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
        cost_usd=usage.get("cost_usd"), branch_id=branch_id, depth=depth,
    )
    return result


def _execute_full(model_name: str, prompt: str, *, branch_id: str | None = None, depth: int | None = None,
                  allow_fallback: bool = True) -> dict[str, Any]:
    """Execute model candidate with fallback."""
    pinned_backend = None
    if model_name in POLICIES or model_name == "auto":
        policy = get_policy(model_name if model_name in POLICIES else None)
        raw_candidates = eligible_backends(registry.list_all(), policy)
        if not raw_candidates:
            raise ExecutionError(503, f"no healthy backend satisfies the '{policy.name}' routing policy")
        candidates = quota_router.rank_candidates(raw_candidates, policy=policy, prompt=prompt)
    else:
        # registry.get() only ever matches a backend's own literal `name`,
        # never a pool_name or capability preset -- even one that currently
        # resolves to a single member via list_pool() below. That makes it
        # the exact, unambiguous signal for "the caller pinned one specific
        # backend" versus "the caller named a group (pool/preset/policy)".
        # A pin means exactly that backend, never a substitute: skip the
        # mesh-wide failover below entirely, regardless of the caller's
        # allow_fallback default, and don't expand into list_pool()'s
        # pool_name-sharing siblings either.
        pinned_backend = registry.get(model_name)
        if pinned_backend is not None:
            raw_candidates = [pinned_backend]
            candidates = raw_candidates
            allow_fallback = False
        else:
            raw_candidates = registry.list_pool(model_name)
            if not raw_candidates:
                raise ExecutionError(404, f"no backend registered as '{model_name}'")
            candidates = quota_router.rank_candidates(raw_candidates)



    # Herald is already watching everything a CLI-backed candidate streams
    # (that's the whole "window" design) -- when one fails partway through
    # real work (quota ran out mid-task, not before it started), carry that
    # real progress forward into whichever candidate is tried next instead
    # of starting the fallback cold with only the original prompt. Updated
    # after every failed attempt, primary pool or mesh-wide, so a chain of
    # several fallbacks each builds on the last one's real progress.
    working_prompt = prompt
    last_failed_backend: str | None = None
    last_partial_output = ""

    def _carry_forward(result: dict[str, Any], backend_name: str) -> None:
        nonlocal working_prompt, last_failed_backend, last_partial_output
        partial = (result.get("partial_content") or "").strip()
        if not partial:
            return
        working_prompt = (
            f"{working_prompt}\n\n[Continuity from {backend_name}, which was doing this "
            "same task but hit a failure (quota, timeout, or disconnect) before finishing -- "
            "this is its own real output up to that point, not a summary. Continue from here "
            "instead of restarting from scratch; don't repeat work already reflected below.]\n"
            f"{partial[:4000]}"
        )
        last_failed_backend = backend_name
        last_partial_output = partial

    attempted = 0
    failures: list[str] = []
    for backend in candidates:
        if not backend.enabled or backend.circuit_open:
            _emit_run_progress(
                "backend_skipped", f"Skipped {backend.name} because it is unavailable.",
                backend=backend.name,
                reason="disabled" if not backend.enabled else "circuit_open",
            )
            continue
        attempted += 1
        result = _attempt_backend(backend, working_prompt, branch_id=branch_id, depth=depth)
        if result.get("ok"):
            return result
        failures.append(f"{backend.name}: {result.get('error')}")
        _carry_forward(result, backend.name)

    # --- Mesh-wide failover: if the requested pool/model failed, cascade to any healthy backend ---
    fallback_policy = get_policy("balanced")
    fallback_candidates = eligible_backends(registry.list_all(), fallback_policy)
    fallback_candidates = [b for b in fallback_candidates if b.pool_name != model_name and b.name != model_name]
    if allow_fallback and fallback_candidates:
        fallback_ranked = quota_router.rank_candidates(fallback_candidates, policy=fallback_policy, prompt=prompt)
        for fb in fallback_ranked:
            if not fb.enabled or fb.circuit_open:
                continue
            note = f" (carrying forward {last_failed_backend}'s partial progress)" if last_failed_backend else ""
            _emit_run_progress("pool_fallback", f"'{model_name}' failed; failing over to {fb.name}{note}", backend=fb.name)
            result = _attempt_backend(fb, working_prompt, branch_id=branch_id, depth=depth)
            if result.get("ok"):
                return result
            _carry_forward(result, fb.name)


    partial_notice = (
        f"\n\nUnverified partial output from {last_failed_backend}:\n{last_partial_output[:4000]}"
        if last_partial_output else ""
    )
    if pinned_backend is not None:
        if attempted == 0:
            raise ExecutionError(
                503, f"'{model_name}' is disabled or has an open circuit (pinned, no fallback); retry after cooldown",
            )
        raise ExecutionError(502, f"'{model_name}' failed (pinned, no fallback): " + "; ".join(failures) + partial_notice)
    if attempted == 0:
        raise ExecutionError(
            503,
            f"all {len(candidates)} candidate(s) for '{model_name}' are disabled or have an open circuit; retry after cooldown",
        )
    raise ExecutionError(502, f"all {attempted} candidate(s) for '{model_name}' failed: " + "; ".join(failures) + partial_notice)



def _execute(model_name: str, prompt: str, *, branch_id: str | None = None, depth: int | None = None) -> str:
    """Text-only convenience wrapper around _execute_full, for callers (the
    agentic loop, other protocol surfaces) that just need the answer."""
    return _execute_full(model_name, prompt, branch_id=branch_id, depth=depth)["content"]


def _execute_tolerant(model_name: str, prompt: str, *, branch_id: str | None = None, depth: int | None = None) -> str:
    """Same as _execute, but for use inside the agentic loop -- a nested
    consultation failing should surface as text the orchestrating model can
    react to, not blow up the whole chain."""
    try:
        return _execute(model_name, prompt, branch_id=branch_id, depth=depth)
    except ExecutionError as exc:
        return f"[consultation with {model_name} failed: {exc.detail}]"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _prompt_from_openai_messages(messages: list[dict[str, Any]]) -> str:
    """Preserve harness tool calls/results while flattening for CLI backends."""
    rendered = []
    for message in messages:
        role = message.get("role", "user")
        text = _message_text(message.get("content"))
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            call_lines = []
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name")
                args = function.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                if name:
                    call_lines.append(f'TOOL_CALL: {json.dumps({"name": name, "arguments": args if isinstance(args, dict) else {}}, separators=(",", ":"))}')
            if call_lines:
                text = (text + "\n" if text else "") + "\n".join(call_lines)
        if role == "tool":
            text = f"Tool result for {message.get('tool_call_id')}: {text}"
        rendered.append(f"{role}: {text}")
    return "\n\n".join(rendered)


# ---------------------------------------------------------------------------
# OpenAI/ChatGPT-compatible surface (also what LM Studio speaks natively)
# ---------------------------------------------------------------------------

class OpenAIChatRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    agentic: bool = False
    project: str | None = None
    part: str | None = None
    max_tool_iterations: int = 6
    max_depth: int = 3
    max_parallel: int = 4
    max_model_calls: int = 16
    max_tool_calls: int = 24
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    mode: str = "balanced"
    force_harness: bool = False
    consult_depth: int = 0
    cli_account: str | None = None


def _external_tool_catalog(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    catalog = []
    for tool in tools or []:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        catalog.append({
            "name": function["name"], "description": function.get("description", ""),
            "parameters": function.get("parameters") or {},
        })
    return catalog


def _external_tool_prompt(prompt: str, catalog: list[dict[str, Any]]) -> str:
    return (
        f"{prompt}\n\n[External coding-harness tools]\n"
        f"{json.dumps(catalog, separators=(',', ':'))}\n"
        "If tools are required, you may emit standard JSON lines or clean action blocks:\n"
        'TOOL_CALL: {"name":"exact-name","arguments":{"key":"value"}}\n'
        "Or natural action lines:\n"
        "RUN: <command>\n"
        "READ: <file_path>\n"
        "WRITE_FILE: <path>\n```\n<content>\n```\n"
        "EDIT_FILE: <path>\n<<<<<<< SEARCH\n<target>\n=======\n<replacement>\n>>>>>>>\n"
        "SEARCH: <query>\n"
        "The harness will execute them and return results in the next message. "
        "Otherwise answer normally. Never invent dummy placeholder paths like /path/to/..."
    )


def _extract_tools_with_bridge(content: str, catalog: list[dict[str, Any]], bridge_backend: str | None = None) -> list[dict[str, Any]]:
    """Extract tool calls from model reasoning using direct parsing or a fast bridge model."""
    if not catalog or not content.strip():
        return []
    allowed = {tool["name"] for tool in catalog}
    actions = parse_actions(content, allowed)
    if actions:
        return actions

    # Try fast bridge model if available in registry
    bridge_model = bridge_backend or "fast"
    if not registry.list_pool(bridge_model) and not registry.get(bridge_model):
        return []

    bridge_prompt = (
        "You are a strict deterministic tool call compiler.\n"
        "Given the reasoning and plan from the primary AI model, identify any exact tools to call.\n"
        f"Available tools catalog:\n{json.dumps(catalog, separators=(',', ':'))}\n\n"
        f"Primary model response:\n{content}\n\n"
        "Output ONLY valid tool calls using lines starting with:\n"
        'TOOL_CALL: {"name":"tool-name","arguments":{"key":"value"}}\n'
        "Or natural action lines: RUN: <cmd>, READ: <path>, WRITE_FILE: <path>\n```\n<content>\n```\n"
        "If no tool should be called, reply ONLY with: NO_TOOLS"
    )
    try:
        bridge_res = _execute_full(bridge_model, bridge_prompt)
        bridge_content = bridge_res.get("content", "")
        if "NO_TOOLS" in bridge_content:
            return []
        return parse_actions(bridge_content, allowed)
    except Exception:
        return []


def _openai_response_payload(
    model: str, content: str, catalog: list[dict[str, Any]], bridge_backend: str | None = None,
) -> dict[str, Any]:
    response_id = f"chatcmpl-herald-{uuid.uuid4().hex[:16]}"
    allowed = {tool["name"] for tool in catalog}
    calls = []
    actions = _extract_tools_with_bridge(content, catalog, bridge_backend=bridge_backend)
    for action in actions:
        if action.get("kind") != "tool" or action.get("name") not in allowed:
            continue
        arguments = action.get("arguments", {})
        if not isinstance(arguments, dict):
            continue
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:20]}", "type": "function",
            "function": {
                "name": action["name"],
                "arguments": json.dumps(arguments, separators=(",", ":")),
            },
        })
    message: dict[str, Any] = {"role": "assistant", "content": content.strip()}
    finish_reason = "stop"
    if calls:
        # Preserve text explanation alongside tool_calls so the TUI displays thoughts and progress
        message = {"role": "assistant", "content": content.strip() or None, "tool_calls": calls}
        finish_reason = "tool_calls"
    return {
        "id": response_id, "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


def _stream_openai_payload(payload: dict[str, Any]):
    choice = payload["choices"][0]
    message = choice["message"]
    base = {
        "id": payload["id"], "object": "chat.completion.chunk",
        "created": payload["created"], "model": payload["model"],
    }
    first = {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    yield f"data: {json.dumps(first)}\n\n"
    if message.get("content"):
        content = message["content"]
        # Stream in progressive word/line chunks for immediate UI rendering
        lines = content.splitlines(keepends=True)
        for line in lines:
            words = line.split(" ")
            for i, word in enumerate(words):
                piece = word + (" " if i < len(words) - 1 else "")
                chunk = {
                    **base,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
    if message.get("tool_calls"):
        for index, call in enumerate(message["tool_calls"]):
            chunk = {
                **base,
                "choices": [{
                    "index": 0,
                    "delta": {"tool_calls": [{
                        "index": index, "id": call["id"], "type": "function",
                        "function": call["function"],
                    }]},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
    final = {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


def _mask_config(config: dict[str, Any]) -> dict[str, Any]:
    """Redact secret-looking values on the way OUT over HTTP -- never used on
    the internal path where adapters actually need the real key. A config
    key whose name contains "key"/"token"/"secret" (case-insensitive) is
    replaced with its last 4 characters, so the dashboard can show which
    credential is in use without ever round-tripping the plaintext value."""
    def mask(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {nested_key: mask(nested_value, nested_key) for nested_key, nested_value in value.items()}
        if isinstance(value, list):
            return [mask(item, key) for item in value]
        if isinstance(value, str) and any(s in key.lower() for s in ("key", "token", "secret", "password")):
            return f"***{value[-4:]}" if len(value) > 4 else "***"
        return value

    return mask(config)


def _backend_to_dict(backend) -> dict[str, Any]:
    return {
        "name": backend.name, "backend_type": backend.backend_type,
        "config": _mask_config(backend.config), "capabilities": backend.capabilities,
        "cost_per_1k_tokens": backend.cost_per_1k_tokens, "priority": backend.priority,
        "enabled": backend.enabled, "pool_name": backend.pool_name,
        "circuit_open": backend.circuit_open, "consecutive_failures": backend.consecutive_failures,
    }


def _account_to_dict(account) -> dict[str, Any]:
    return {
        "id": account.id, "name": account.name, "provider": account.provider,
        "auth_kind": account.auth_kind, "config": _mask_config(account.config),
        "secret_ref": account.secret_ref, "enabled": account.enabled,
        "priority": account.priority, "tags": account.tags,
        "created_at": account.created_at, "updated_at": account.updated_at,
    }


def _lane_to_dict(lane) -> dict[str, Any]:
    return {
        "id": lane.id, "account_id": lane.account_id, "account": lane.account_name,
        "name": lane.name, "backend_name": lane.backend_name, "model": lane.model,
        "capabilities": lane.capabilities, "priority": lane.priority,
        "enabled": lane.enabled, "created_at": lane.created_at,
        "updated_at": lane.updated_at,
    }


def _resolve_cli_account_lane(name: str):
    """Resolve one CLI identity without allowing a pool/account substitution."""
    account = account_registry.get_account(name)
    if account is None:
        raise HTTPException(404, f"CLI account '{name}' was not found")
    if not account.enabled:
        raise HTTPException(400, f"CLI account '{name}' is disabled")
    if account.auth_kind != "cli_profile":
        raise HTTPException(400, f"account '{name}' is not a CLI profile")
    lane = account_registry.select_enabled_lane(name)
    if lane is None:
        raise HTTPException(400, f"CLI account '{name}' has no enabled model lane")
    backend = registry.get(lane.backend_name)
    if backend is None or backend.backend_type != "cli" or not backend.enabled:
        raise HTTPException(409, f"CLI account '{name}' is inactive; activate it before use")
    return lane, backend


def _execute_cli_account(name: str, prompt: str) -> tuple[Any, dict[str, Any]]:
    lane, backend = _resolve_cli_account_lane(name)
    if backend.circuit_open:
        raise HTTPException(503, f"CLI account '{name}' is temporarily unavailable")
    result = _attempt_backend(backend, prompt, branch_id=None, depth=None)
    if not result.get("ok"):
        from herald.router.sanitization import sanitize_error
        detail = sanitize_error(result.get("error") or "CLI transport failure")
        raise HTTPException(502, f"CLI account '{name}' failed: {detail}")
    return lane, result


class BackendCreateRequest(BaseModel):
    name: str
    backend_type: str
    config: dict[str, Any]
    capabilities: dict[str, Any] | None = None
    cost_per_1k_tokens: float | None = None
    priority: int = 100
    enabled: bool = True
    pool_name: str | None = None


class AccountCreateRequest(BaseModel):
    name: str
    provider: str
    auth_kind: str
    config: dict[str, Any] = {}
    secret_ref: str | None = None
    enabled: bool = True
    priority: int = 100
    tags: list[str] = []


class AccountLaneCreateRequest(BaseModel):
    name: str
    backend_name: str
    model: str = ""
    capabilities: dict[str, Any] = {}
    priority: int = 100
    enabled: bool = True


class AccountTestRequest(BaseModel):
    prompt: str = "Reply with exactly: Herald connection OK"


@app.get("/backends")
def list_backends() -> dict[str, Any]:
    return {"backends": [_backend_to_dict(b) for b in registry.list_all()]}


@app.post("/backends")
def create_backend(request: BackendCreateRequest) -> dict[str, Any]:
    """Upserts by name (see Registry.register) -- also how you add another
    key to an existing pool: register a new row with the same pool_name."""
    try:
        registry.register(
            backend_type=request.backend_type, name=request.name, config=request.config,
            capabilities=request.capabilities, cost_per_1k_tokens=request.cost_per_1k_tokens,
            priority=request.priority, enabled=request.enabled, pool_name=request.pool_name,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return _backend_to_dict(registry.get(request.name))


@app.delete("/backends/{name}")
def delete_backend(name: str) -> dict[str, Any]:
    if registry.get(name) is None:
        raise HTTPException(404, f"no backend registered as '{name}'")
    registry.remove(name)
    return {"removed": name}


@app.get("/accounts")
def list_accounts(provider: str | None = None, enabled_only: bool = False) -> dict[str, Any]:
    return {
        "accounts": [
            _account_to_dict(account)
            for account in account_registry.list_accounts(
                provider=provider, enabled_only=enabled_only,
            )
        ]
    }


@app.post("/accounts")
def create_account(request: AccountCreateRequest) -> dict[str, Any]:
    try:
        account_registry.register_account(
            name=request.name, provider=request.provider, auth_kind=request.auth_kind,
            config=request.config, secret_ref=request.secret_ref, enabled=request.enabled,
            priority=request.priority, tags=request.tags,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return _account_to_dict(account_registry.get_account(request.name))


@app.delete("/accounts/{name}")
def delete_account(name: str) -> dict[str, Any]:
    if not account_registry.remove_account(name):
        raise HTTPException(404, f"account '{name}' not found")
    return {"removed": name}


@app.get("/accounts/{name}/lanes")
def list_account_lanes(name: str) -> dict[str, Any]:
    if account_registry.get_account(name) is None:
        raise HTTPException(404, f"account '{name}' not found")
    return {"lanes": [_lane_to_dict(lane) for lane in account_registry.list_lanes(account=name)]}


@app.post("/accounts/{name}/lanes")
def create_account_lane(name: str, request: AccountLaneCreateRequest) -> dict[str, Any]:
    if not registry.list_pool(request.backend_name):
        raise HTTPException(400, f"router backend or pool '{request.backend_name}' not found")
    try:
        lane_id = account_registry.register_lane(
            account=name, name=request.name, backend_name=request.backend_name,
            model=request.model, capabilities=request.capabilities,
            priority=request.priority, enabled=request.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    lane = next(lane for lane in account_registry.list_lanes(account=name) if lane.id == lane_id)
    return _lane_to_dict(lane)


@app.post("/accounts/{name}/activate")
def activate_account(name: str) -> dict[str, Any]:
    """Materialize configured account metadata into callable router backends."""
    account = account_registry.get_account(name)
    if account is None:
        raise HTTPException(404, f"account '{name}' not found")
    if account.auth_kind == "cli_profile":
        lanes = account_registry.list_lanes(account=name)
        if not lanes:
            raise HTTPException(400, f"CLI account '{name}' has no enabled model lane")
        for lane in lanes:
            lane_config = dict(account.config)
            if lane.model:
                lane_config.setdefault("model", lane.model)
            registry.register(
                backend_type="cli", name=lane.backend_name, config=lane_config,
                capabilities=lane.capabilities, priority=lane.priority,
                enabled=account.enabled and lane.enabled,
            )
        return {
            "account": _account_to_dict(account),
            "lanes": [_lane_to_dict(lane) for lane in lanes],
            "active": True,
        }
    if account.auth_kind != "api_key":
        raise HTTPException(400, "only CLI-profile and api_key accounts can be activated")
    if not account.secret_ref:
        raise HTTPException(400, "api_key account requires a secret_ref")
    model_name = account.config.get("model_name") or account.config.get("model")
    if not model_name:
        raise HTTPException(400, "api_key account config requires model_name")
    metadata = {"capabilities", "pool_name", "cost_per_1k_tokens", "model", "priority"}
    backend_config = {
        key: value for key, value in account.config.items() if key not in metadata
    }
    backend_config.update({
        "provider": backend_config.get("provider", account.provider),
        "model_name": model_name, "secret_ref": account.secret_ref,
    })
    registry.register(
        backend_type="api_key", name=account.name, config=backend_config,
        capabilities=account.config.get("capabilities") or {},
        cost_per_1k_tokens=account.config.get("cost_per_1k_tokens"),
        priority=account.priority, enabled=account.enabled,
        pool_name=account.config.get("pool_name"),
    )
    lanes = account_registry.list_lanes(account=account.name)
    if not lanes:
        account_registry.register_lane(
            account=account.name, name="default", backend_name=account.name,
            model=str(model_name), capabilities=account.config.get("capabilities") or {},
            priority=account.priority, enabled=account.enabled,
        )
        lanes = account_registry.list_lanes(account=account.name)
    return {
        "account": _account_to_dict(account),
        "backend": _backend_to_dict(registry.get(account.name)),
        "lanes": [_lane_to_dict(lane) for lane in lanes],
    }


@app.post("/accounts/{name}/test")
def test_account(name: str, request: AccountTestRequest) -> dict[str, Any]:
    """Make one direct diagnostic call through a named account lane.

    This deliberately bypasses automatic routing, tools, and delegation: the
    administration UI uses it to verify one credential/profile, not as a chat
    or coding environment.
    """
    account = account_registry.get_account(name)
    if account is None:
        raise HTTPException(404, f"account '{name}' not found")
    if not account.enabled:
        raise HTTPException(400, f"account '{name}' is disabled")
    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(400, "test prompt cannot be empty")
    if len(prompt) > 2000:
        raise HTTPException(400, "test prompt is limited to 2000 characters")

    lanes = [lane for lane in account_registry.list_lanes(account=name) if lane.enabled]
    if not lanes and account.auth_kind == "api_key":
        activate_account(name)
        lanes = [lane for lane in account_registry.list_lanes(account=name) if lane.enabled]
    if not lanes:
        raise HTTPException(400, f"account '{name}' has no enabled model lane")

    lane = lanes[0]
    if not registry.list_pool(lane.backend_name):
        raise HTTPException(400, f"account lane backend '{lane.backend_name}' is unavailable")
    try:
        result = _execute_full(lane.backend_name, prompt)
    except ExecutionError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None
    return {
        "ok": True,
        "account": account.name,
        "lane": lane.name,
        "backend": lane.backend_name,
        "content": result["content"],
    }


@app.get("/v1/models")
def openai_list_models() -> dict[str, Any]:
    backends = registry.list_all(enabled_only=True)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for backend in backends:
        for name, backend_type in ((backend.name, backend.backend_type), (backend.pool_name, "pool")):
            if not name or name in seen:
                continue
            seen.add(name)
            rows.append({
                "id": name, "object": "model", "backend_type": backend_type,
                "capabilities": backend.capabilities,
                "circuit_open": backend.circuit_open, "priority": backend.priority,
            })
    for preset in ("code", "reason", "fast"):
        if preset not in seen and registry.list_pool(preset):
            rows.append({
                "id": preset, "object": "model", "backend_type": "capability_pool",
                "capabilities": {preset if preset != "reason" else "reasoning": True},
                "circuit_open": False, "priority": 0,
            })
    for policy in POLICIES.values():
        rows.append({
            "id": policy.name, "object": "model", "backend_type": "routing_policy",
            "capabilities": {"policy": True, "free_only": policy.free_only},
            "circuit_open": False, "priority": 0,
        })
    return {
        "object": "list",
        "data": rows,
    }


@app.post("/v1/chat/completions")
def openai_chat_completions(request: OpenAIChatRequest) -> Any:
    named_lane = None
    if request.cli_account:
        # Resolve at the execution boundary.  The client-supplied model is
        # informational only for this request and cannot redirect identity.
        named_lane, named_backend = _resolve_cli_account_lane(request.cli_account)
        selected_model = named_backend.name
    else:
        selected_model = request.model
    try:
        policy = get_policy(selected_model if selected_model in POLICIES else request.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    prompt = _prompt_from_openai_messages(request.messages)
    external_catalog = _external_tool_catalog(request.tools) if not request.agentic else []
    if external_catalog:
        prompt = _external_tool_prompt(prompt, external_catalog)
    thinking = None
    # Tag adapters.py's call_cli() (a real CLI subprocess with no direct
    # access to this request's project/part) so its own live step events
    # can be filtered by /schedule watch, same as the harness-level events
    # below. Not wrapped in try/finally: every request re-sets this before
    # use, so the only staleness risk is a stray event between requests on
    # a reused threadpool thread tagged with the previous request's scope --
    # low-impact (filtered out by any watcher targeting the current run)
    # and not worth the indentation-rewrite risk of wrapping this whole
    # multi-return-point function body in a try/finally.
    from herald.router import event_bus
    event_bus.set_scope({
        "project": request.project, "part": request.part,
        "consult_depth": request.consult_depth,
    })
    direct_cli_backend = registry.get(selected_model)
    if (
        request.agentic and not direct_cli_backend
        and selected_model not in POLICIES and selected_model != "auto"
        and not registry.list_pool(selected_model)
    ):
        # A model name that resolves to nothing (no direct backend, no
        # policy, no pool) used to fall through into run_harness_agent
        # anyway, which calls execute_model_fn=_execute_tolerant for its
        # own top-level model -- a wrapper meant only to keep ONE failed
        # *nested* mid-chain delegation from killing the whole harness
        # run, not to swallow a request for a model that never existed.
        # That meant an invalid model name silently returned HTTP 200 with
        # the error buried as ordinary-looking response text ("[consultation
        # with X failed: ...]"), so a caller checking ok/success (like
        # consult_models' fallback-chain logic) could never detect the
        # failure and never actually fall back. Fail loudly and immediately
        # instead, matching what a direct (non-agentic) call to the same
        # bad name already does via _execute_full.
        raise HTTPException(404, f"no backend, policy, or pool named '{selected_model}'")
    if request.cli_account:
        _lane, full = _execute_cli_account(request.cli_account, prompt)
        content, thinking, trace = full["content"], full.get("thinking"), None
    elif request.agentic and direct_cli_backend and direct_cli_backend.backend_type == "cli" and not request.force_harness:
        # Window mode: `selected_model` names one specific CLI-profile
        # account directly (not a policy/pool -- registry.get() only
        # matches exact backend names), so there is nothing for Herald to
        # choose or orchestrate. call_cli() already invokes codex/claude/
        # antigravity with --json/stream-json (real native tool access,
        # confirmed live) -- routing this through run_harness_agent below
        # would throw that away and force the CLI to speak Herald's own
        # TOOL_CALL:/READ: text protocol against Herald's own tool catalog
        # instead of the native tools it's actually trained on, which is
        # exactly what caused codex-backup to hallucinate paths tonight.
        # Let it run its own complete session in one call instead.
        try:
            full = _execute_full(selected_model, prompt)
        except ExecutionError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        content, thinking, trace = full["content"], full.get("thinking"), None
    elif request.agentic:
        instances = _catalog_scope(request.project, request.part)
        tools, discovery_errors = _callable_tool_catalog(instances)
        available_models = model_catalog(
            registry.list_all(enabled_only=True), policy, exclude=selected_model,
        )
        prompt_tools = compact_tools(tools, policy)

        def _emit_agent_step(kind: str, data: dict[str, Any]) -> None:
            # Live per-step visibility for an in-flight agentic call.
            # run_harness_agent() already calls this on every model
            # delegation and tool call (tool_started, model_finished, etc)
            # via its own `emit()` -- it just had nowhere to go before this.
            # Tagged with project/part (not a request id) since that's what
            # a caller like a Herald schedule already knows ahead of time --
            # no new correlation-id plumbing needed to watch "this
            # schedule's currently-running work" via the SSE stream.
            # NOTE: this route handler is a sync `def`, so FastAPI runs it
            # in a worker thread, not the main event loop thread --
            # event_bus.emit_nowait() already has to tolerate being called
            # off-loop (same as registry.py's record_failure/success), so
            # this follows the same existing pattern rather than a new one.
            try:
                from herald.router import event_bus
                event_bus.emit_nowait(
                    "agent.step", importance=0.2,
                    payload={**data, "kind": kind, "project": request.project, "part": request.part},
                    source="harness_agent",
                )
            except Exception:
                pass

        result = run_harness_agent(
            _execute_tolerant,
            selected_model,
            prompt,
            models=available_models,
            tools=prompt_tools,
            call_tool_fn=lambda name, arguments: _execute_catalog_tool(
                name, arguments, tools, discovery_errors,
            ),
            limits=HarnessLimits(
                max_depth=request.max_depth,
                max_parallel=request.max_parallel,
                max_iterations=request.max_tool_iterations,
                max_model_calls=request.max_model_calls,
                max_tool_calls=request.max_tool_calls,
            ),
            event_fn=_emit_agent_step,
        )
        content, trace = result["content"], result["trace"]
    else:
        try:
            full = _execute_full(selected_model, prompt)
        except ExecutionError as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        content, thinking, trace = full["content"], full.get("thinking"), None
    bridge_backend = policy.tool_bridge or "fast"
    response = _openai_response_payload(
        selected_model, content, external_catalog, bridge_backend=bridge_backend,
    )
    response["herald_policy"] = policy.to_dict()
    # Separate thinking stream: only ever populated when a backend actually
    # returns real reasoning text (LM Studio's reasoning_content today) --
    # never fabricated from a token count.
    if thinking:
        response["thinking"] = thinking
    if trace is not None:
        response["orchestration_trace"] = trace
        if request.agentic:
            response["orchestration_budget"] = result.get("budget")
    if request.stream:
        return StreamingResponse(_stream_openai_payload(response), media_type="text/event-stream")
    return response


@app.get("/route/status")
def route_status() -> dict[str, Any]:
    """Return the most recently executed backend, its type, and latency for live UI display."""
    calls = telemetry.recent_calls(limit=5)
    last = calls[0] if calls else {}
    return {
        "last_backend": last.get("backend_name"),
        "last_backend_type": last.get("backend_type"),
        "last_duration_ms": last.get("duration_ms"),
        "last_success": bool(last.get("success")),
        "recent": [
            {
                "backend": c.get("backend_name"),
                "duration_ms": c.get("duration_ms"),
                "success": bool(c.get("success")),
            }
            for c in calls[:5]
        ],
    }


@app.get("/route/current")
def route_current() -> dict[str, Any]:
    return route_status()


# ---------------------------------------------------------------------------
# Anthropic-compatible surface
# ---------------------------------------------------------------------------

class AnthropicMessagesRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int = 1024


@app.post("/v1/messages")
def anthropic_messages(request: AnthropicMessagesRequest) -> dict[str, Any]:
    # Anthropic's content field can be a plain string or a list of typed
    # content blocks; normalize both to text for the single-prompt adapters.
    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        return ""

    prompt = "\n\n".join(f"{m.get('role', 'user')}: {_text(m.get('content'))}" for m in request.messages)
    try:
        content = _execute(request.model, prompt)
    except ExecutionError as exc:
        raise HTTPException(exc.status_code, {"type": "error", "error": {"message": exc.detail}}) from None
    return {
        "id": f"msg_router_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": request.model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# ---------------------------------------------------------------------------
# Ollama-compatible surface
# ---------------------------------------------------------------------------

class OllamaChatRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False


class OllamaGenerateRequest(BaseModel):
    model: str
    prompt: str
    stream: bool = False


@app.get("/api/tags")
def ollama_tags() -> dict[str, Any]:
    backends = registry.list_all(enabled_only=True)
    return {
        "models": [
            {"name": b.name, "model": b.name, "details": {"backend_type": b.backend_type}}
            for b in backends
        ]
    }


@app.post("/api/chat")
def ollama_chat(request: OllamaChatRequest) -> dict[str, Any]:
    prompt = _prompt_from_openai_messages(request.messages)
    try:
        content = _execute(request.model, prompt)
    except ExecutionError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None
    return {
        "model": request.model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message": {"role": "assistant", "content": content},
        "done": True,
    }


@app.post("/api/generate")
def ollama_generate(request: OllamaGenerateRequest) -> dict[str, Any]:
    try:
        content = _execute(request.model, request.prompt)
    except ExecutionError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None
    return {
        "model": request.model,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "response": content,
        "done": True,
    }


# ---------------------------------------------------------------------------
# Node control -- lifecycle management (load/unload/status) for local-model
# runtimes on other nodes. Separate from inference (above): a registered
# `local_model` backend just calls whatever's already loaded; these endpoints
# are what actually gets a model loaded/unloaded in the first place.
# ---------------------------------------------------------------------------

class LoadRequest(BaseModel):
    node: str = "local"
    runtime: str  # "lmstudio" | "ollama"
    model: str


class ReloadRequest(BaseModel):
    targets: list[str] = ["router"]
    delay_seconds: int = 3
    force: bool = False


@app.get("/control/reload/status")
def reload_status() -> dict[str, Any]:
    return reload_control.source_status(Path(__file__).resolve().parents[2])


@app.post("/control/reload", status_code=202)
def schedule_reload(request: ReloadRequest) -> dict[str, Any]:
    active = [
        run.public() for run in _agent_runs().list(limit=100)
        if run.status in {"queued", "running"}
    ]
    if active and not request.force:
        raise HTTPException(
            409, {"message": "long-running work is still active", "active_runs": active},
        )
    try:
        return reload_control.schedule_reload(
            request.targets, delay_seconds=request.delay_seconds,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None


@app.get("/nodes/dashboard")
def nodes_dashboard() -> dict[str, Any]:
    """Cross-node health dashboard and active run telemetry."""
    from herald.tailscale import fleet_status
    nodes = {}
    
    # Primary local router node
    nodes["local"] = {
        "name": "local",
        "role": "primary_router",
        "status": "online",
        "reachable": True,
        "backends_count": len(registry.list_all()),
    }

    # Discover and poll active fleet peers
    for device in fleet_status(include_offline=False):
        hostname = device.get("hostname")
        if not hostname or hostname in ("local", "localhost"):
            continue
        status_res = node_control.lmstudio_status(hostname)
        nodes[hostname] = {
            "name": hostname,
            "role": "fleet_node",
            "reachable": status_res.get("ok", False),
            "status": "online" if status_res.get("ok") else "standby",
            "lms_status": status_res,
        }

    # Active flow runs and sessions summary
    active_runs = []
    try:
        store = _flow_runs()
        active_runs = [r.public() for r in store.list(limit=20) if r.status in ("running", "pending_approval")]
    except Exception:
        pass

    return {
        "nodes": nodes,
        "active_runs": active_runs,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@app.get("/control/local/status")
def control_status(node: str = "local", runtime: str = "lmstudio") -> dict[str, Any]:
    fn = {"lmstudio": node_control.lmstudio_status, "ollama": node_control.ollama_status}.get(runtime)
    if fn is None:
        raise HTTPException(400, f"unknown runtime '{runtime}'")
    return fn(node)


@app.get("/control/local/list")
def control_list(node: str = "local", runtime: str = "lmstudio") -> dict[str, Any]:
    fn = {
        "lmstudio": node_control.lmstudio_list_on_disk,
        "ollama": node_control.ollama_list_on_disk,
    }.get(runtime)
    if fn is None:
        raise HTTPException(400, f"unknown runtime '{runtime}'")
    return fn(node)


@app.post("/control/local/load")
def control_load(request: LoadRequest) -> dict[str, Any]:
    try:
        if request.runtime == "lmstudio":
            result = node_control.lmstudio_load(request.model, request.node)
        elif request.runtime == "ollama":
            result = node_control.ollama_load(request.node, request.model)
        else:
            raise HTTPException(400, f"unknown runtime '{request.runtime}'")
    except node_control.UnsafeModelNameError as exc:
        raise HTTPException(400, str(exc))
    if result.get("ok"):
        # Keep the discovered backend row's enabled state in sync with reality
        # -- a load just succeeded, so it's now safe to route calls to it
        # without every candidate-pool loop treating "not loaded yet" as a
        # failure. No-op if this exact model was never discovered/registered.
        discovery.set_local_model_loaded(registry, request.node, request.runtime, request.model, True)
    return result


@app.post("/control/local/unload")
def control_unload(request: LoadRequest) -> dict[str, Any]:
    try:
        if request.runtime == "lmstudio":
            result = node_control.lmstudio_unload(request.model, request.node)
        elif request.runtime == "ollama":
            result = node_control.ollama_unload(request.node, request.model)
        else:
            raise HTTPException(400, f"unknown runtime '{request.runtime}'")
    except node_control.UnsafeModelNameError as exc:
        raise HTTPException(400, str(exc))
    if result.get("ok"):
        discovery.set_local_model_loaded(registry, request.node, request.runtime, request.model, False)
    return result


@app.post("/backends/discover")
def backends_discover(node: str = "local") -> dict[str, Any]:
    """Registers every downloaded LM Studio/Ollama model on `node` as a
    local_model backend (enabled only if currently loaded) -- see
    discovery.py for the naming/enabled-state rules."""
    return discovery.discover_and_register(registry, node)


@app.post("/backends/bootstrap")
def backends_bootstrap() -> dict[str, Any]:
    """Re-discover local CLIs and free G4F gateways idempotently."""
    return bootstrap_registry(registry)


# ---------------------------------------------------------------------------
# Telemetry -- logging and usage tracking, ingested here for the UI to
# display once it exists (per-backend token usage, cost where known,
# browser-session key health, recent call log).
# ---------------------------------------------------------------------------

@app.get("/auth/status")
def auth_status() -> dict[str, Any]:
    """Login status for enabled CLI accounts configured in this Router."""
    profiles = _configured_cli_usage_profiles()
    return {"clis": cli_auth.configured_status(profiles)}


@app.get("/auth/capabilities")
def auth_capabilities() -> dict[str, Any]:
    supported = cli_auth.auth_capabilities().get("clis", {})
    configured = {}
    for profile in _configured_cli_usage_profiles():
        adapter = str(profile.get("cli_name") or "")
        if adapter in supported:
            configured[str(profile["name"])] = {**supported[adapter], "adapter": adapter}
    return {"clis": configured}


@app.get("/auth/providers")
def auth_provider_discovery() -> dict[str, Any]:
    """Available login adapters are discovery, not connected accounts."""
    from herald.router.provider_discovery import discover_login_providers
    return {"providers": discover_login_providers()}



class LoginStartRequest(BaseModel):
    cli: str


class LoginCodeRequest(BaseModel):
    cli: str
    code: str


def _auth_lifecycle_response(result: dict[str, Any]) -> Any:
    """Keep lifecycle failures structured and safe on every HTTP surface."""
    if not result.get("error"):
        return result
    if result.get("state") == "idle":
        status_code = 404
    elif result.get("state") == "pending":
        status_code = 409
    else:
        status_code = 400
    safe = {
        key: value for key, value in result.items()
        if key in {"cli", "state", "running", "exit_code", "cancelled", "output", "error"}
    }
    return JSONResponse(safe, status_code=status_code)


def _configured_login_target(name: str) -> tuple[str, str, dict[str, str]] | None:
    """Resolve a requested name (a profile name or a bare CLI/adapter name) to
    (adapter, account, env). `account` is the profile name itself so two
    profiles sharing one adapter (two `antigravity` accounts, say) get
    independent login sessions; `env` is that profile's own config.env
    (its own HOME/CODEX_HOME/etc.) so each account's login lands in
    its own credential storage instead of overwriting a shared default."""
    normalized = name.strip().casefold()
    for profile in _configured_cli_usage_profiles():
        profile_name = str(profile.get("name") or "")
        adapter = str(profile.get("cli_name") or "").casefold()
        if normalized in {profile_name.casefold(), adapter}:
            config = profile.get("config") if isinstance(profile.get("config"), dict) else {}
            raw_env = config.get("env") if isinstance(config.get("env"), dict) else {}
            env = {str(key): str(value) for key, value in raw_env.items()}
            return adapter, (profile_name or adapter), env
    return None


@app.post("/auth/login/start")
def auth_login_start(request: LoginStartRequest) -> Any:
    """Start a CLI device auth login flow."""
    target = _configured_login_target(request.cli)
    if target is None:
        raise HTTPException(404, f"configured CLI account '{request.cli}' was not found")
    adapter, account, env = target
    return _auth_lifecycle_response(cli_auth.start_login(adapter, account=account, env=env))


@app.post("/auth/login/code")
def auth_login_code(request: LoginCodeRequest) -> Any:
    """Paste a code back into an in-progress login process stdin."""
    target = _configured_login_target(request.cli)
    if target is None:
        raise HTTPException(404, f"configured CLI account '{request.cli}' was not found")
    adapter, account, _env = target
    return _auth_lifecycle_response(cli_auth.submit_login_code(adapter, request.code, account=account))



@app.get("/auth/login/{cli}")
def auth_login_snapshot(cli: str) -> Any:
    target = _configured_login_target(cli)
    if target is None:
        raise HTTPException(404, f"configured CLI account '{cli}' was not found")
    adapter, account, _env = target
    return _auth_lifecycle_response(cli_auth.login_snapshot(adapter, account=account))


@app.post("/auth/login/{cli}/cancel")
def auth_login_cancel(cli: str) -> Any:
    """Cancel a router-owned pending login process; credentials are untouched."""
    target = _configured_login_target(cli)
    if target is None:
        raise HTTPException(404, f"configured CLI account '{cli}' was not found")
    adapter, account, _env = target
    return _auth_lifecycle_response(cli_auth.cancel_login(adapter, account=account))


@app.post("/auth/refresh")
def auth_refresh_all() -> dict[str, Any]:
    """Compatibility endpoint: recheck status only; never claim token refresh."""
    statuses = cli_auth.configured_status(_configured_cli_usage_profiles())
    return {
        "status": "ok",
        "operation": "status_check",
        "credentials_refreshed": False,
        "checked_at": datetime.now(UTC).isoformat(),
        "message": "Authentication status rechecked; no credentials were refreshed.",
        "details": {row["cli"]: row for row in statuses},
    }


@app.get("/usage/cli")
def usage_cli(refresh: bool = False) -> dict[str, Any]:
    """Usage for CLI-backed tools own sessions."""
    return {"clis": cli_usage.all_usage(refresh=refresh, profiles=_configured_cli_usage_profiles())}


def _configured_cli_usage_profiles() -> list[dict[str, Any]]:
    """Describe only enabled CLI identities registered to this Router."""
    profiles: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str]] = set()
    for account in account_registry.list_accounts(enabled_only=True):
        if account.auth_kind != "cli_profile":
            continue
        config = dict(account.config)
        raw_name = str(config.get("cli_name") or account.provider or account.name)
        family = "antigravity" if raw_name.startswith("profile_") else raw_name
        try:
            from herald.router import adapters
            from clink.registry import ClinkRegistry
            family = ClinkRegistry().get_client(raw_name).runner or family
        except Exception:
            pass  # Preserve configured identities even if their CLI is missing.
        environment = config.get("env") if isinstance(config.get("env"), dict) else {}
        source = str(
            environment.get("CODEX_HOME")
            or environment.get("CLAUDE_CONFIG_DIR")
            or environment.get("HOME")  # antigravity (agy) isolates its ~/.gemini
            or environment.get("XDG_CONFIG_HOME")
            or config.get("home")
            or config.get("config_dir")
            # Fall back to the account's own name, not a shared "default" bucket,
            # so two accounts of a CLI whose isolation variable isn't one of the
            # ones named above (or that isolate purely by account.name) don't
            # collide and silently drop one of them here.
            or account.name
        )
        source_key = (family.casefold(), source.casefold())
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        profiles.append({"name": account.name, "cli_name": family, "config": config})
    return profiles



@app.get("/usage/summary")
def http_usage_summary() -> dict[str, Any]:
    """Return the aggregate telemetry consumed by both Herald clients."""
    return telemetry.usage_summary()


@app.get("/usage/all")
def usage_all(refresh: bool = False, include_history: bool = False) -> dict[str, Any]:
    """Unify every usage signal Herald can measure without inventing quotas."""
    summary = telemetry.usage_summary()
    measured = {row["backend_name"]: row for row in summary["by_backend"]}
    backends = []
    for backend in registry.list_all():
        row = measured.get(backend.name, {})
        backends.append({
            "name": backend.name,
            "type": backend.backend_type,
            "pool": backend.pool_name,
            "enabled": backend.enabled,
            "circuit_open": backend.circuit_open,
            "total_calls": row.get("total_calls", 0),
            "successful_calls": row.get("successful_calls", 0),
            "input_tokens": row.get("total_input_tokens"),
            "output_tokens": row.get("total_output_tokens"),
            "cost_usd": row.get("total_cost_usd"),
            "measurement": "router_telemetry" if row else "no_recorded_router_calls",
            "provider_quota": "not_exposed",
        })
    if include_history:
        known = {row["name"] for row in backends}
        for name, row in measured.items():
            if name not in known:
                backends.append({
                    "name": name, "type": row.get("backend_type"), "pool": None,
                    "enabled": False, "circuit_open": False,
                    "total_calls": row.get("total_calls", 0),
                    "successful_calls": row.get("successful_calls", 0),
                    "input_tokens": row.get("total_input_tokens"),
                    "output_tokens": row.get("total_output_tokens"),
                    "cost_usd": row.get("total_cost_usd"),
                    "measurement": "historical_router_telemetry",
                    "provider_quota": "not_exposed",
                })
    totals = {
        "calls": sum(int(row["total_calls"] or 0) for row in backends),
        "successful_calls": sum(int(row["successful_calls"] or 0) for row in backends),
        "input_tokens": sum(int(row["input_tokens"] or 0) for row in backends),
        "output_tokens": sum(int(row["output_tokens"] or 0) for row in backends),
        "known_cost_usd": sum(float(row["cost_usd"] or 0) for row in backends),
        "cost_reporting_backends": sum(row["cost_usd"] is not None for row in backends),
    }
    return {
        "totals": totals,
        "backends": backends,
        "cli_sessions": cli_usage.all_usage(
            refresh=refresh, profiles=_configured_cli_usage_profiles()
        ),
        "browser_sessions": summary["browser_sessions"],
        "quotas": quota_tracker.get_all_quotas(registry.list_all()),
        "coverage": [
            "Router calls are measured for CLI, API-key, browser-session, and local backends.",
            "Codex and Claude subscription percentages and reset timers come from the same native account sources used by their /usage screens.",
            "Claude local transcripts also expose token/cache usage; Codex subscription transcripts may not expose per-session token counts.",
            "G4F/browser sessions and local models have health/call counts but no provider token charge.",
            "Google AI Studio and web sliding windows are tracked against their daily and burst limits.",
        ],
    }



@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "healthy", "service": "Herald Unified Router"}


# ---------------------------------------------------------------------------
# Workspace -- the directory coding tools operate inside
# ---------------------------------------------------------------------------

class WorkspaceSetRequest(BaseModel):
    path: str


@app.get("/workspace")
def get_workspace() -> dict[str, Any]:
    """Return the active workspace root used by the built-in coding tools."""
    import os
    workspace = os.environ.get("HERALD_WORKSPACE", str(Path.cwd()))
    return {"workspace": workspace}


@app.post("/workspace")
def set_workspace(request: WorkspaceSetRequest) -> dict[str, Any]:
    """Change the workspace root that the built-in coding tools operate inside.

    Updates the ``HERALD_WORKSPACE`` environment variable for this process and
    re-registers the coding-tools instance so the next agentic session picks up
    the new path.  The router does **not** need a restart.
    """
    import os
    from herald.router.bootstrap import (
        _CODING_TOOLS_INSTANCE_NAME, _CODING_TOOLS_PATH, _register_coding_tools,
    )
    new_path = Path(request.path).expanduser().resolve()
    if not new_path.exists():
        raise HTTPException(400, f"path '{new_path}' does not exist")
    if not new_path.is_dir():
        raise HTTPException(400, f"path '{new_path}' is not a directory")
    os.environ["HERALD_WORKSPACE"] = str(new_path)
    # Re-register to pick up the new env value.
    tool_registry.remove_tool_instance(_CODING_TOOLS_INSTANCE_NAME)
    _register_coding_tools(registry)
    return {"workspace": str(new_path), "coding_tools_re_registered": True}


@app.get("/fleet/context")
def get_fleet_context() -> dict[str, Any]:
    """Return live Tailscale and SSH device context for agents and interactive shell."""
    try:
        from herald.tailscale import agent_context, fleet_status
        return {"ok": True, "context": agent_context(), "devices": fleet_status()}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "context": ""}


@app.get("/steering/personas/{name}")
def get_steering_persona(name: str) -> dict[str, Any]:
    """Expose a named steering persona's prompt text so non-Python clients
    (the mobile web app) can fold it into a session's instructions."""
    from herald.steering import steering
    return {"name": name, "prompt": steering.get(name)}


@app.post("/voice/transcribe")
async def voice_transcribe(
    audio: UploadFile = File(...),
    browser_transcript: str | None = Form(None),
) -> dict[str, Any]:
    """Multi-engine speech-to-text for the mobile app: the browser sends its
    recorded clip (transcribed here with Whisper, and SAPI where available)
    plus whatever the browser's own Web Speech API already heard client-side.
    When engines disagree, the router itself arbitrates using full-sentence
    context -- the same approach `herald talk` uses on the CLI."""
    import tempfile as _tempfile
    import os as _os

    suffix = Path(audio.filename or "clip.webm").suffix or ".webm"
    fd, tmp_path = _tempfile.mkstemp(suffix=suffix, prefix="herald-voice-")
    try:
        with _os.fdopen(fd, "wb") as fh:
            total = 0
            limit = _max_voice_upload_bytes()
            while chunk := await audio.read(_VOICE_UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, f"audio upload exceeds the {limit}-byte limit")
                fh.write(chunk)
        candidates = voice_io.transcribe_all(tmp_path, extra_candidates=[browser_transcript] if browser_transcript else None)
    finally:
        await audio.close()
        _os.unlink(tmp_path)

    texts = [c["text"] for c in candidates if c.get("text")]
    unique = list(dict.fromkeys(texts))
    if not unique:
        return {"text": "", "candidates": candidates}
    if len(unique) == 1:
        return {"text": unique[0], "candidates": candidates}

    listing = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(unique))
    prompt = (
        "These are candidate transcriptions of the same short spoken utterance, "
        "produced by different speech recognizers that may have misheard words:\n\n"
        f"{listing}\n\n"
        "Reply with ONLY the single most likely correct transcription -- pick the best "
        "one or merge them if the correct sentence is obvious from combining parts. "
        "No commentary, no quotes, no numbering."
    )
    try:
        reconciled = _execute("auto", prompt).strip()
    except ExecutionError:
        reconciled = ""
    return {"text": reconciled or unique[0], "candidates": candidates}


@app.get("/v1/usage")
def v1_usage(refresh: bool = False) -> dict[str, Any]:
    """Compatibility alias for PAL Bridge /v1/usage."""
    all_u = usage_all(refresh=refresh)
    return {
        "status": "healthy",
        "unified_usage_monitoring": all_u.get("cli_sessions"),
        "totals": all_u.get("totals"),
        "backends": all_u.get("backends"),
        "timestamp": datetime.now(UTC).isoformat()
    }


class PalModelGroupRequest(BaseModel):
    prompt: str
    system_prompt: str | None = None
    stream: bool | None = False


class PalModelGroupResponse(BaseModel):
    profile: str
    engine: str
    model_name: str
    meter: str
    usage_monitoring: dict[str, Any] | None = None
    response: str
    status: str = "success"


def _route_pal_profile(profile_key: str, default_model: str, req: PalModelGroupRequest) -> PalModelGroupResponse:
    prompt = f"System: {req.system_prompt}\n\nUser: {req.prompt}" if req.system_prompt else req.prompt
    candidate = profile_key if registry.get(profile_key) else ("antigravity" if registry.get("antigravity") else "claude-cli")
    try:
        res = _execute_full(candidate, prompt)
        content = res.get("content", "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return PalModelGroupResponse(
        profile=profile_key,
        engine="Herald Unified Router",
        model_name=default_model,
        meter=f"Routed via {candidate}",
        response=content,
        status="success"
    )


@app.post("/v1/profile/gemini", response_model=PalModelGroupResponse)
@app.post("/v1/group1", response_model=PalModelGroupResponse)
@app.post("/v1/group/gemini", response_model=PalModelGroupResponse)
def route_pal_gemini(req: PalModelGroupRequest) -> PalModelGroupResponse:
    return _route_pal_profile("antigravity-gemini", "Gemini 3.7 Flash (Medium)", req)


@app.post("/v1/profile/claude", response_model=PalModelGroupResponse)
@app.post("/v1/group2", response_model=PalModelGroupResponse)
@app.post("/v1/group/claude_gpt", response_model=PalModelGroupResponse)
def route_pal_claude(req: PalModelGroupRequest) -> PalModelGroupResponse:
    return _route_pal_profile("antigravity-claude", "Claude Sonnet 4.6 (Thinking)", req)


@app.post("/v1/profile/gpt", response_model=PalModelGroupResponse)
def route_pal_gpt(req: PalModelGroupRequest) -> PalModelGroupResponse:
    return _route_pal_profile("antigravity-gpt", "GPT-OSS 120B (Medium)", req)


@app.post("/v1/native/codex", response_model=PalModelGroupResponse)
def route_pal_native_codex(req: PalModelGroupRequest) -> PalModelGroupResponse:
    prompt = f"System: {req.system_prompt}\n\nUser: {req.prompt}" if req.system_prompt else req.prompt
    try:
        res = _execute_full("codex-primary", prompt)
        content = res.get("content", "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return PalModelGroupResponse(
        profile="Native Codex",
        engine="Codex CLI",
        model_name="codex default",
        meter="Native OpenAI Codex CLI",
        response=content,
        status="success"
    )


@app.post("/v1/native/claude", response_model=PalModelGroupResponse)
def route_pal_native_claude(req: PalModelGroupRequest) -> PalModelGroupResponse:
    prompt = f"System: {req.system_prompt}\n\nUser: {req.prompt}" if req.system_prompt else req.prompt
    try:
        res = _execute_full("claude-cli", prompt)
        content = res.get("content", "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return PalModelGroupResponse(
        profile="Native Claude Code",
        engine="Claude CLI",
        model_name="claude default",
        meter="Native Claude Code CLI",
        response=content,
        status="success"
    )


@app.get("/logs/recent")
def logs_recent(limit: int = 50) -> dict[str, Any]:
    return {"calls": telemetry.recent_calls(limit)}



# ---------------------------------------------------------------------------
# Direct CLI & Tool Execution Endpoints
# ---------------------------------------------------------------------------

class ClinkRunRequest(BaseModel):
    cli_name: str
    prompt: str
    env: dict[str, Any] = {}
    timeout: int = 180


@app.post("/clink/run")
def http_clink_run(request: ClinkRunRequest) -> dict[str, Any]:
    from herald.router.adapters import call_cli
    config = {"cli_name": request.cli_name, "env": request.env}
    return call_cli(config, request.prompt, timeout=request.timeout)


class ToolRunRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}
    project: str | None = None
    part: str | None = None


_tool_discovery_cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_tool_discovery_cache_lock = threading.Lock()


def _discover_instance_cached(instance) -> dict[str, Any]:
    key = (instance.id, instance.updated_at)
    now = time.monotonic()
    with _tool_discovery_cache_lock:
        cached = _tool_discovery_cache.get(key)
        if cached and now < cached[0]:
            return cached[1]
    result = discover_tools(instance.transport, instance.config)
    ttl = 300 if result.get("ok") else 10
    with _tool_discovery_cache_lock:
        for stale in [item for item in _tool_discovery_cache if item[0] == instance.id and item != key]:
            _tool_discovery_cache.pop(stale, None)
        _tool_discovery_cache[key] = (now + ttl, result)
    return result


_CUSTOM_TOOL_INSTANCE = "__custom__"


def _custom_tool_catalog_rows() -> list[dict[str, Any]]:
    """`@herald.tool`-decorated functions (herald/dev.py) were registered
    into an in-memory dict that nothing in the actual agent/model tool loop
    ever read -- a user following that decorator's own docstring got a tool
    that silently did nothing when an agent tried to call it. This bridges
    them into the same catalog shape MCP-discovered tools use, so they
    become real, callable tools instead of dead registration."""
    from herald.dev import list_custom_tools
    rows = []
    for schema in list_custom_tools():
        name = schema["name"]
        rows.append({
            "name": name,
            "qualified_name": f"custom.{name}",
            "instance": _CUSTOM_TOOL_INSTANCE,
            "alias": "custom",
            "package": None,
            "version": None,
            "scope": "global",
            "description": schema.get("description", ""),
            "input_schema": schema.get("inputSchema") or {},
            "transport": "python",
            "tags": ["custom"],
        })
    return rows


def _callable_tool_catalog(instances=None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    discovered: list[dict[str, Any]] = _custom_tool_catalog_rows()
    errors: list[dict[str, str]] = []
    if instances is None:
        instances = tool_registry.list_tool_instances(scope="global")
    instances = list(instances)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(instances)))) as executor:
        results = list(executor.map(_discover_instance_cached, instances))
    for instance, result in zip(instances, results):
        if not result.get("ok"):
            errors.append({"instance": instance.name, "error": result.get("error", "discovery failed")})
            continue
        for tool in result.get("tools", []):
            actual_name = tool.get("name")
            if not actual_name:
                continue
            if instance.allowed_tools is not None and actual_name not in instance.allowed_tools:
                continue
            discovered.append({
                "name": actual_name,
                "qualified_name": f"{instance.alias or instance.name}.{actual_name}",
                "instance": instance.name,
                "alias": instance.alias,
                "package": instance.package_name,
                "version": instance.version,
                "scope": instance.scope,
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema") or tool.get("input_schema") or {},
                "transport": instance.transport,
                "tags": instance.tags,
            })

    counts: dict[str, int] = {}
    for row in discovered:
        counts[row["name"]] = counts.get(row["name"], 0) + 1
    for row in discovered:
        if counts[row["name"]] > 1:
            row["name"] = row["qualified_name"]
    return discovered, errors


def _catalog_scope(project: str | None, part: str | None):
    if bool(project) != bool(part):
        raise HTTPException(400, "project and part must be provided together")
    instances = tool_registry.resolve_mcp_access(project=project, part=part)
    if not project:
        instances.extend(tool_registry.list_tool_instances(scope="global"))
    unique = {}
    for instance in instances:
        key = (instance.id, instance.alias)
        existing = unique.get(key)
        if existing and existing.allowed_tools is not None:
            if instance.allowed_tools is None:
                existing.allowed_tools = None
            else:
                existing.allowed_tools = sorted(set(existing.allowed_tools + instance.allowed_tools))
        elif not existing:
            unique[key] = instance
    return list(unique.values())


@app.get("/tools")
def http_list_tools(project: str | None = None, part: str | None = None) -> dict[str, Any]:
    tools, errors = _callable_tool_catalog(_catalog_scope(project, part))
    return {"tools": tools, "discovery_errors": errors}


def _execute_catalog_tool(
    name: str,
    arguments: dict[str, Any],
    catalog: list[dict[str, Any]],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    matches = [
        row for row in catalog
        if name in {row["name"], row["qualified_name"]}
    ]
    if not matches:
        instance = tool_registry.get_tool_instance(name)
        if instance and not instance.config.get("tool_name"):
            return {
                "ok": False,
                "error": f"'{name}' is an MCP server instance, not a callable tool",
            }
        detail = f"callable tool '{name}' not found"
        if errors:
            detail += f"; discovery errors: {errors}"
        return {"ok": False, "error": detail}
    if len(matches) > 1:
        qualified = [row["qualified_name"] for row in matches]
        return {"ok": False, "error": f"tool name is ambiguous; use one of: {qualified}"}

    match = matches[0]
    if match["instance"] == _CUSTOM_TOOL_INSTANCE:
        from herald.dev import execute_custom_tool
        try:
            result = execute_custom_tool(match["name"], arguments)
        except Exception as exc:  # noqa: BLE001 - one custom tool bug must not crash the router
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"content": result if isinstance(result, str) else json.dumps(result, default=str)}

    instance = tool_registry.get_tool_instance(match["instance"])
    config = {**instance.config, "tool_name": match["qualified_name"].split(".", 1)[1]}
    return execute_tool(instance.name, instance.transport, config, arguments)


@app.post("/tools/run")
def http_run_tool(request: ToolRunRequest) -> dict[str, Any]:
    catalog, errors = _callable_tool_catalog(_catalog_scope(request.project, request.part))
    result = _execute_catalog_tool(request.name, request.arguments, catalog, errors)
    if not result.get("ok") and "not found" in result.get("error", ""):
        from herald.tool_evolution import synthesize_local_tool
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", request.name).strip("_")[:64] or "missing_tool"
        schema = {
            "type": "object",
            "properties": {str(key): {"description": "Observed argument"} for key in request.arguments},
        }
        proposal = synthesize_local_tool(
            safe_name,
            f"Local proposal synthesized after missing capability: {safe_name}",
            schema,
        )
        raise HTTPException(404, {"error": result["error"], "local_tool_proposal": str(proposal)})
    if not result.get("ok") and "ambiguous" in result.get("error", ""):
        raise HTTPException(409, result["error"])
    return result


# ---------------------------------------------------------------------------
# Named stateful agents -- sequential package loops with encrypted memory
# ---------------------------------------------------------------------------

class AgentSessionCreateRequest(BaseModel):
    name: str
    memory: str | None = None
    model: str = "auto"
    mode: str = "efficiency"
    instructions: str = ""
    project: str | None = None
    part: str | None = None
    agentic: bool = False
    recent_turns: int = 8
    ledger_entries: int = 40
    retention_days: int | None = None


class AgentSessionMessageRequest(BaseModel):
    prompt: str
    request_id: str | None = None


class AgentSessionImportRequest(BaseModel):
    data: dict[str, Any]


class AgentSessionResetRequest(BaseModel):
    force: bool = False
    instructions: str = ""


_agent_session_store: AgentSessionStore | None = None
_agent_run_store: AgentRunStore | None = None
_agent_session_locks: dict[str, threading.Lock] = {}
_agent_session_locks_guard = threading.Lock()
_agent_run_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="herald-agent-run")


def _agent_sessions() -> AgentSessionStore:
    global _agent_session_store
    if _agent_session_store is None:
        _agent_session_store = AgentSessionStore()
    return _agent_session_store


def _agent_runs() -> AgentRunStore:
    global _agent_run_store
    if _agent_run_store is None:
        _agent_run_store = AgentRunStore()
    return _agent_run_store


def _agent_lock(session_id: str) -> threading.Lock:
    with _agent_session_locks_guard:
        return _agent_session_locks.setdefault(session_id, threading.Lock())


def _session_model(requested: str, mode: str, prompt: str = "") -> str:
    try:
        policy = get_policy(mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if requested == "auto":
        # "auto" is the real let-the-router-decide signal, so this is the
        # only path complexity-aware reordering applies to -- requesting a
        # named policy directly as `model` (requested in POLICIES) stays
        # literal, unaffected by prompt content.
        try:
            return automatic_model(registry.list_all(), policy, prompt=prompt)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None
    if requested in POLICIES:
        try:
            return automatic_model(registry.list_all(), policy)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None
    if not registry.list_pool(requested):
        raise HTTPException(400, f"model or backend '{requested}' is unavailable")
    return requested


@app.post("/agent-sessions")
def open_agent_session(request: AgentSessionCreateRequest) -> dict[str, Any]:
    if len(request.instructions) > 20_000:
        raise HTTPException(400, "agent instructions are limited to 20000 characters")
    try:
        session = _agent_sessions().open(
            name=request.name,
            memory=request.memory or request.name,
            model=_session_model(request.model, request.mode, prompt=request.instructions),
            mode=request.mode,
            instructions=request.instructions.strip(),
            project=request.project,
            part=request.part,
            agentic=request.agentic,
            recent_turns=request.recent_turns,
            ledger_entries=request.ledger_entries,
            retention_days=request.retention_days,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"session": session.public(), "reused": session.turn_count > 0}


@app.get("/agent-sessions")
def list_agent_sessions(limit: int = 100) -> dict[str, Any]:
    return {
        "sessions": [
            session.public()
            for session in _agent_sessions().list(min(max(limit, 1), 500))
        ]
    }


@app.get("/agent-sessions/search")
def search_agent_sessions(
    query: str,
    project: str | None = None,
    part: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    if not query.strip():
        return {"results": []}
    results = _agent_sessions().search(
        query=query,
        project=project,
        part=part,
        since=since,
        until=until,
        limit=min(max(limit, 1), 100),
    )
    return {"results": results}


@app.get("/agent-sessions/{session_id}")
def inspect_agent_session(session_id: str) -> dict[str, Any]:
    try:
        return _agent_sessions().inspect(session_id)
    except KeyError:
        raise HTTPException(404, "agent session not found") from None


@app.post("/agent-sessions/{session_id}/messages")
def agent_session_message(session_id: str, request: AgentSessionMessageRequest) -> dict[str, Any]:
    return _execute_agent_session_message(session_id, request, wait_for_lock=False)


_LIVE_ACTION_RE = re.compile(
    r"\b(check|inspect|scan|search|find|list|read|open|query|run|execute|test|verify|"
    r"inventory|refresh|update|modify|edit|create|delete|start|stop|restart|deploy)\b",
    re.IGNORECASE,
)
_LIVE_TARGET_RE = re.compile(
    r"\b(ssh|device|machine|computer|server|filesystem|file|folder|director(?:y|ies)|"
    r"repo(?:sitory)?|repositories|project|service|process|logs?|account|usage|status|"
    r"git|github|database|port|endpoint)\b|(?:[a-z]:\\|/home/|/opt/|~/)",
    re.IGNORECASE,
)


def _requires_live_tool(input_text: str, tools: list[dict[str, Any]]) -> bool:
    """Require grounding when an actionable request depends on observable state."""

    if not tools:
        return False
    return bool(_LIVE_ACTION_RE.search(input_text) and _LIVE_TARGET_RE.search(input_text))


def _execute_agent_session_message(
    session_id: str, request: AgentSessionMessageRequest, *, wait_for_lock: bool,
) -> dict[str, Any]:
    try:
        session = _agent_sessions().require(session_id)
    except KeyError:
        raise HTTPException(404, "agent session not found") from None
    # Canonicalize prefixes before caching and locking so a short ID and the
    # full ID cannot enter the same sequential memory concurrently.
    session_id = session.id
    cached = _agent_sessions().cached_response(session_id, request.request_id)
    if cached is not None:
        return {"content": cached, "session": session.public(), "cached": True}
    incoming = request.prompt.strip()
    if not incoming:
        raise HTTPException(400, "agent input cannot be empty")
    if len(incoming) > 100_000:
        raise HTTPException(400, "agent input is limited to 100000 characters")
    lock = _agent_lock(session_id)
    if wait_for_lock:
        _emit_run_progress("session_wait", "Waiting for earlier work in this conversation.")
        lock.acquire()
    elif not lock.acquire(blocking=False):
        raise HTTPException(409, "agent session is busy with another sequential call")

    try:
        _emit_run_progress("preparing", "Preparing memory, device context, and coding tools.")
        prompt_parts = []
        instructions = _agent_sessions().instructions(session_id)
        if instructions:
            prompt_parts.append(f"[Permanent instructions for {session.name}]\n{instructions}")
        memory = _agent_sessions().memory_prompt(session_id)
        if memory:
            prompt_parts.append(f"[Memory cache: {session.memory}]\n{memory}")
        # Device awareness is best-effort: agents remain usable on hosts where
        # Tailscale is not installed or its daemon is temporarily unavailable.
        try:
            from herald.tailscale import agent_context
            prompt_parts.append(f"[Device and SSH context]\n{agent_context()}")
        except Exception:  # noqa: BLE001
            pass
        prompt_parts.append(f"[Current input]\n{incoming}")
        prompt = "\n\n".join(prompt_parts)
        if session.agentic:
            instances = _catalog_scope(session.project, session.part)
            tools, discovery_errors = _callable_tool_catalog(instances)
            policy = get_policy(session.mode)
            require_tool = _requires_live_tool(incoming, tools)
            _emit_run_progress(
                "tools_ready", f"Loaded {len(tools)} tools for this coding scope.",
                tools=len(tools), discovery_errors=len(discovery_errors),
                scope=f"{session.project}/{session.part}" if session.project else "global",
            )
            inherited_callback = _run_progress.get()

            def harness_event(kind: str, data: dict[str, Any]) -> None:
                if not inherited_callback:
                    return
                labels = {
                    "model_iteration": f"Model iteration {data.get('iteration')} is running.",
                    "model_finished": "The active model produced a final response.",
                    "tool_started": f"Running tool {data.get('tool')}.",
                    "tool_finished": f"Tool {data.get('tool')} finished.",
                    "delegation_started": f"Delegating work to {data.get('model')}.",
                    "delegation_finished": f"Delegated work from {data.get('model')} returned.",
                    "actions_completed": f"Completed {data.get('count')} requested action(s).",
                    "grounding_required": "Requiring a live tool result before accepting an answer.",
                }
                inherited_callback(kind, labels.get(kind, kind.replace("_", " ").title()), data)

            # Escalation ladder: start on the session's own (usually cheap/free)
            # policy tier, and only climb to a more expensive one if that tier
            # comes back with nothing usable -- e.g. a free model that only
            # lists a directory and never actually reads or edits the file it
            # was asked to fix. Climbing is a last resort, not a first choice,
            # so each tier is only tried once and the ladder is capped at
            # "quality" (never escalates as far as burning a CLI subscription
            # session on "local", which has nowhere cheaper to go anyway).
            _ESCALATION_LADDER = ("efficiency", "balanced", "quality")
            tiers_to_try = [policy.name] if policy.name not in _ESCALATION_LADDER else [
                tier for tier in _ESCALATION_LADDER if _ESCALATION_LADDER.index(tier) >= _ESCALATION_LADDER.index(policy.name)
            ]
            # Non-empty text is not the same as done: a model can confidently
            # answer a question it never actually investigated (a hallucinated
            # file path, a generic example instead of the real fix). When the
            # request reads as wanting an actual code change, require evidence
            # of one -- a successful write_file/edit_file call -- before
            # accepting the tier's result, not just any text back.
            wants_edit = bool(re.search(
                r"\b(fix|patch|edit|modify|change|update|implement|apply|write|correct)\b.*"
                r"\b(code|file|function|bug|vulnerability|script|repo|module)\b",
                incoming, re.IGNORECASE,
            ))
            result: dict[str, Any] = {}
            for attempt, tier_name in enumerate(tiers_to_try):
                tier_policy = get_policy(tier_name)
                if attempt > 0:
                    _emit_run_progress(
                        "escalating",
                        f"'{tiers_to_try[attempt - 1]}' didn't produce a usable result; "
                        f"escalating to '{tier_name}'.",
                        from_tier=tiers_to_try[attempt - 1], to_tier=tier_name,
                    )
                result = run_harness_agent(
                    _execute_tolerant, session.model, prompt,
                    models=model_catalog(
                        registry.list_all(enabled_only=True), tier_policy, exclude=session.model,
                    ),
                    tools=compact_tools(tools, tier_policy),
                    call_tool_fn=lambda name, arguments: _execute_catalog_tool(
                        name, arguments, tools, discovery_errors),
                    # Raised from 8/16/24: polling a run_command_background job
                    # to completion (check_command) costs an iteration per
                    # poll, on top of the edit-build-read-error-edit cycle
                    # itself, so a real coding task can legitimately need more.
                    limits=HarnessLimits(max_depth=2, max_parallel=4,
                                         max_iterations=14, max_model_calls=24,
                                         max_tool_calls=40),
                    event_fn=harness_event,
                    # Force grounding on every escalated retry even if the
                    # original request didn't obviously read as needing a
                    # live tool -- an empty first attempt is itself evidence
                    # that grounding was needed and skipped.
                    require_tool=require_tool or attempt > 0,
                )
                has_content = bool(str(result.get("content", "")).strip())
                edited = any(name in (result.get("tools_used") or []) for name in ("write_file", "edit_file"))
                if has_content and (not wants_edit or edited):
                    break
            content = str(result.get("content", ""))
        else:
            try:
                content = str(_execute_full(session.model, prompt)["content"])
            except ExecutionError as exc:
                raise HTTPException(exc.status_code, exc.detail) from None
        updated = _agent_sessions().append(
            session_id, incoming, content, request_id=request.request_id,
        )
        response = {"content": content, "session": updated.public(), "cached": False}
        if session.agentic:
            response["trace"] = result.get("trace", [])
            response["budget"] = result.get("budget", {})
        return response
    finally:
        lock.release()


def _run_agent_message(run_id: str, session_id: str,
                       request: AgentSessionMessageRequest) -> None:
    store = _agent_runs()

    def progress(kind: str, message: str, data: dict[str, Any]) -> None:
        store.event(run_id, kind, message, data)

    store.mark_running(run_id)
    store.event(run_id, "started", "Herald started this task in the background.")
    token = _run_progress.set(progress)
    try:
        response = _execute_agent_session_message(
            session_id, request, wait_for_lock=True,
        )
        store.complete(run_id, response)
    except HTTPException as exc:
        store.fail(run_id, str(exc.detail))
    except Exception as exc:  # keep background failures inspectable after disconnect
        store.fail(run_id, f"{type(exc).__name__}: {exc}")
    finally:
        _run_progress.reset(token)


@app.post("/agent-sessions/{session_id}/runs", status_code=202)
def create_agent_run(session_id: str, request: AgentSessionMessageRequest) -> dict[str, Any]:
    try:
        session = _agent_sessions().require(session_id)
    except KeyError:
        raise HTTPException(404, "agent session not found") from None
    incoming = request.prompt.strip()
    if not incoming:
        raise HTTPException(400, "agent input cannot be empty")
    if len(incoming) > 100_000:
        raise HTTPException(400, "agent input is limited to 100000 characters")
    run, created = _agent_runs().create(
        session.id, incoming, request_id=request.request_id,
    )
    if created:
        _agent_run_executor.submit(_run_agent_message, run.id, session.id, request)
    return {"run": _agent_runs().inspect(run.id), "reused": not created}


@app.get("/agent-runs/{run_id}")
def inspect_agent_run(run_id: str) -> dict[str, Any]:
    try:
        return {"run": _agent_runs().inspect(run_id)}
    except KeyError:
        raise HTTPException(404, "agent run not found") from None


@app.get("/agent-sessions/{session_id}/runs")
def list_agent_runs(session_id: str, limit: int = 50) -> dict[str, Any]:
    try:
        session = _agent_sessions().require(session_id)
    except KeyError:
        raise HTTPException(404, "agent session not found") from None
    return {
        "runs": [
            _agent_runs().inspect(run.id)
            for run in _agent_runs().list(session_id=session.id, limit=limit)
        ]
    }


@app.post("/agent-sessions/{session_id}/reset")
def reset_agent_session(session_id: str, request: AgentSessionResetRequest) -> dict[str, Any]:
    try:
        return {"session": _agent_sessions().reset(
            session_id, force=request.force, instructions=request.instructions,
        ).public()}
    except KeyError:
        raise HTTPException(404, "agent session not found") from None


@app.get("/agent-sessions/{session_id}/export")
def export_agent_session(session_id: str) -> dict[str, Any]:
    try:
        return _agent_sessions().export(session_id)
    except KeyError:
        raise HTTPException(404, "agent session not found") from None


@app.get("/mobile/api/conversations")
def mobile_conversations(limit: int = 100) -> dict[str, Any]:
    """Return only chat sessions created by the mobile Herald client."""
    sessions = [
        session.public()
        for session in _agent_sessions().list(min(max(limit, 1), 500))
        if session.name.startswith("mobile_")
    ]
    return {"conversations": sessions}


@app.get("/mobile/api/conversations/{session_id}")
def mobile_conversation(session_id: str) -> dict[str, Any]:
    """Expose the displayable transcript without leaking the memory ledger."""
    try:
        exported = _agent_sessions().export(session_id)
    except KeyError:
        raise HTTPException(404, "agent session not found") from None
    session = exported["session"]
    if not str(session.get("name", "")).startswith("mobile_"):
        raise HTTPException(404, "mobile conversation not found")
    messages: list[dict[str, str]] = []
    for turn in exported.get("state", {}).get("turns", []):
        messages.append({"role": "user", "content": str(turn.get("input", ""))})
        messages.append({"role": "assistant", "content": str(turn.get("output", ""))})
    return {"session": session, "messages": messages}


@app.post("/agent-sessions/{session_id}/import")
def import_agent_session(session_id: str, request: AgentSessionImportRequest) -> dict[str, Any]:
    try:
        return {"session": _agent_sessions().import_state(session_id, request.data).public()}
    except KeyError:
        raise HTTPException(404, "agent session not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.delete("/agent-sessions/{session_id}")
def delete_agent_session(session_id: str) -> dict[str, Any]:
    try:
        resolved = _agent_sessions().require(session_id).id
    except KeyError:
        raise HTTPException(404, "agent session not found") from None
    deleted_runs = _agent_runs().delete_for_session(resolved)
    _agent_sessions().delete(resolved)
    return {"deleted": resolved, "deleted_runs": deleted_runs}


# ---------------------------------------------------------------------------
# Declarative model flows -- authored through SDK/CLI and operable by the console
# ---------------------------------------------------------------------------

class FlowRequest(BaseModel):
    spec: dict[str, Any] | str
    input: str = ""
    project: str | None = None
    part: str | None = None


def _flow_spec(value: dict[str, Any] | str) -> FlowSpec:
    if isinstance(value, str):
        import yaml
        parsed = yaml.safe_load(value)
        if not isinstance(parsed, dict):
            raise ValueError("flow YAML must contain an object")
        value = parsed
    return FlowSpec.from_dict(value)


@app.get("/routing/policies")
def list_routing_policies() -> dict[str, Any]:
    return {"policies": [policy.to_dict() for policy in POLICIES.values()]}


@app.post("/flows/validate")
def validate_flow(request: FlowRequest) -> dict[str, Any]:
    try:
        spec = _flow_spec(request.spec)
        policy = get_policy(spec.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {
        "valid": True, "flow": spec.to_dict(), "policy": policy.to_dict(),
        "stages": len(spec.stages), "agents": len(spec.agents),
    }


def _execute_flow_request(
    request: FlowRequest, *, state: dict[str, Any] | None = None,
    checkpoint=None,
) -> dict[str, Any]:
    if bool(request.project) != bool(request.part):
        raise HTTPException(400, "project and part must be provided together")
    try:
        spec = _flow_spec(request.spec)
        policy = get_policy(spec.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    instances = _catalog_scope(request.project, request.part)
    tools, discovery_errors = _callable_tool_catalog(instances)
    limits = HarnessLimits(
        max_depth=spec.limits.get("max_depth", 2),
        max_parallel=spec.limits.get("max_parallel", 4),
        # Raised from 4/8/12: a real edit-build-read-error-edit cycle,
        # especially one that polls a run_command_background job to
        # completion, easily costs more than 4 iterations even for a
        # single successful task. Still overridable per-flow via spec.limits.
        max_iterations=spec.limits.get("max_iterations", 10),
        max_model_calls=spec.limits.get("max_model_calls", 16),
        max_tool_calls=spec.limits.get("max_tool_calls", 24),
    ).bounded()
    flow_budget = ExecutionBudget(limits)
    if state and isinstance(state.get("budget"), dict):
        flow_budget.model_calls = min(int(state["budget"].get("model_calls", 0)), limits.max_model_calls)
        flow_budget.tool_calls = min(int(state["budget"].get("tool_calls", 0)), limits.max_tool_calls)

    def call_agent(agent: AgentSpec, prompt: str, mode: str) -> dict[str, Any]:
        active_policy = get_policy(agent.model if agent.model in POLICIES else mode)
        try:
            selected_model = (
                automatic_model(registry.list_all(), active_policy, prompt=prompt)
                if agent.model == "auto"
                else automatic_model(registry.list_all(), active_policy)
                if agent.model in POLICIES else agent.model
            )
        except ValueError as exc:
            return {"content": f"[flow agent {agent.name} failed: {exc}]", "error": str(exc)}
        if not agent.tools:
            if not flow_budget.claim_model():
                return {
                    "content": "Herald stopped because the flow model-call budget was exhausted.",
                    "model": selected_model, "memory": agent.memory, "agentic": False,
                    "error": "model-call budget exhausted",
                }
            return {
                "content": _execute_tolerant(selected_model, prompt),
                "model": selected_model, "memory": agent.memory, "agentic": False,
            }
        result = run_harness_agent(
            _execute_tolerant, selected_model, prompt,
            models=model_catalog(
                registry.list_all(enabled_only=True), active_policy, exclude=selected_model,
            ),
            tools=compact_tools(tools, active_policy),
            call_tool_fn=lambda name, arguments: _execute_catalog_tool(
                name, arguments, tools, discovery_errors,
            ),
            limits=limits,
            budget=flow_budget,
        )
        return {
            "content": result["content"], "model": selected_model,
            "memory": agent.memory, "agentic": True,
            "orchestration_trace": result["trace"], "budget": result["budget"],
        }

    def save_checkpoint(value: dict[str, Any]) -> None:
        value = {**value, "budget": flow_budget.snapshot()}
        if checkpoint:
            checkpoint(value)

    result = FlowRunner(call_agent).run(
        spec, request.input, state=state, checkpoint=save_checkpoint if checkpoint else None,
    )
    result["budget"] = flow_budget.snapshot()
    result["limits"] = {
        "max_model_calls": limits.max_model_calls,
        "max_tool_calls": limits.max_tool_calls,
        "max_depth": limits.max_depth,
        "max_parallel": limits.max_parallel,
    }
    return result


@app.post("/flows/run")
def run_flow(request: FlowRequest) -> dict[str, Any]:
    return _execute_flow_request(request)


_flow_run_store: FlowRunStore | None = None
_event_bus: EventBus | None = None


def _flow_runs() -> FlowRunStore:
    global _flow_run_store
    if _flow_run_store is None:
        _flow_run_store = FlowRunStore()
    return _flow_run_store


def _events() -> EventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


@app.get("/flow-runs")
def list_flow_runs(limit: int = 50) -> dict[str, Any]:
    return {"runs": [run.public() for run in _flow_runs().list(min(max(limit, 1), 200))]}


@app.post("/flow-runs")
def create_flow_run(request: FlowRequest) -> dict[str, Any]:
    try:
        spec = _flow_spec(request.spec)
        get_policy(spec.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    store = _flow_runs()
    run = store.create(
        spec.to_dict(), request.input, project=request.project, part=request.part,
    )
    store.mark_running(run.id)
    _events().emit("flow.run.started", run.public())

    def checkpoint_run(state: dict[str, Any]) -> None:
        current = store.save_checkpoint(run.id, state)
        _events().emit("flow.run.checkpoint", current.public())

    try:
        result = _execute_flow_request(
            FlowRequest(
                spec=spec.to_dict(), input=request.input,
                project=request.project, part=request.part,
            ),
            checkpoint=checkpoint_run,
        )
        completed = store.complete(run.id, result)
        _events().emit("flow.run.completed", completed.public())
    except Exception as exc:  # checkpoint remains resumable
        failed = store.fail(run.id, f"flow execution failed: {type(exc).__name__}")
        _events().emit("flow.run.failed", failed.public())
        raise HTTPException(500, {"run": failed.public(), "error": "flow execution failed"}) from None
    return {"run": completed.public(), "result": result}


@app.get("/flow-runs/{run_id}")
def get_flow_run(run_id: str) -> dict[str, Any]:
    run = _flow_runs().get(run_id)
    if run is None:
        raise HTTPException(404, "flow run not found")
    return {"run": run.public()}


@app.get("/flow-runs/{run_id}/result")
def get_flow_run_result(run_id: str) -> dict[str, Any]:
    try:
        run = _flow_runs().get(run_id)
        result = _flow_runs().result(run_id)
    except KeyError:
        raise HTTPException(404, "flow run not found") from None
    if result is None:
        raise HTTPException(409, "flow run has no completed result")
    return {"run": run.public(), "result": result}


@app.post("/flow-runs/{run_id}/resume")
def resume_flow_run(run_id: str) -> dict[str, Any]:
    store = _flow_runs()
    run = store.get(run_id)
    if run is None:
        raise HTTPException(404, "flow run not found")
    if run.status == "completed":
        return {"run": run.public(), "result": store.result(run_id)}
    definition = store.definition(run_id)
    state = store.checkpoint(run_id)
    store.mark_running(run_id)
    _events().emit("flow.run.resumed", store.get(run_id).public())

    def checkpoint_run(value: dict[str, Any]) -> None:
        current = store.save_checkpoint(run_id, value)
        _events().emit("flow.run.checkpoint", current.public())

    try:
        result = _execute_flow_request(
            FlowRequest(
                spec=definition["spec"], input=definition["input"],
                project=definition.get("project"), part=definition.get("part"),
            ),
            state=state, checkpoint=checkpoint_run,
        )
        completed = store.complete(run_id, result)
        _events().emit("flow.run.completed", completed.public())
    except Exception as exc:
        failed = store.fail(run_id, f"flow resume failed: {type(exc).__name__}")
        _events().emit("flow.run.failed", failed.public())
        raise HTTPException(500, {"run": failed.public(), "error": "flow resume failed"}) from None
    return {"run": completed.public(), "result": result}


class HookCreateRequest(BaseModel):
    name: str
    pattern: str
    transport: str
    config: dict[str, Any]
    secret_ref: str | None = None
    enabled: bool = True


@app.get("/events")
def list_events(limit: int = 100, topic: str | None = None) -> dict[str, Any]:
    return {"events": _events().list_events(min(max(limit, 1), 500), topic=topic)}


@app.get("/hooks")
def list_hooks() -> dict[str, Any]:
    return {"hooks": _events().list_hooks()}


@app.post("/hooks")
def create_hook(request: HookCreateRequest) -> dict[str, Any]:
    try:
        hook = _events().register_hook(
            request.name, request.pattern, request.transport, request.config,
            secret_ref=request.secret_ref, enabled=request.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return hook


@app.delete("/hooks/{name}")
def delete_hook(name: str) -> dict[str, Any]:
    if not _events().remove_hook(name):
        raise HTTPException(404, "hook not found")
    return {"removed": name}


# ---------------------------------------------------------------------------
# Project / Part / ToolInstance REST API
# Mirrors the MCP tools in mcp_router.py but over HTTP for non-MCP clients.
# ---------------------------------------------------------------------------

# -- ToolInstance --

class ToolInstanceCreateRequest(BaseModel):
    name: str
    transport: str
    config: dict[str, Any]
    description: str = ""
    tags: list[str] = []
    package_name: str = ""
    version: str = "unversioned"
    scope: str = "global"
    project: str | None = None
    source: dict[str, Any] = {}
    isolation_key: str = ""


class MCPGroupCreateRequest(BaseModel):
    name: str
    description: str = ""


class MCPGroupToolRequest(BaseModel):
    tool: str
    allowed_tools: list[str] | None = None
    alias: str | None = None
    position: int | None = None


class MCPGroupBindingRequest(BaseModel):
    target_type: str
    target_key: str = "*"
    position: int | None = None


class MCPAccessRunRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}
    groups: list[str] | None = None
    project: str | None = None
    part: str | None = None
    profile: str | None = None


_COMPLEXITY_POLICY_SUGGESTION: dict[str, str] = {
    "trivial": "efficiency",
    "standard": "efficiency",
    "complex": "quality",
    "large_context": "quality",
    "local": "local",
}


@app.get("/routing/suggest-policy")
def http_suggest_routing_policy(prompt: str) -> dict[str, Any]:
    """Classify a sample prompt's complexity and suggest a routing policy
    for it -- used by the pi interface's `/model` command (bare, no
    argument) to passively suggest a policy based on the user's most
    recent input instead of requiring them to already know which one
    fits. Never overrides the user's explicit choice; purely advisory."""
    from herald.routing_policy import classify_task_complexity
    complexity = classify_task_complexity(prompt)
    return {
        "complexity": complexity,
        "suggested_policy": _COMPLEXITY_POLICY_SUGGESTION.get(complexity, "balanced"),
    }


class RoutingPolicyCreateRequest(BaseModel):
    name: str
    description: str = ""
    automatic_order: list[str]
    delegate_types: list[str]
    free_only: bool = False
    compact_tool_catalog: bool = False
    tool_bridge: str | None = None
    project: str | None = None


@app.post("/routing-policies")
def http_register_routing_policy(request: RoutingPolicyCreateRequest) -> dict[str, Any]:
    if request.name.lower() in POLICIES:
        raise HTTPException(400, f"'{request.name}' is a built-in policy name and cannot be overridden")
    valid_types = {"api_key", "local_model", "cli", "browser_session"}
    unknown = set(request.delegate_types) - valid_types
    if unknown:
        raise HTTPException(400, f"unknown delegate_types: {sorted(unknown)} (valid: {sorted(valid_types)})")
    from herald.router.custom_policies import get_store
    try:
        get_store().upsert(
            request.name.lower(), description=request.description,
            automatic_order=request.automatic_order, delegate_types=request.delegate_types,
            free_only=request.free_only, compact_tool_catalog=request.compact_tool_catalog,
            tool_bridge=request.tool_bridge, project=request.project,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"name": request.name.lower(), "status": "registered"}


class LocalModelIdleConfigRequest(BaseModel):
    model_name: str
    idle_unload_minutes: float | None = None


@app.post("/local-model-idle-config")
def http_register_idle_unload_config(request: LocalModelIdleConfigRequest) -> dict[str, Any]:
    from herald.router.idle_unload import get_store
    get_store().upsert(request.model_name, request.idle_unload_minutes)
    return {"model_name": request.model_name, "idle_unload_minutes": request.idle_unload_minutes, "status": "registered"}


class ConnectorRegisterRequest(BaseModel):
    name: str
    type: str
    base_url: str
    auth: str = "none"
    capability_tags: list[str] = []


@app.post("/connectors")
def http_register_connector(request: ConnectorRegisterRequest) -> dict[str, Any]:
    """Declarative connector manifest entry from router.yaml's `connectors`
    block -- dispatches to the matching auto-discovery function in
    herald/connectors.py so a new external endpoint can be dropped in
    without editing that file directly."""
    from herald import connectors as connector_mod

    secret_ref = None
    if request.auth and request.auth.lower() != "none":
        secret_ref = request.auth if ":" in request.auth else f"env:{request.auth}"
        try:
            from herald.router.account_registry import validate_secret_ref
            validate_secret_ref(secret_ref)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    caps = {tag: True for tag in request.capability_tags} if request.capability_tags else None

    try:
        if request.type == "ollama":
            result = connector_mod.connect_ollama(request.base_url, pool_name=request.name)
        elif request.type == "lmstudio":
            result = connector_mod.connect_lmstudio(request.base_url, pool_name=request.name)
        elif request.type == "openrouter":
            if not secret_ref:
                raise HTTPException(400, f"connector '{request.name}': type 'openrouter' requires an env:, keyring:, or vault: auth reference")
            result = connector_mod.connect_openrouter(secret_ref, pool_name=request.name)
        elif request.type in ("openai-compatible", "custom-openapi"):
            result = connector_mod.connect_openai_compatible(
                request.name, request.base_url, secret_ref=secret_ref,
                pool_name=request.name, capabilities=caps,
            )
        else:
            raise HTTPException(400, f"connector '{request.name}': unknown type '{request.type}'")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 -- surface as a normal registration error, not a 500
        raise HTTPException(400, f"connector '{request.name}' registration failed: {exc}") from None

    if result.get("status") == "error":
        raise HTTPException(400, f"connector '{request.name}': {result.get('error')}")
    return {"name": request.name, "status": "registered", **result}


# ---------------------------------------------------------------------------
# Scheduler HTTP surface -- needed so non-Python clients (e.g. the pi
# interactive-shell extension) can manage schedules without touching
# scheduler.py's SQLite store directly.
# ---------------------------------------------------------------------------

def _schedule_to_dict(s) -> dict[str, Any]:
    return {
        "id": s.id, "name": s.name, "trigger_type": s.trigger_type,
        "cron_expression": s.cron_expression, "event_type": s.event_type,
        "event_filter": s.event_filter, "action_type": s.action_type,
        "project": s.project, "part": s.part, "prompt": s.prompt,
        "flow_spec_json": s.flow_spec_json, "enabled": s.enabled,
        "created_at": s.created_at, "last_fired_at": s.last_fired_at,
        "last_status": s.last_status, "model": s.model, "agentic": s.agentic,
    }


@app.get("/schedules")
def http_list_schedules(enabled_only: bool = False) -> dict[str, Any]:
    from herald.router.scheduler import ScheduleStore
    schedules = ScheduleStore().list_all(enabled_only=enabled_only)
    return {"schedules": [_schedule_to_dict(s) for s in schedules]}


class ScheduleCreateRequest(BaseModel):
    name: str
    trigger_type: str
    action_type: str
    cron_expression: str | None = None
    event_type: str | None = None
    event_filter: dict[str, Any] | None = None
    project: str | None = None
    part: str | None = None
    prompt: str | None = None
    flow_spec_json: str | None = None
    enabled: bool = True
    model: str | None = None
    agentic: bool = True
    python_target: str | None = None
    python_kwargs: dict[str, Any] | None = None


@app.post("/schedules")
def http_add_schedule(request: ScheduleCreateRequest) -> dict[str, Any]:
    from herald.router.scheduler import ScheduleStore
    store = ScheduleStore()
    try:
        schedule_id = store.add(
            request.name, trigger_type=request.trigger_type, action_type=request.action_type,
            cron_expression=request.cron_expression, event_type=request.event_type,
            event_filter=request.event_filter, project=request.project, part=request.part,
            prompt=request.prompt, flow_spec_json=request.flow_spec_json, enabled=request.enabled,
            model=request.model, agentic=request.agentic,
            python_target=request.python_target, python_kwargs=request.python_kwargs,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"id": schedule_id, "name": request.name, "status": "created"}


@app.get("/schedules/{name}/runs")
def http_schedule_runs(name: str, limit: int = 50) -> dict[str, Any]:
    from herald.router.scheduler import ScheduleStore
    store = ScheduleStore()
    if store.get(name) is None:
        raise HTTPException(404, f"no schedule named {name!r}")
    return {"name": name, "runs": store.list_runs(name, limit=limit)}


@app.delete("/schedules/{name}")
def http_remove_schedule(name: str) -> dict[str, Any]:
    from herald.router.scheduler import ScheduleStore
    ScheduleStore().remove(name)
    return {"name": name, "status": "removed"}


@app.post("/schedules/{name}/enable")
def http_enable_schedule(name: str) -> dict[str, Any]:
    from herald.router.scheduler import ScheduleStore
    ScheduleStore().set_enabled(name, True)
    return {"name": name, "enabled": True}


@app.post("/schedules/{name}/disable")
def http_disable_schedule(name: str) -> dict[str, Any]:
    from herald.router.scheduler import ScheduleStore
    ScheduleStore().set_enabled(name, False)
    return {"name": name, "enabled": False}


@app.post("/schedules/{name}/run")
async def http_run_schedule(name: str) -> dict[str, Any]:
    from herald.router.scheduler import Scheduler, ScheduleStore
    store = ScheduleStore()
    sched = store.get(name)
    if sched is None:
        raise HTTPException(404, f"no schedule named {name!r}")
    Scheduler(store)._fire_in_background(sched)
    return {"name": name, "status": "firing"}


def _admin_control():
    """Import the dev-build-only Admin control module, or 404 if it isn't present.

    The private Admin review console (admin_control/admin_state/admin_workspace,
    admin_loop) ships only in the maintainer's development build, never in the
    published `herald-ai` package, so these routes degrade to 404 instead of a
    raw import traceback when the module is absent.
    """
    try:
        from herald.router import admin_control
    except ImportError:
        raise HTTPException(404, "Admin console is a development-build-only feature") from None
    return admin_control


@app.get("/admin/status")
def http_admin_status() -> dict[str, Any]:
    return _admin_control().status()


@app.get("/admin/reviews")
def http_admin_reviews() -> dict[str, Any]:
    return _admin_control().review_inbox()


@app.post("/admin/stop")
def http_admin_stop() -> dict[str, Any]:
    return _admin_control().request_stop()


@app.post("/admin/resume")
def http_admin_resume() -> dict[str, Any]:
    return _admin_control().clear_stop()


class AdminGoalRequest(BaseModel):
    title: str
    instructions: str
    priority: int = 100


@app.post("/admin/goals", status_code=201)
def http_admin_inject_goal(request: AdminGoalRequest) -> dict[str, Any]:
    try:
        return _admin_control().inject_goal(request.title, request.instructions, priority=request.priority)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/admin/promote")
def http_admin_promote() -> dict[str, Any]:
    try:
        return _admin_control().promote()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/admin/reviews/{dispatch_id}/approve")
def http_admin_approve(dispatch_id: str) -> dict[str, Any]:
    try:
        return _admin_control().approve(dispatch_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


class AdminDenyRequest(BaseModel):
    reason: str


@app.post("/admin/reviews/{dispatch_id}/deny")
def http_admin_deny(dispatch_id: str, request: AdminDenyRequest) -> dict[str, Any]:
    try:
        return _admin_control().deny(dispatch_id, reason=request.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


class AdminPublishRequest(BaseModel):
    target: str = "pypi"
    version: str | None = None


@app.post("/admin/publish", status_code=202)
def http_admin_publish(request: AdminPublishRequest) -> dict[str, Any]:
    try:
        return _admin_control().request_publish(target=request.target, version=request.version)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


# ---------------------------------------------------------------------------
# Approval-gate HTTP surface.
# ---------------------------------------------------------------------------

class ApprovalAuditRequest(BaseModel):
    tool_name: str
    args: dict[str, Any] = {}
    risk_tier: str
    capability_ceiling: str
    decision: str


@app.post("/approvals/audit")
def http_audit_interactive_approval(request: ApprovalAuditRequest) -> dict[str, str]:
    """Persist decisions made synchronously by pi's in-process tool hook."""
    from herald.router.approval_gate import get_store
    get_store().log(
        tool_name=request.tool_name, args=request.args,
        risk_tier=request.risk_tier, ceiling=request.capability_ceiling,
        decision=request.decision,
    )
    return {"status": "logged"}

def _approval_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "token": row["token"], "tool_name": row["tool_name"],
        "args": json.loads(row["args_json"]), "preview": row["preview"],
        "risk_tier": row["risk_tier"], "requested_at": row["requested_at"],
        "decided_at": row["decided_at"], "decision": row["decision"],
    }


@app.get("/approvals/pending")
def http_list_pending_approvals() -> dict[str, Any]:
    from herald.router.approval_gate import get_store
    return {"pending": [_approval_to_dict(r) for r in get_store().list_pending()]}


@app.get("/approvals/{token}")
def http_get_approval(token: str) -> dict[str, Any]:
    from herald.router.approval_gate import get_store
    row = get_store().get_pending(token)
    if row is None:
        raise HTTPException(404, "approval token not found")
    return _approval_to_dict(row)


# Maps a gated coding_tools.py tool name to the post-gate helper that
# actually performs the operation (added alongside the gate itself so an
# approval can be executed without re-triggering gate_call).
_APPROVAL_EXECUTORS: dict[str, Any] = {}


def _get_approval_executors() -> dict[str, Any]:
    if not _APPROVAL_EXECUTORS:
        from herald import coding_tools as ct
        _APPROVAL_EXECUTORS.update({
            "write_file": lambda args: ct._do_write_file(args["path"], args["content"]),
            "edit_file": lambda args: ct._do_edit_file(
                args["path"], args["old_content"], args["new_content"], args.get("occurrence", 1),
            ),
            "run_command": lambda args: ct._do_run_command(
                args["command"], args.get("cwd", "."), args.get("timeout", 60), args.get("env_extra"),
            ),
        })
    return _APPROVAL_EXECUTORS


@app.post("/approvals/{token}/approve")
def http_approve(token: str) -> dict[str, Any]:
    from herald.router.approval_gate import approve
    executors = _get_approval_executors()
    row = None
    try:
        from herald.router.approval_gate import get_store
        row = get_store().get_pending(token)
        if row is None:
            raise HTTPException(404, "approval token not found or already decided")
        executor = executors.get(row["tool_name"])
        if executor is None:
            raise HTTPException(400, f"no approval executor registered for tool '{row['tool_name']}'")
        result = approve(token, executor)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    return {"token": token, "status": "approved", "result": result}


@app.post("/approvals/{token}/deny")
def http_deny_approval(token: str) -> dict[str, Any]:
    from herald.router.approval_gate import deny
    try:
        deny(token)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    return {"token": token, "status": "denied"}


# ---------------------------------------------------------------------------
# Capability-drafting HTTP surface. Approving here never auto-deploys --
# matches the existing CLI behavior of printing the code for manual review.
# ---------------------------------------------------------------------------

def _proposal_to_dict(p, *, full: bool = False) -> dict[str, Any]:
    base = {
        "id": p.id, "gap_type": p.gap_type, "gap_details": p.gap_details,
        "sandbox_ok": p.sandbox_ok, "risk_level": p.risk_level,
        "status": p.status, "created_at": p.created_at, "decided_at": p.decided_at,
    }
    if full:
        base["draft_source"] = p.draft_source
        base["sandbox_output"] = p.sandbox_output
        base["risk_findings"] = p.risk_findings
    return base


@app.get("/capabilities/proposals")
def http_list_capability_proposals() -> dict[str, Any]:
    from herald.router.capability_drafting import get_store
    return {"proposals": [_proposal_to_dict(p) for p in get_store().list_pending()]}


@app.get("/capabilities/proposals/{proposal_id}")
def http_get_capability_proposal(proposal_id: int) -> dict[str, Any]:
    from herald.router.capability_drafting import get_store
    proposal = get_store().get(proposal_id)
    if proposal is None:
        raise HTTPException(404, "proposal not found")
    return _proposal_to_dict(proposal, full=True)


@app.post("/capabilities/proposals/{proposal_id}/approve")
def http_approve_capability_proposal(proposal_id: int) -> dict[str, Any]:
    from herald.router.capability_drafting import get_store
    store = get_store()
    proposal = store.decide(proposal_id, "approved")
    if proposal is None:
        raise HTTPException(404, "proposal not found or already decided")
    return {
        "id": proposal_id, "status": "approved", "auto_deployed": False,
        "draft_source": proposal.draft_source,
        "note": "Not auto-deployed. Review draft_source and wire it into coding_tools.py manually.",
    }


@app.post("/capabilities/proposals/{proposal_id}/deny")
def http_deny_capability_proposal(proposal_id: int) -> dict[str, Any]:
    from herald.router.capability_drafting import get_store
    proposal = get_store().decide(proposal_id, "denied")
    if proposal is None:
        raise HTTPException(404, "proposal not found or already decided")
    return {"id": proposal_id, "status": "denied"}


# ---------------------------------------------------------------------------
# Event-bus quiet mode + live SSE stream.
# ---------------------------------------------------------------------------

@app.get("/event-bus/quiet-mode")
def http_get_quiet_mode() -> dict[str, Any]:
    from herald.router import event_bus
    return {"quiet_mode": event_bus.quiet_mode()}


class QuietModeRequest(BaseModel):
    enabled: bool


@app.post("/event-bus/quiet-mode")
def http_set_quiet_mode(request: QuietModeRequest) -> dict[str, Any]:
    from herald.router import event_bus
    event_bus.set_quiet_mode(request.enabled)
    return {"quiet_mode": event_bus.quiet_mode()}


@app.get("/event-bus/stream")
async def http_event_bus_stream() -> StreamingResponse:
    """SSE stream of live event-bus events. Registers a temporary sink for
    the lifetime of the connection; unregisters on client disconnect. This
    is what lets a client (e.g. the pi extension) show a live approval
    prompt or notification the moment it fires, instead of polling."""
    from herald.router import event_bus

    queue: asyncio.Queue = asyncio.Queue()

    async def _sink(event: event_bus.Event) -> None:
        await queue.put(event)

    event_bus.register_sink(_sink, event_types=None, min_importance=0.0, respect_quiet_mode=False)

    async def _generate():
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event.to_dict())}\n\n"
        finally:
            event_bus.unregister_sink(_sink)

    return StreamingResponse(_generate(), media_type="text/event-stream")


def _ti_to_dict(ti) -> dict[str, Any]:
    return {
        "id": ti.id, "name": ti.name, "description": ti.description,
        "transport": ti.transport, "config": _mask_config(ti.config), "tags": ti.tags,
        "package": ti.package_name, "version": ti.version, "scope": ti.scope,
        "project_id": ti.project_id, "source": ti.source,
        "isolation_key": ti.isolation_key, "alias": ti.alias,
    }


@app.get("/tool-instances")
def http_list_tool_instances(
    scope: str | None = None, project: str | None = None, include_global: bool = False,
) -> dict[str, Any]:
    return {"tool_instances": [_ti_to_dict(t) for t in tool_registry.list_tool_instances(
        scope=scope, project=project, include_global=include_global,
    )]}


@app.post("/tool-instances")
def http_register_tool_instance(request: ToolInstanceCreateRequest) -> dict[str, Any]:
    if request.transport not in ("stdio", "http", "sse"):
        raise HTTPException(400, f"transport must be 'stdio', 'http', or 'sse'")
    try:
        tid = tool_registry.register_tool_instance(
            name=request.name, transport=request.transport, config=request.config,
            description=request.description, tags=request.tags,
            package_name=request.package_name, version=request.version,
            scope=request.scope, project=request.project, source=request.source,
            isolation_key=request.isolation_key,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return _ti_to_dict(tool_registry.get_tool_instance(tid))


@app.delete("/tool-instances/{name}")
def http_delete_tool_instance(name: str) -> dict[str, Any]:
    ok = tool_registry.remove_tool_instance(name)
    if not ok:
        raise HTTPException(404, f"tool instance '{name}' not found")
    return {"removed": name}


@app.get("/tool-instances/{name}/sharing")
def http_tool_sharing(name: str) -> dict[str, Any]:
    """Which parts reference this tool instance?"""
    refs = tool_registry.parts_using_tool(name)
    return {"tool": name, "referenced_by": refs}


# ---------------------------------------------------------------------------
# Controlled MCP groups and shared gateway access
# ---------------------------------------------------------------------------

@app.get("/mcp-groups")
def http_list_mcp_groups() -> dict[str, Any]:
    return {
        "groups": [tool_registry.describe_mcp_group(group.id)
                   for group in tool_registry.list_mcp_groups()]
    }


@app.post("/mcp-groups")
def http_create_mcp_group(request: MCPGroupCreateRequest) -> dict[str, Any]:
    try:
        group_id = tool_registry.register_mcp_group(request.name, request.description)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return tool_registry.describe_mcp_group(group_id)


@app.get("/mcp-groups/{name}")
def http_get_mcp_group(name: str) -> dict[str, Any]:
    value = tool_registry.describe_mcp_group(name)
    if not value:
        raise HTTPException(404, f"MCP group '{name}' not found")
    return value


@app.delete("/mcp-groups/{name}")
def http_delete_mcp_group(name: str) -> dict[str, Any]:
    if not tool_registry.remove_mcp_group(name):
        raise HTTPException(404, f"MCP group '{name}' not found")
    return {"removed": name}


@app.post("/mcp-groups/{name}/tools")
def http_add_mcp_group_tool(name: str, request: MCPGroupToolRequest) -> dict[str, Any]:
    try:
        tool_registry.add_tool_to_mcp_group(
            group=name, tool=request.tool, allowed_tools=request.allowed_tools,
            alias=request.alias, position=request.position,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return tool_registry.describe_mcp_group(name)


@app.delete("/mcp-groups/{name}/tools/{tool}")
def http_remove_mcp_group_tool(name: str, tool: str) -> dict[str, Any]:
    if not tool_registry.remove_tool_from_mcp_group(group=name, tool=tool):
        raise HTTPException(404, f"'{tool}' is not in MCP group '{name}'")
    return tool_registry.describe_mcp_group(name)


@app.post("/mcp-groups/{name}/bindings")
def http_bind_mcp_group(name: str, request: MCPGroupBindingRequest) -> dict[str, Any]:
    try:
        tool_registry.bind_mcp_group(
            group=name, target_type=request.target_type,
            target_key=request.target_key, position=request.position,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return tool_registry.describe_mcp_group(name)


@app.delete("/mcp-groups/{name}/bindings/{target_type}/{target_key:path}")
def http_unbind_mcp_group(name: str, target_type: str, target_key: str) -> dict[str, Any]:
    if not tool_registry.unbind_mcp_group(
        group=name, target_type=target_type, target_key=target_key,
    ):
        raise HTTPException(404, "MCP group binding not found")
    return tool_registry.describe_mcp_group(name)


def _mcp_access_instances(
    *, groups: list[str] | None = None, project: str | None = None,
    part: str | None = None, profile: str | None = None,
):
    try:
        return tool_registry.resolve_mcp_access(
            groups=groups, project=project, part=part, profile=profile,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/mcp-access")
def http_mcp_access(
    groups: str | None = None, project: str | None = None,
    part: str | None = None, profile: str | None = None,
) -> dict[str, Any]:
    selected = [item for item in (groups or "").split(",") if item] or None
    tools, errors = _callable_tool_catalog(_mcp_access_instances(
        groups=selected, project=project, part=part, profile=profile,
    ))
    return {"tools": tools, "discovery_errors": errors}


@app.post("/mcp-access/run")
def http_run_mcp_access_tool(request: MCPAccessRunRequest) -> dict[str, Any]:
    catalog, errors = _callable_tool_catalog(_mcp_access_instances(
        groups=request.groups, project=request.project,
        part=request.part, profile=request.profile,
    ))
    result = _execute_catalog_tool(request.name, request.arguments, catalog, errors)
    if not result.get("ok") and "not found" in result.get("error", ""):
        raise HTTPException(404, result["error"])
    if not result.get("ok") and "ambiguous" in result.get("error", ""):
        raise HTTPException(409, result["error"])
    return result


# ---------------------------------------------------------------------------
# External CLI/plugin inventory and Kapture browser bridge
# ---------------------------------------------------------------------------

KAPTURE_VERSION = "2.6.1"
_g4f_capture_manager: G4FCaptureManager | None = None


def _captures() -> G4FCaptureManager:
    global _g4f_capture_manager
    if _g4f_capture_manager is None:
        _g4f_capture_manager = G4FCaptureManager(
            emit_event=lambda topic, payload: _events().emit(topic, payload),
        )
    return _g4f_capture_manager


@app.get("/integrations/discover")
def integrations_discover() -> dict[str, Any]:
    discovered = discover_cli_integrations()
    registered_names = {tool.name for tool in tool_registry.list_tool_instances()}
    for item in discovered:
        item["imported"] = item["name"] in registered_names
    return {
        "integrations": discovered,
        "registered_tools": [
            {
                "name": tool.name, "transport": tool.transport,
                "package": tool.package_name, "version": tool.version,
                "scope": tool.scope,
            }
            for tool in tool_registry.list_tool_instances()
        ],
    }


class IntegrationPreviewRequest(BaseModel):
    owner: str
    name: str
    source: str | None = None


class IntegrationImportRequest(IntegrationPreviewRequest):
    registry_name: str | None = None
    scope: str = "global"
    project: str | None = None


@app.post("/integrations/preview")
def integration_preview(request: IntegrationPreviewRequest) -> dict[str, Any]:
    try:
        return preview_cli_integration(
            request.owner, request.name, source=request.source,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/integrations/import")
def integration_import(request: IntegrationImportRequest) -> dict[str, Any]:
    if request.scope not in {"global", "project"}:
        raise HTTPException(400, "scope must be global or project")
    if request.scope == "project" and not request.project:
        raise HTTPException(400, "project-scoped imports require project")
    try:
        transport, config, source_metadata = import_cli_integration(
            request.owner, request.name, source=request.source, vault=SecretVault(),
        )
        registry_name = request.registry_name or (
            request.name if request.scope == "global" else f"{request.project}::{request.name}"
        )
        tool_id = tool_registry.register_tool_instance(
            name=registry_name, transport=transport, config=config,
            description=f"Imported from {request.owner}: {request.name}",
            tags=["imported", request.owner, "mcp"], package_name=request.name,
            version="external", scope=request.scope, project=request.project,
            source=source_metadata, isolation_key=f"{request.owner}::{registry_name}",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    result = _ti_to_dict(tool_registry.get_tool_instance(tool_id))
    _events().emit("integration.imported", {
        "owner": request.owner, "source_name": request.name,
        "registry_name": registry_name, "scope": request.scope,
        "project": request.project,
    })
    return result


def _kapture_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 61822), timeout=0.5):
            return True
    except OSError:
        return False


@app.get("/capture/kapture")
def kapture_status() -> dict[str, Any]:
    instance = tool_registry.get_tool_instance("kapture")
    return {
        "registered": instance is not None,
        "bridge_reachable": _kapture_reachable(),
        "version": KAPTURE_VERSION,
        "tool_instance": _ti_to_dict(instance) if instance else None,
        "capture_support": {
            "browser_tabs": True, "network_monitor": True,
            "network_requests": True, "network_bodies": True,
            "automatic_cookie_export": False,
            "automatic_g4f_session_capture": True,
        },
        "note": (
            "Kapture supplies browser/network capture through MCP. Herald can verify and "
            "materialize G4F ChatGPT session artifacts without passing credentials to a model; "
            "generic cross-provider cookie export remains disabled."
        ),
    }


@app.post("/capture/kapture/register")
def register_kapture() -> dict[str, Any]:
    tool_id = tool_registry.register_tool_instance(
        name="kapture", transport="stdio",
        config={"command": ["npx", "-y", f"kapture-mcp@{KAPTURE_VERSION}", "bridge"]},
        description="Shared Chromium browser automation and network capture bridge",
        tags=["browser", "capture", "g4f", "mcp"],
        package_name="kapture-mcp", version=KAPTURE_VERSION, scope="global",
        source={
            "type": "npm", "package": "kapture-mcp",
            "repository": "https://github.com/williamkapke/kapture",
        },
        isolation_key="global::kapture",
    )
    return {
        "registered": True,
        "tool_instance": _ti_to_dict(tool_registry.get_tool_instance(tool_id)),
    }


class G4FCaptureStartRequest(BaseModel):
    account: str
    timeout: float = 120


@app.get("/capture/g4f/accounts")
def g4f_capture_accounts() -> dict[str, Any]:
    try:
        accounts_path = find_accounts_file()
        data = json.loads(accounts_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(404, str(exc)) from None
    accounts = []
    for account in data.get("accounts", []):
        session_path = accounts_path.parent / account.get("dir", "") / "har_and_cookies" / "session.har"
        accounts.append({
            "name": account.get("name"), "email_hint": account.get("email_hint"),
            "plan": account.get("plan"), "enabled": account.get("enabled", True),
            "port": account.get("port"), "session_materialized": session_path.is_file(),
            "session_updated_at": (
                datetime.fromtimestamp(session_path.stat().st_mtime, UTC).isoformat()
                if session_path.is_file() else None
            ),
        })
    return {"accounts": accounts, "source": str(accounts_path)}


@app.get("/capture/g4f/sessions")
def g4f_capture_sessions(limit: int = 50) -> dict[str, Any]:
    return {"sessions": [session.public() for session in _captures().registry.list(min(max(limit, 1), 200))]}


@app.post("/capture/g4f/sessions")
def g4f_capture_start(request: G4FCaptureStartRequest) -> dict[str, Any]:
    if not _kapture_reachable():
        raise HTTPException(503, "Kapture bridge is offline on port 61822")
    try:
        session = _captures().start(request.account, timeout=request.timeout)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise HTTPException(400, str(exc)) from None
    return {
        "session": session.public(),
        "instruction": (
            "Send any short message in the connected ChatGPT tab. Herald is watching "
            "network metadata directly; no model receives the captured credentials."
        ),
    }


@app.get("/capture/g4f/sessions/{session_id}")
def g4f_capture_snapshot(session_id: str) -> dict[str, Any]:
    session = _captures().registry.get(session_id)
    if session is None:
        raise HTTPException(404, "capture session not found")
    return {
        "session": session.public(),
        "requires_g4f_restart": session.status == "materialized",
    }


@app.post("/capture/g4f/sessions/{session_id}/cancel")
def g4f_capture_cancel(session_id: str) -> dict[str, Any]:
    try:
        session = _captures().cancel(session_id)
    except KeyError:
        raise HTTPException(404, "capture session not found") from None
    return {"session": session.public()}


# -- Agent patch staging zone --

class StagingPatchRequest(BaseModel):
    agent_name: str
    diff_content: str
    files_touched: list[str]
    task_id: str | None = None


class StagingAutoMergeRequest(BaseModel):
    patch_ids: list[str]


@app.get("/staging/patches")
def http_list_staging_patches(
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    try:
        patches = _staging().list_patches(status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"patches": [patch.public() for patch in patches]}


@app.post("/staging/patches", status_code=201)
def http_submit_staging_patch(request: StagingPatchRequest) -> dict[str, Any]:
    try:
        patch_id = _staging().submit_patch(
            request.agent_name,
            request.diff_content,
            request.files_touched,
            task_id=request.task_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    patch = _staging().get_patch(patch_id)
    assert patch is not None
    return {"patch": patch.public(), "collisions": _staging().detect_collisions(patch_id)}


@app.post("/staging/patches/{patch_id}/apply")
def http_apply_staging_patch(patch_id: str) -> dict[str, Any]:
    try:
        return _staging().apply_or_stage_patch(patch_id)
    except KeyError:
        raise HTTPException(404, "staged patch not found") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/staging/patches/auto-merge")
def http_auto_merge_staging_patches(request: StagingAutoMergeRequest) -> dict[str, Any]:
    try:
        return _staging().auto_merge_patches(request.patch_ids)
    except KeyError as exc:
        raise HTTPException(404, f"staged patch {exc.args[0]} not found") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


# -- Projects --

class ProjectCreateRequest(BaseModel):
    name: str
    description: str = ""


def _proj_to_dict(p) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "description": p.description}


@app.get("/projects")
def http_list_projects() -> dict[str, Any]:
    return {"projects": [_proj_to_dict(p) for p in tool_registry.list_projects()]}


@app.post("/projects")
def http_create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    pid = tool_registry.register_project(name=request.name, description=request.description)
    return _proj_to_dict(tool_registry.get_project(pid))


@app.delete("/projects/{name}")
def http_delete_project(name: str) -> dict[str, Any]:
    ok = tool_registry.remove_project(name)
    if not ok:
        raise HTTPException(404, f"project '{name}' not found")
    return {"removed": name}


# -- Parts --

class PartCreateRequest(BaseModel):
    name: str
    description: str = ""


class PartToolsSetRequest(BaseModel):
    tools: list[str]  # ordered list of tool instance names


class PartToolBindRequest(BaseModel):
    tool: str
    position: int | None = None
    alias: str | None = None


def _part_to_dict(part, tools) -> dict[str, Any]:
    return {
        "id": part.id, "project": part.project_name,
        "name": part.name, "description": part.description,
        "tools": [_ti_to_dict(t) for t in tools],
    }


@app.get("/projects/{project}/parts")
def http_list_parts(project: str) -> dict[str, Any]:
    parts = tool_registry.list_parts(project)
    out = []
    for p in parts:
        tools = tool_registry.resolve_scope(project=project, part=p.name)
        out.append(_part_to_dict(p, tools))
    return {"project": project, "parts": out}


@app.post("/projects/{project}/parts")
def http_create_part(project: str, request: PartCreateRequest) -> dict[str, Any]:
    try:
        part_id = tool_registry.register_part(
            project=project, name=request.name, description=request.description
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    part = tool_registry.get_part(project=project, part=part_id)
    tools = tool_registry.resolve_scope(project=project, part=request.name)
    return _part_to_dict(part, tools)


@app.delete("/projects/{project}/parts/{part}")
def http_delete_part(project: str, part: str) -> dict[str, Any]:
    ok = tool_registry.remove_part(project=project, part=part)
    if not ok:
        raise HTTPException(404, f"part '{part}' not found in project '{project}'")
    return {"removed": part, "project": project}


@app.get("/projects/{project}/parts/{part}/scope")
def http_resolve_scope(project: str, part: str) -> dict[str, Any]:
    """Resolve the exact ordered tool list for this project+part scope."""
    tools = tool_registry.resolve_scope(project=project, part=part)
    return {
        "project": project, "part": part,
        "tools": [_ti_to_dict(t) for t in tools],
    }


@app.put("/projects/{project}/parts/{part}/tools")
def http_set_part_tools(project: str, part: str, request: PartToolsSetRequest) -> dict[str, Any]:
    """Replace a part tool list entirely (ordered)."""

    try:
        tool_registry.set_part_tools(
            project=project, part=part, tool_names_or_ids=request.tools
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    tools = tool_registry.resolve_scope(project=project, part=part)
    return {
        "project": project, "part": part,
        "tools": [_ti_to_dict(t) for t in tools],
    }


@app.post("/projects/{project}/parts/{part}/tools")
def http_bind_tool(project: str, part: str, request: PartToolBindRequest) -> dict[str, Any]:
    """Bind a single tool instance to a part (append or at explicit position)."""
    try:
        tool_registry.bind_tool_to_part(
            project=project, part=part, tool=request.tool, position=request.position,
            alias=request.alias,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    tools = tool_registry.resolve_scope(project=project, part=part)
    return {
        "project": project, "part": part, "bound": request.tool,
        "tools": [_ti_to_dict(t) for t in tools],
    }


@app.delete("/projects/{project}/parts/{part}/tools/{tool}")
def http_unbind_tool(project: str, part: str, tool: str) -> dict[str, Any]:
    """Remove a single tool instance binding from a part."""
    ok = tool_registry.unbind_tool_from_part(project=project, part=part, tool=tool)
    if not ok:
        raise HTTPException(404, f"'{tool}' is not bound to {project}/{part}")
    tools = tool_registry.resolve_scope(project=project, part=part)
    return {
        "project": project, "part": part, "unbound": tool,
        "tools": [_ti_to_dict(t) for t in tools],
    }


@app.get("/control/local/governor")
def governor_stats() -> dict[str, Any]:
    """Live concurrency stats."""
    return governor.stats()


@app.get("/control/local/idle")
def governor_idle_backends(idle_sec: float = 600.0) -> dict[str, Any]:
    """List idle backends."""
    return {"idle_backends": governor.idle_backends(idle_sec), "threshold_sec": idle_sec}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8790)
