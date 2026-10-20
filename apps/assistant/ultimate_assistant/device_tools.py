"""This device's eyes and hands, offered to the assistant through Herald.

Screen vision and Windows UI control are modules (optional-modules/screen-vision,
windows-ui) that run on the device they act on; turning a capability on installs the
module here. Each device's copy is named for the node and joins Herald's global
``device`` group, with the user's own app servers (device_mcp_servers), so every
model and CLI the router runs can see and operate any of the user's devices -- through
the node link, never through SSH or a second router.
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote
from pathlib import Path

import httpx

from .node_link import device_instance, link
from .settings import settings

DEVICE_GROUP = "device"
# Screen vision and Windows control are modules: turning a capability on installs its module on
# this device the first time, then enables it (module_manager binds it into the device group).
MODULES = {"vision": "screen-vision", "control": "windows-ui"}
DESCRIPTIONS = {
    "vision": "Screen capture of every monitor, OCR, and optional vision-model description.",
    "control": "Windows window list (z-order, what is actually visible) and user-opted input control.",
}
CAPABILITY_SETTING = {"vision": "desktop_vision", "control": "desktop_control"}


def _router() -> tuple[str, dict[str, str]]:
    key = os.environ.get("HERALD_API_KEY")
    return settings.herald_url.rstrip("/"), ({"Authorization": f"Bearer {key}"} if key else {})


def instance_name(capability: str) -> str:
    from .module_manager import ModuleManager
    return ModuleManager.tool_name(MODULES[capability])


def register(capability: str) -> str:
    """Turn on one desktop capability on this device; returns its Herald instance name."""
    from .module_manager import ModuleManager
    from .module_tools import _locked
    manager = ModuleManager()
    with _locked(manager):
        result = manager.install(MODULES[capability])
    if result.get("status") != "ready":
        missing = ", ".join(result.get("missing") or result.get("setup") or []) or "setup"
        raise ValueError(f"The {MODULES[capability]} module isn't ready on this device yet: {missing}")
    return instance_name(capability)


def set_membership(name: str, enabled: bool) -> None:
    """Keep Herald's global `device` group in step with what the user enabled."""
    base, headers = _router()
    group = httpx.get(f"{base}/mcp-groups/{DEVICE_GROUP}", headers=headers, timeout=15)
    if group.status_code == 404:
        if not enabled:
            return
        httpx.post(f"{base}/mcp-groups", headers=headers, json={
            "name": DEVICE_GROUP, "description": "The user's devices: screen, input and app tools.",
        }, timeout=15).raise_for_status()
        httpx.post(f"{base}/mcp-groups/{DEVICE_GROUP}/bindings", headers=headers,
                   json={"target_type": "global"}, timeout=15).raise_for_status()
    else:
        group.raise_for_status()
    if enabled:
        httpx.post(f"{base}/mcp-groups/{DEVICE_GROUP}/tools", headers=headers,
                   json={"tool": name}, timeout=15).raise_for_status()
    else:
        response = httpx.delete(f"{base}/mcp-groups/{DEVICE_GROUP}/tools/{quote(name, safe='')}",
                                headers=headers, timeout=15)
        if response.status_code != 404:
            response.raise_for_status()


def register_app_server(key: str, spec: dict) -> str:
    """Offer one of the user's own MCP servers on this device (an app's control server, e.g. Playnite's).

    spec: {"command": [...], "cwd"?, "env"?, "description"?, "timeout"?,
           "progressive_catalog"?, "native_allowed_tools"?}. Catalog and native filter
    stay local to this device. It runs here, on the device
    it controls, and joins the global `device` group like the screen and window tools. Credentials stay
    with the server (it reads its own token files); none go into Herald."""
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ValueError(f"device_mcp_servers.{key}: command must be a list of strings")
    catalog = spec.get("progressive_catalog")
    if catalog is not None and (not isinstance(catalog, str) or not catalog.strip()):
        raise ValueError(f"device_mcp_servers.{key}: progressive_catalog must be a non-empty path string")
    allowed = spec.get("native_allowed_tools")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(isinstance(name, str) and name for name in allowed):
            raise ValueError(f"device_mcp_servers.{key}: native_allowed_tools must be a list of non-empty strings")
        if catalog is None:
            raise ValueError(f"device_mcp_servers.{key}: native_allowed_tools requires progressive_catalog")
    name = device_instance("ultimate-assistant-" + key)
    base, headers = _router()
    config = {"command": command, "env": dict(spec.get("env") or {}), "timeout": int(spec.get("timeout") or 120)}
    if spec.get("cwd"):
        config["cwd"] = str(spec["cwd"])
    if catalog is not None:
        catalog_path = Path(catalog)
        if not catalog_path.is_absolute():
            catalog_path = settings.workspace_root / catalog_path
        config["progressive_catalog"] = str(catalog_path)
        if allowed is not None:
            config["native_allowed_tools"] = allowed
    httpx.post(f"{base}/tool-instances", headers=headers, timeout=20, json=link.localize({
        "name": name, "transport": "stdio", "scope": "global",
        "description": str(spec.get("description") or f"{key} tools on this device"),
        "tags": ["assistant", "device-app", key],
        "package_name": "ultimate-assistant-device-apps", "version": "local", "config": config,
    })).raise_for_status()
    set_membership(name, True)
    return name


def offer_enabled() -> list[str]:
    """Offer whatever desktop capabilities the user turned on for this device
    (settings.json, or ULTIMATE_ASSISTANT_DESKTOP_VISION/_CONTROL=1), plus the user's own app MCP
    servers listed under settings.json "device_mcp_servers"."""
    try:
        values = json.loads(settings.user_settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        values = {}
    offered = []
    for capability, setting in CAPABILITY_SETTING.items():
        if values.get(setting) or os.environ.get("ULTIMATE_ASSISTANT_" + setting.upper()) == "1":
            offered.append(register(capability))
    for key, spec in (values.get("device_mcp_servers") or {}).items():
        if isinstance(spec, dict) and spec.get("enabled", True):
            try:
                offered.append(register_app_server(str(key), spec))
            except (ValueError, httpx.HTTPError) as exc:  # one bad entry must not cost the device its other tools
                import logging
                logging.getLogger(__name__).warning("Could not offer device MCP server %s: %s", key, exc)
    return offered
