"""Pairing the phone app with this assistant: where the phone reaches it, and a one-time invitation
the phone opens (QR code or link) to pair.

The phone reaches the computer over the user's Tailscale network (HTTPS through `tailscale serve`),
so nothing is exposed to the internet and the phone works away from home. An address can also be
set explicitly (setting "phone_url" or ULTIMATE_ASSISTANT_PHONE_URL).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import DEFAULT_DEVICE

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SERVE_PORT = 8443


def app_port() -> int:
    return int(os.environ.get("ULTIMATE_ASSISTANT_PORT", "8765"))


def _tailscale() -> str | None:
    found = shutil.which("tailscale")
    if not found and os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale" / "tailscale.exe"
        found = str(candidate) if candidate.exists() else None
    return found


def _run(*args: str) -> dict[str, Any] | None:
    executable = _tailscale()
    if not executable:
        return None
    try:
        done = subprocess.run([executable, *args], capture_output=True, text=True, timeout=15, creationflags=FLAGS)
        return json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def endpoint() -> dict[str, Any]:
    """{"state": "ready", "url"} or {"state": "needs_setup", "detail"}."""
    configured = os.environ.get("ULTIMATE_ASSISTANT_PHONE_URL")
    if not configured:
        try:
            from ..settings import settings
            configured = json.loads(settings.user_settings_file.read_text("utf-8")).get("phone_url")
        except (OSError, ValueError):
            configured = None
    if configured:
        return {"state": "ready", "url": str(configured).rstrip("/"), "via": "configured"}
    if not _tailscale():
        return {"state": "needs_setup", "detail": "Install Tailscale on this computer and the phone and sign in to the same "
                                                  "account, so the phone can reach the assistant from anywhere."}
    status = _run("status", "--json")
    name = ((status or {}).get("Self") or {}).get("DNSName", "").rstrip(".")
    if not name:
        return {"state": "needs_setup", "detail": "Tailscale is installed but not signed in on this computer."}
    serve = _run("serve", "status", "--json") or {}
    target = f"127.0.0.1:{app_port()}"
    for host, web in (serve.get("Web") or {}).items():
        proxy = ((web.get("Handlers") or {}).get("/") or {}).get("Proxy", "")
        if target in proxy:
            return {"state": "ready", "url": "https://" + host.replace(":443", ""), "via": "tailscale"}
    return {"state": "needs_setup", "detail": f"Tailscale is signed in as {name}; this assistant isn't served to the "
                                              f"Tailscale network yet (set_up_phone_access does it).", "machine": name}


def set_up_access() -> dict[str, Any]:
    """Serve the assistant to the user's own Tailscale network (HTTPS, not the internet)."""
    executable = _tailscale()
    if not executable:
        return endpoint()
    done = subprocess.run([executable, "serve", "--bg", f"--https={SERVE_PORT}", f"http://127.0.0.1:{app_port()}"],
                          capture_output=True, text=True, timeout=30, creationflags=FLAGS)
    if done.returncode:
        return {"state": "failed", "detail": (done.stderr or done.stdout).strip()[:500]}
    return endpoint()


def invitation(device_id: str = DEFAULT_DEVICE) -> dict[str, Any]:
    """A one-time invitation (5 minutes) as a link and a QR code the phone app opens."""
    where = endpoint()
    if where["state"] != "ready":
        return where
    from .services import service
    invite = service("pairing").begin(device_id)
    link = (f"ultimateassistant://conversation?backend_url={quote(where['url'], safe='')}"
            f"&challenge={quote(invite['challenge'], safe='')}")
    return {"state": "ready", "url": where["url"], "link": link, "challenge": invite["challenge"],
            "expires_at": invite.get("expires_at"), "qr_svg": qr_svg(link), "phone_app": phone_app()}


def phone_app() -> dict[str, Any] | None:
    """Where to get the phone app (version, download URL, SHA-256), from the release channel's
    release.json; None when it can't be read."""
    try:
        from ..settings import settings
        from ..updater import manifest_source
        import httpx
        manifest = httpx.get(manifest_source(settings.user_settings_file.parent), follow_redirects=True, timeout=10).json()
        app = manifest.get("phone_app")
        return app if isinstance(app, dict) and str(app.get("url", "")).startswith("https://") else None
    except Exception:  # noqa: BLE001 - pairing works without the link
        return None


def qr_svg(text: str) -> str | None:
    try:
        import segno
    except ImportError:
        return None
    output = io.BytesIO()
    segno.make(text, error="m").save(output, kind="svg", scale=6, border=2, dark="#111", light="#fff")
    return output.getvalue().decode("utf-8")

