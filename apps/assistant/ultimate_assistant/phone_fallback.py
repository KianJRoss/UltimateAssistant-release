"""The phone's way in while the main node is unreachable: this desktop's emergency mode.

A small endpoint on loopback (port 8842), published to the user's own Tailscale network over
HTTPS by `tailscale serve` (port 8444). It answers only the owner's phone: Serve forwards
from loopback with the caller's tailnet address, and `tailscale whois` must say that address
is an Android or iOS device of the same Tailscale user as this computer. No credential is
needed, so there is none to leak; nothing reaches it from outside the tailnet.

The phone learns this address from the main node while it is up (phone/fallbacks.py; this
node registers it on every connect), and turns to it when sending fails. The turn is answered
by emergency.py, queued, and handed to the main node's conversation once it is back.

`tailscale serve` needs root, or this user as Tailscale's operator: once, run
  sudo tailscale set --operator=$USER
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import ipaddress
import logging
import threading
import uuid

from .phone.pairing_invite import _run as tailscale

LOG = logging.getLogger(__name__)
PORT = 8842
SERVE_PORT = 8444
PHONE_OS = {"android", "ios"}


def owner_phone(peer: str | None, forwarded_for: str | None) -> bool:
    """Whether a request came through Serve from a phone of the same Tailscale user."""
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            return False
        address = str(ipaddress.ip_address((forwarded_for or "").split(",")[0].strip()))
    except ValueError:
        return False
    me = ((tailscale("status", "--json") or {}).get("Self") or {}).get("UserID")
    who = tailscale("whois", "--json", address) or {}
    node, user = who.get("Node") or {}, who.get("UserProfile") or {}
    os_name = str((node.get("Hostinfo") or {}).get("OS") or "").lower()
    return bool(me) and str(user.get("ID")) == str(me) and os_name in PHONE_OS


def build_app():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from . import emergency
    from .node_link import node_label

    async def allowed(request: Request) -> bool:
        return await asyncio.to_thread(owner_phone, request.client.host if request.client else None,
                                       request.headers.get("x-forwarded-for"))

    async def ping(request: Request):
        if not await allowed(request):
            return JSONResponse({"detail": "Only the owner's phone, through Tailscale."}, status_code=403)
        return JSONResponse({"device": node_label(), "clis": emergency.available()})

    async def chat(request: Request):
        if not await allowed(request):
            return JSONResponse({"detail": "Only the owner's phone, through Tailscale."}, status_code=403)
        try:
            body = await request.json()
            message = str(body.get("message") or "").strip()
            conversation = str(uuid.UUID(str(body.get("conversation_id"))))
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"detail": "Expected a message and a conversation_id."}, status_code=400)
        if not message or len(message) > 20000:
            return JSONResponse({"detail": "Expected a message of 1-20000 characters."}, status_code=400)
        try:
            exchange = await asyncio.to_thread(emergency.answer, message, conversation)
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        return JSONResponse({"reply": exchange["reply"], "via": exchange["via"], "device": node_label()})

    return Starlette(routes=[Route("/emergency/ping", ping, methods=["GET"]),
                             Route("/emergency/chat", chat, methods=["POST"])])


def publish() -> str | None:
    """Serve the endpoint to the tailnet (HTTPS) and return its address, or None if Serve refused."""
    import subprocess
    from .phone.pairing_invite import FLAGS, _tailscale
    executable = _tailscale()
    name = (((tailscale("status", "--json") or {}).get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not executable or not name:
        return None
    done = subprocess.run([executable, "serve", "--bg", f"--https={SERVE_PORT}", f"http://127.0.0.1:{PORT}"],
                          capture_output=True, text=True, timeout=20, creationflags=FLAGS)
    if done.returncode:
        LOG.warning("Phone emergency fallback not published (%s). Once, run: sudo tailscale set --operator=$USER",
                    (done.stderr or done.stdout).strip()[:200])
        return None
    return f"https://{name}:{SERVE_PORT}"


def register(url: str) -> None:
    """Tell the main node where the phone can find this desktop (on every connect)."""
    from . import emergency
    from .node_link import node_label, node_name
    with closing(emergency.main_session()) as client:
        client.post("api/phone/fallbacks", json={"node": node_name(), "url": url, "label": node_label()}).raise_for_status()


def start() -> bool:
    import uvicorn
    from .node_link import link
    # proxy_headers off: the caller's address must stay the socket's (Serve's loopback), so the
    # forwarded tailnet address is checked here rather than trusted by uvicorn.
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=PORT, log_level="warning",
                                           proxy_headers=False))
    threading.Thread(target=server.run, name="phone-fallback", daemon=True).start()

    def published() -> None:
        url = publish()
        if not url:
            return
        def on_connect():
            try:
                register(url)
            except Exception:  # noqa: BLE001 - the main node may be away; the next connect tries again
                LOG.info("Could not register the phone fallback with the main node yet")
        link.on_connect.append(on_connect)
        on_connect()
    threading.Thread(target=published, name="phone-fallback-publish", daemon=True).start()
    return True
