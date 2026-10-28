"""The main node's app on this device: a loopback proxy to <HERALD_URL>/nodes/main/http.

A device that isn't the main node reaches the one assistant through Herald, which wants the
router key; a browser can't add it. This serves the main node's own UI at
http://127.0.0.1:8841 (the port the app uses on a main node) and adds the key to every
request, streaming replies through. Loopback is a secure context, so the page may use the
microphone. Nothing listens beyond this machine.
"""
from __future__ import annotations

import logging
import json
import os
import threading
import secrets
import hmac
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from .settings import settings

LOG = logging.getLogger(__name__)
PORT = int(os.environ.get("ULTIMATE_ASSISTANT_UI_PORT", "8841"))
HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "keep-alive",
               "content-encoding", "authorization", "x-api-key", "accept-encoding", "origin",
               "x-ultimate-assistant-device-token"}


def device_interface() -> str:
    from .node_link import node_label, node_name
    import platform
    return (f"Desktop device: {node_label()} (node {node_name()}), {platform.system()}. "
            "Device actions and canvas output should target this node unless the user names another device.")


async def _unread_marks(client, target: str, headers: dict, images=None) -> str:
    """What the user pointed at or showed (teach mode) on this device since its last turn
    (canvas.marks_context, teach_store.context)."""
    from .canvas import marks_context
    from .node_link import node_name
    from .teach_store import context as teaching_context
    parts = []
    params = {"device": node_name(), "unread": "true", "take": "true"}
    for path, key, describe in (("/api/canvas/marks", "marks", marks_context),
                                ("/api/teach/recordings", "recordings", teaching_context)):
        try:
            response = await client.get(f"{target}{path}", headers=headers, timeout=10, params=params)
            response.raise_for_status()
            records = response.json().get(key, [])
            parts.append(describe(records))
            if images is not None:
                from .visual_input import references
                references(records, "marks" if key == "marks" else "teach", images)
        except (httpx.HTTPError, ValueError):
            LOG.warning("Could not read this device's %s", key, exc_info=True)
    return "\n".join(part for part in parts if part)


def _run_skill_here(body: dict) -> dict:
    """A skill that belongs to this device, sent by the main node (skills.run_on_device)."""
    import tempfile
    from . import skills
    runner = str(body.get("runner") or "")
    if runner not in skills.RUNNERS or not str(body.get("script") or "").strip():
        return {"ok": False, "output": "", "error": "expected a runner and a script", "exit_code": -1, "seconds": 0}
    with tempfile.TemporaryDirectory(prefix="ua-skill-") as folder:
        script = Path(folder) / skills.RUNNERS[runner]
        script.write_text(str(body["script"]), encoding="utf-8")
        return skills.execute(runner, script, dict(body.get("parameters") or {}), name=str(body.get("name") or "skill"),
                              timeout=min(float(body.get("timeout") or 300), 1800))


def build_app(target: str, key: str, *, transport=None, device_token: str = ""):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None), transport=transport)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await client.aclose()

    def refused(request: Request):
        """Only this device's own pages may use it: loopback Host and a matching Origin."""
        from urllib.parse import urlsplit
        host = urlsplit(str(request.url)).hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"detail": "Expected a loopback Host"}, status_code=403)
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin and origin.rstrip("/").lower() != expected.rstrip("/").lower():
            return JSONResponse({"detail": "Request Origin does not match this device"}, status_code=403)
        return None

    # Emergency mode (emergency.py): while the main node is unreachable this page answers on the device.
    # Its endpoint runs a CLI with full rights, so it also needs a token only that page carries.
    emergency_token = secrets.token_urlsafe(24)

    def emergency_page():
        from starlette.responses import HTMLResponse
        page = (Path(__file__).parent / "ui" / "emergency.html").read_text(encoding="utf-8")
        return HTMLResponse(page.replace("__EMERGENCY_TOKEN__", emergency_token), status_code=503,
                            headers={"Cache-Control": "no-store", "Content-Security-Policy":
                                     "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"})

    async def emergency_chat(request: Request):
        problem = refused(request)
        if problem:
            return problem
        if not hmac.compare_digest(request.headers.get("x-ua-emergency-token", ""), emergency_token):
            return JSONResponse({"detail": "Open the assistant page again (emergency session expired)."}, status_code=403)
        try:
            message = str((await request.json()).get("message") or "").strip()
        except ValueError:
            message = ""
        if not message or len(message) > 20000:
            return JSONResponse({"detail": "Expected a message of 1-20000 characters."}, status_code=400)
        from . import emergency
        try:
            exchange = await asyncio.to_thread(emergency.answer, message, emergency.device_conversation())
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        return JSONResponse({"reply": exchange["reply"], "via": exchange["via"]})

    async def forward(request: Request):
        # Authenticate the browser's origin here before attaching the Router's key.
        # The remote Router sees its own Host and would reject this loopback origin.
        problem = refused(request)
        if problem:
            return problem
        local_token = request.headers.get("x-ultimate-assistant-device-token")
        if local_token:
            if not device_token or not hmac.compare_digest(local_token, device_token):
                return JSONResponse({"detail": "Invalid device session"}, status_code=403)
            path = request.path_params["path"]
            if path == "api/modules" and request.method == "GET":
                from .module_manager import ModuleManager
                from .node_link import node_name
                return JSONResponse({"node": node_name(), "platform": sys.platform,
                                     "modules": await asyncio.to_thread(ModuleManager().summary)})
            if path == "api/skills/run" and request.method == "POST":
                return JSONResponse(await asyncio.to_thread(_run_skill_here, await request.json()))
            parts = path.split("/")
            if len(parts) == 4 and parts[:2] == ["api", "modules"] and request.method == "POST":
                from .module_tools import manage_optional_module
                try:
                    result = await asyncio.to_thread(manage_optional_module, parts[2], parts[3])
                    return JSONResponse(json.loads(result))
                except (ValueError, RuntimeError) as exc:
                    return JSONResponse({"detail": str(exc)}, status_code=400)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = await request.body()
        if request.method == "POST" and request.path_params["path"] == "api/chat":
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    images = list(payload.get("images") or [])
                    pointed = await _unread_marks(client, target, headers, images)
                    if images:
                        payload["images"] = images
                    payload["interface"] = (device_interface() + ("\n" + pointed if pointed else "") + "\n"
                                            + str(payload.get("interface") or ""))
                    body = json.dumps(payload).encode()
            except (ValueError, UnicodeError):
                pass  # The owning API returns its normal validation error.
        upstream = client.build_request(request.method, f"{target}/{request.path_params['path']}",
                                        params=request.query_params, headers=headers,
                                        content=body)
        try:
            response = await client.send(upstream, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            response = None
        if response is None or response.status_code in (502, 503, 504):
            safe = (response is None or request.method in ("GET", "HEAD") or
                    response.headers.get("x-herald-execution-status") == "not_started")
            if response is not None:
                await response.aclose()
            if request.method == "GET" and request.path_params["path"] in ("", "index.html"):
                return emergency_page()
            if not safe:
                return JSONResponse({"detail": "The connection failed after submission; execution may have "
                                     "started. Inspect the result before repeating the request.",
                                     "emergency": False, "execution_status": "unknown"}, status_code=503)
            return JSONResponse({"detail": "The main assistant is unreachable.", "emergency": True,
                                 "execution_status": "not_started"}, status_code=503)
        return StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                                 headers={k: v for k, v in response.headers.items() if k.lower() not in HOP_HEADERS},
                                 background=_close(response))

    async def desktop_state(request: Request):
        problem = refused(request)
        if problem:
            return problem
        from .desktop import socket_path, state_path
        import socket as _socket
        import uuid as _uuid
        if request.method == "POST":
            try:
                body = await request.json()
                conv_id = str(_uuid.UUID(str(body.get("conversation_id") or "")))
            except (ValueError, TypeError):
                return JSONResponse({"detail": "Invalid conversation_id"}, status_code=400)
            try:
                with _socket.socket(_socket.AF_UNIX) as sock:
                    sock.settimeout(2)
                    sock.connect(str(socket_path()))
                    sock.sendall(json.dumps({"action": "conversation", "text": conv_id}).encode() + b"\n")
                    with sock.makefile("r") as stream:
                        return JSONResponse(json.loads(stream.readline(65537)))
            except OSError:
                conv_file = settings.user_data_dir / "desktop-conversation.json"
                conv_file.parent.mkdir(parents=True, exist_ok=True)
                conv_file.write_text(json.dumps({"id": conv_id}) + "\n", encoding="utf-8")
                return JSONResponse({"state": "idle", "conversation_id": conv_id})
        try:
            payload = json.loads(state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {"state": "idle", "conversation_active": False}
        if not payload.get("conversation_id"):
            try:
                payload["conversation_id"] = json.loads(
                    (settings.user_data_dir / "desktop-conversation.json").read_text(encoding="utf-8")
                ).get("id", "")
            except (OSError, ValueError):
                payload["conversation_id"] = ""
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    return Starlette(routes=[Route("/api/emergency/chat", emergency_chat, methods=["POST"]),
                             Route("/api/desktop/state", desktop_state, methods=["GET", "POST"]),
                             Route("/{path:path}", forward, methods=methods)], lifespan=lifespan)


def _close(response):
    from starlette.background import BackgroundTask
    return BackgroundTask(response.aclose)


def start() -> bool:
    """Serve the main node's app on loopback in the background (once)."""
    import uvicorn
    from .node_link import link
    target = settings.herald_url.rstrip("/") + "/nodes/main/http"
    device_token = secrets.token_urlsafe(32)
    app = build_app(target, os.environ.get("HERALD_API_KEY", ""), device_token=device_token)
    link.serve_http(f"http://127.0.0.1:{PORT}", {"x-ultimate-assistant-device-token": device_token}, main=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, name="assistant-ui-proxy", daemon=True).start()
    LOG.info("Assistant app for this device at http://127.0.0.1:%s (main node through Herald)", PORT)
    return True
