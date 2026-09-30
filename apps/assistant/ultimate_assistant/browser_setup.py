"""Configure browser MCPs using the existing Herald project registry."""
from __future__ import annotations

import os
import shutil
import subprocess
import socket
import time
import asyncio
import threading
from pathlib import Path
from urllib.parse import quote

from .onboarding import router_request
from .settings import settings

EXTENSION_URL = "https://chromewebstore.google.com/detail/kapture-mcp-browser-autom/ejfnegenodbdcodemkibocefmajjjjbn"
_playwright_process = None
_playwright_log = None
_playwright_stop = threading.Event()
_playwright_ready = threading.Event()
_playwright_thread = None
_playwright_error = None


def _keep_playwright_session() -> None:
    """Keep one client connected so Playwright retains its shared browser."""
    async def keep():
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(timeout=120) as client:
            async with streamable_http_client("http://127.0.0.1:18793/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("browser_tabs", {"action": "list"})
                    if result.isError:
                        raise ValueError("Playwright browser session failed its initial health call.")
                    _playwright_ready.set()
                    while not _playwright_stop.is_set():
                        await asyncio.sleep(.25)
    global _playwright_error
    try:
        asyncio.run(keep())
    except Exception as exc:
        _playwright_error = exc
        _playwright_ready.set()


def start_playwright() -> None:
    global _playwright_process, _playwright_log, _playwright_thread, _playwright_error
    if _playwright_process is not None and _playwright_process.poll() is None:
        return
    root = settings.workspace_root
    node_path = root / ".runtime/node-path.txt"
    node = str(Path(node_path.read_text().strip()) / "node.exe") if node_path.exists() else shutil.which("node.exe")
    browsers = sorted((root / ".runtime/browsers").glob("chromium-*/chrome-win64/chrome.exe"))
    if not browsers:
        browsers = sorted((root / ".runtime/browsers").glob("chromium-*/chrome-win/chrome.exe"))
    if not node or not browsers:
        raise ValueError("Playwright browser dependencies are missing.")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 18793))
    settings.user_data_dir.mkdir(parents=True,exist_ok=True)
    _playwright_log = (settings.user_data_dir / "playwright-server.log").open("a",encoding="utf-8")
    command = [node, str(root / ".runtime/browser-mcp/node_modules/@playwright/mcp/cli.js"),
               "--host", "127.0.0.1", "--allowed-hosts", "127.0.0.1:18793,localhost:18793", "--port", "18793", "--browser", "chrome", "--executable-path", str(browsers[-1]),
               "--user-data-dir", str(settings.user_data_dir / "playwright-profile"), "--shared-browser-context"]
    if os.environ.get("ULTIMATE_ASSISTANT_SYNTHETIC_TEST") == "1":
        command.append("--headless")
    _playwright_process = subprocess.Popen(command, stdout=_playwright_log,stderr=_playwright_log,
                                          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    import httpx
    for _ in range(40):
        if _playwright_process.poll() is not None: raise ValueError("Playwright MCP server failed to start.")
        try:
            httpx.get("http://127.0.0.1:18793/mcp",timeout=1)
            _playwright_stop.clear()
            _playwright_ready.clear()
            _playwright_error = None
            _playwright_thread = threading.Thread(target=_keep_playwright_session, daemon=True)
            _playwright_thread.start()
            if not _playwright_ready.wait(30) or _playwright_error:
                stop_playwright()
                raise ValueError("Playwright could not establish its persistent browser session.")
            return
        except httpx.HTTPError: time.sleep(.25)
    raise ValueError("Playwright MCP startup timed out.")


def stop_playwright() -> None:
    global _playwright_process, _playwright_log, _playwright_thread
    _playwright_stop.set()
    if _playwright_thread:
        _playwright_thread.join(timeout=5)
    if _playwright_process is not None and _playwright_process.poll() is None:
        _playwright_process.terminate()
        try: _playwright_process.wait(timeout=10)
        except subprocess.TimeoutExpired: _playwright_process.kill()
    if _playwright_log: _playwright_log.close()
    _playwright_process = None
    _playwright_log = None
    _playwright_thread = None


def configure(browser: str, assistant) -> dict:
    assistant.ensure_tool_scope()
    root = settings.workspace_root
    packages = root / ".runtime/browser-mcp/node_modules"
    # Resolve package-declared binary rather than assume a build directory.
    import json
    package = "kapture-mcp" if browser == "kapture" else "@playwright/mcp"
    directory = packages / package
    if not (directory / "package.json").exists():
        raise ValueError("Browser dependencies are not installed. Run setup-browser-tools.ps1.")
    metadata = json.loads((directory / "package.json").read_text("utf-8"))
    binary = metadata["bin"]
    if isinstance(binary, dict): binary = next(iter(binary.values()))
    script = directory / binary
    if browser == "kapture":
        # Run the bridge directly: the package CLI adds a child process that
        # outlives Windows stdio teardown and keeps its pipe open.
        script = script.parent / "bridge.js"
        if not script.exists():
            raise ValueError("The installed Kapture package is missing its bridge.")
        # Kapture 2.6.1's server honors KAPTURE_PORT but its bridge hardcodes
        # 61822. Preserve its handshake/reconnect implementation in a separate
        # adapter copy so isolated tests can use a different port.
        source = script.read_text("utf-8")
        marker = "const PORT = 61822;"
        if source.count(marker) != 1:
            raise ValueError("Unsupported Kapture bridge version.")
        adapter = script.with_name("ultimate-assistant-bridge.js")
        adapter.write_text(source.replace(marker, "const PORT = Number(process.env.KAPTURE_PORT || 61822);"), "utf-8")
        script = adapter
    node_path = root / ".runtime/node-path.txt"
    node = str(Path(node_path.read_text().strip()) / "node.exe") if node_path.exists() else shutil.which("node.exe")
    if not node: raise ValueError("Node.js is unavailable.")
    name = "ultimate-assistant-" + browser
    port = int(os.environ.get("ULTIMATE_ASSISTANT_KAPTURE_PORT", "61822"))
    config = {"command":[node,str(script)],"env":{"KAPTURE_PORT":str(port)},"timeout":120}
    transport = "stdio"
    if browser == "playwright":
        start_playwright()
        config = {"url":"http://127.0.0.1:18793/mcp","timeout":120}
        transport = "http"
        assistant._save_capability("playwright_enabled", True)
    router_request("POST", "/tool-instances", {"name":name, "transport":transport, "scope":"project", "project":settings.herald_project,
                   "package_name":package, "version":metadata["version"], "tags":["browser",browser],
                   "config":config,
                   "description":"Existing connected browser tabs via Kapture" if browser == "kapture" else "Playwright browser automation with a separate persistent profile"})
    router_request("POST", f"/projects/{quote(settings.herald_project,safe='')}/parts/{quote(settings.herald_part,safe='')}/tools",
                   {"tool":name,"alias":browser})
    return verify(browser, assistant)


def verify(browser: str, assistant) -> dict:
    catalog = assistant.list_tools()
    name = "ultimate-assistant-" + browser
    tools = [t for t in catalog.get("tools",[]) if t.get("instance") == name]
    errors = [e for e in catalog.get("discovery_errors",[]) if e.get("instance") == name]
    if errors or not tools:
        return {"status":"blocked", "detail":"Browser MCP tool discovery failed. Check package setup and Router logs."}
    if browser == "kapture":
        import httpx
        port = int(os.environ.get("ULTIMATE_ASSISTANT_KAPTURE_PORT", "61822"))
        response = httpx.get(f"http://127.0.0.1:{port}/tabs", timeout=10)
        response.raise_for_status()
        payload = response.json()
        tabs = payload if isinstance(payload, list) else payload.get("tabs", [])
        if not tabs:
            return {"status":"waiting", "detail":"Server discovery passed. Install the Chrome/Chromium extension, open a tab and enable its Kapture connection toggle, then verify again.", "extension_url":EXTENSION_URL}
        return {"status":"completed", "detail":"MCP discovery and read-only connected-tab listing passed.","tool_count":len(tools)}
    tool = next((t for t in tools if t.get("name", "").endswith("browser_tabs")),None)
    if not tool:
        return {"status":"blocked", "detail":"MCP discovered, but no supported read-only tab listing tool is available."}
    result = router_request("POST", "/tools/run", {"name":tool["name"], "arguments":{} if browser == "kapture" else {"action":"list"},
                            "project":settings.herald_project,"part":settings.herald_part})
    if not result.get("ok"):
        return {"status":"blocked", "detail":"Browser read-only health call failed."}
    text = str(result.get("content", result))
    if browser == "kapture" and ("no tabs" in text.lower() or '"tabs": []' in text or '"tabs":[]' in text):
        return {"status":"waiting", "detail":"Install the Chrome/Chromium extension, open a tab and enable its Kapture connection toggle, then verify again.","extension_url":EXTENSION_URL}
    return {"status":"completed", "detail":"MCP tool discovery and read-only tab listing passed.","tool_count":len(tools)}


def disconnect(browser: str, assistant) -> dict:
    router_request("DELETE", f"/projects/{quote(settings.herald_project,safe='')}/parts/{quote(settings.herald_part,safe='')}/tools/ultimate-assistant-{browser}")
    if browser == "playwright":
        assistant._save_capability("playwright_enabled",False)
        stop_playwright()
    return {"status":"completed","detail":"Browser disconnected from this assistant."}

