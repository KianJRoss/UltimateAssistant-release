"""This device's link to Herald: the assistant node.

Every device the assistant runs on is a node. A node keeps one outbound
WebSocket to the user's Herald router (herald/router/node_relay.py) and
answers over it:

* tool requests for the MCP servers it runs on this machine (memory, tasks,
  modules, screen, input...). The assistant registers each server in Herald
  as ``{"node": <this node>, "server": <name>}`` (see ``localize``), so a model
  on any device calls it here. Results go back as the MCP result, images
  included. Servers stay running between calls.
* HTTP requests for this node's own API -- the main node's chat, so an
  interface on another device (Obsidian on a laptop) reaches the assistant.

With Herald on the same machine the link just stays local; it is the same
mechanism either way, and nothing on a device listens for inbound traffic.

Run ``python -m ultimate_assistant.node_link`` on a device that only lends
its screen and hands; the main node starts the link inside the assistant.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shlex
import socket
import sys
import threading
from datetime import timedelta
from typing import Any, Callable

import httpx

from .settings import settings

LOG = logging.getLogger(__name__)
RETRY_SECONDS = (1, 2, 5, 10, 30)
HOP_HEADERS = {"content-length", "connection", "transfer-encoding", "keep-alive", "content-encoding"}


def _saved_settings() -> dict[str, Any]:
    try:
        values = json.loads(settings.user_settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def node_label() -> str:
    return os.environ.get("ULTIMATE_ASSISTANT_NODE_LABEL") or _saved_settings().get("node_label") or socket.gethostname()


def node_name() -> str:
    raw = os.environ.get("ULTIMATE_ASSISTANT_NODE") or _saved_settings().get("node_name") or node_label()
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:40] or "device"


def is_main() -> bool:
    configured = os.environ.get("ULTIMATE_ASSISTANT_MAIN_NODE")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes"}
    return bool(_saved_settings().get("main_node", True))


def load_router_key() -> None:
    """Processes started by other apps (Obsidian, a scheduled task) may lack
    HERALD_API_KEY; fall back to this device's Herald client .env."""
    if os.environ.get("HERALD_API_KEY"):
        return
    with contextlib.suppress(Exception):
        from dotenv import load_dotenv
        from herald.router.storage_paths import data_dir
        load_dotenv(data_dir() / ".env", override=False)


def device_instance(base: str) -> str:
    """Herald instance name for one device's copy of a per-device server."""
    return f"{base}-{node_name()}"


class _Server:
    """One MCP server on this machine, started on first use and kept running."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: str | None,
                 progressive_catalog: str | None = None, native_allowed_tools: list[str] | None = None) -> None:
        self.command, self.env, self.cwd = command, env, cwd
        self.progressive = None
        if progressive_catalog is not None:
            from .progressive_tools import ProgressiveTools
            self.progressive = ProgressiveTools(progressive_catalog, allowed_tools=native_allowed_tools)
        self.session = None
        self.extra_env: dict[str, str] = {}
        self._owner: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None

    async def get(self, extra_env: dict[str, str]):
        self._lock = self._lock or asyncio.Lock()
        async with self._lock:
            if self.session is not None and extra_env != self.extra_env:
                await self._shutdown()
            if self.session is None:
                ready = asyncio.get_running_loop().create_future()
                self._stop = asyncio.Event()
                self._owner = asyncio.create_task(self._own(dict(extra_env), ready, self._stop))
                self.session = await ready
                self.extra_env = dict(extra_env)
            return self.session

    async def _own(self, extra_env: dict[str, str], ready: asyncio.Future, stop: asyncio.Event) -> None:
        """Open, hold and close the server in one task (anyio requires it)."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=self.command[0], args=self.command[1:],
                                       env={**os.environ, **self.env, **extra_env}, cwd=self.cwd)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=600)) as session:
                    await session.initialize()
                    ready.set_result(session)
                    await stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to whoever is waiting
            if not ready.done():
                ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(_describe(exc)))
        finally:
            if self._stop is stop:
                self.session = None

    async def _shutdown(self) -> None:
        owner, stop = self._owner, self._stop
        self.session, self._owner, self._stop = None, None, None
        if stop is not None:
            stop.set()
        if owner is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(owner, 10)

    async def close(self) -> None:
        if self._lock is None:
            return await self._shutdown()
        async with self._lock:
            await self._shutdown()


class NodeLink:
    def __init__(self) -> None:
        self.servers: dict[str, _Server] = {}
        self.http_base: str | None = None
        self.http_headers: dict[str, str] = {}
        self.main = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        # Run (in a thread) after every successful connect: offers live in this
        # process, so a restarted node registers its servers again here.
        self.on_connect: list[Callable[[], Any]] = []

    # -- what this node offers ------------------------------------------------

    def offer(self, name: str, command: list[str] | str, env: dict[str, str] | None = None,
              cwd: str | None = None, *, progressive_catalog: str | None = None,
              native_allowed_tools: list[str] | None = None) -> None:
        if isinstance(command, str):
            command = shlex.split(command, posix=os.name != "nt")
        server = _Server(list(command), {str(k): str(v) for k, v in (env or {}).items()}, cwd,
                         progressive_catalog=progressive_catalog, native_allowed_tools=native_allowed_tools)
        old = self.servers.get(name)
        self.servers[name] = server
        if old is not None and self.loop is not None:
            asyncio.run_coroutine_threadsafe(old.close(), self.loop)

    def serve_http(self, base: str, headers: dict[str, str] | None = None, *, main: bool | None = None) -> None:
        self.http_base, self.http_headers = base.rstrip("/"), dict(headers or {})
        self.main = is_main() if main is None else main

    def localize(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Rewrite a stdio tool registration so Herald reaches it on this node.

        The command stays here; Herald only learns the node and server name.
        Secret references (env_refs) stay in Herald, which resolves them and
        sends the values with each request.
        """
        if payload.get("transport") != "stdio":
            return payload
        config = dict(payload.get("config") or {})
        name = str(payload["name"])
        self.offer(name, config.get("command") or [], config.get("env"), config.get("cwd"),
                   progressive_catalog=config.get("progressive_catalog"),
                   native_allowed_tools=config.get("native_allowed_tools"))
        node_config: dict[str, Any] = {"node": node_name(), "server": name, "timeout": config.get("timeout", 60)}
        if config.get("env_refs"):
            node_config["env_refs"] = config["env_refs"]
        if payload.get("scope") == "global":
            node_config["device_name"] = node_label()
        return {**payload, "transport": "http", "config": node_config}

    # -- connection -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=lambda: asyncio.run(self.run()), name="node-link", daemon=True)
            self._thread.start()

    async def run(self) -> None:
        from websockets.asyncio.client import connect
        self.loop = asyncio.get_running_loop()
        attempt = 0
        while True:
            # Read each attempt: a host may obtain the key only once Herald is reachable.
            url = re.sub(r"^http", "ws", settings.herald_url.rstrip("/")) + "/nodes/connect"
            key = os.environ.get("HERALD_API_KEY")
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            try:
                async with connect(url, additional_headers=headers, max_size=64 * 1024 * 1024,
                                   ping_interval=20, ping_timeout=20) as ws:
                    send_lock = asyncio.Lock()

                    async def send(frame: dict[str, Any]) -> None:
                        async with send_lock:
                            await ws.send(json.dumps(frame))

                    await send({"type": "hello", "node": node_name(), "label": node_label(),
                                "main": bool(self.http_base and self.main)})
                    welcome = json.loads(await ws.recv())
                    if welcome.get("type") != "welcome":
                        raise ConnectionError(f"Herald refused this node: {welcome}")
                    LOG.info("Assistant node %s connected to %s", node_name(), settings.herald_url)
                    self.connected.set()
                    attempt = 0
                    for callback in self.on_connect:
                        threading.Thread(target=_logged(callback), name="node-link-connect", daemon=True).start()
                    async for raw in ws:
                        self._dispatch(json.loads(raw), send)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep reconnecting whatever happened
                LOG.warning("Assistant node link to %s lost: %s", settings.herald_url, exc)
            finally:
                self.connected.clear()
                for task in self._tasks.values():
                    task.cancel()
                self._tasks.clear()
            await asyncio.sleep(RETRY_SECONDS[min(attempt, len(RETRY_SECONDS) - 1)])
            attempt += 1

    def _dispatch(self, frame: dict[str, Any], send) -> None:
        kind, fid = frame.get("type"), str(frame.get("id", ""))
        if kind == "cancel":
            task = self._tasks.pop(fid, None)
            if task:
                task.cancel()
            return
        handler = {"list": self._list, "call": self._call, "http": self._http}.get(kind)
        if handler is None:
            return
        task = asyncio.create_task(handler(frame, send))
        self._tasks[fid] = task
        task.add_done_callback(lambda _t: self._tasks.pop(fid, None))

    async def _session(self, frame: dict[str, Any]):
        server = self.servers.get(str(frame.get("server")))
        if server is None:
            raise LookupError(f"this device does not run '{frame.get('server')}'")
        return server, await server.get({str(k): str(v) for k, v in (frame.get("env") or {}).items()})

    async def _list(self, frame: dict[str, Any], send) -> None:
        try:
            server = self.servers.get(str(frame.get("server")))
            if server is None:
                raise LookupError(f"this device does not run '{frame.get('server')}'")
            if server.progressive is not None:
                tools = server.progressive.schemas()
            else:
                _server, session = await self._session(frame)
                tools = (await session.list_tools()).model_dump(mode="json", exclude_none=True).get("tools", [])
            await send({"type": "result", "id": frame["id"], "result": {"tools": tools}})
        except Exception as exc:  # noqa: BLE001
            await send({"type": "result", "id": frame["id"], "error": _describe(exc)})

    async def _call(self, frame: dict[str, Any], send) -> None:
        server = None
        try:
            server, session = await self._session(frame)
            if server.progressive is not None:
                payload = await server.progressive.call(str(frame.get("tool")), frame.get("arguments") or {}, session)
            else:
                result = await session.call_tool(str(frame.get("tool")), arguments=frame.get("arguments") or {})
                payload = result.model_dump(mode="json", exclude_none=True)
            await send({"type": "result", "id": frame["id"], "result": payload})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - restart the server on its next use
            if server is not None and not isinstance(exc, LookupError):
                await server.close()
            await send({"type": "result", "id": frame["id"], "error": _describe(exc)})

    async def _http(self, frame: dict[str, Any], send) -> None:
        fid = frame["id"]
        if not self.http_base:
            await send({"type": "http_head", "id": fid, "status": 404, "headers": {}})
            await send({"type": "http_end", "id": fid})
            return
        url = self.http_base + str(frame.get("path") or "/")
        if frame.get("query"):
            url += "?" + str(frame["query"])
        headers = {**{k: v for k, v in (frame.get("headers") or {}).items() if k.lower() not in HOP_HEADERS},
                   **self.http_headers}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=None)) as client:
                async with client.stream(str(frame.get("method") or "GET"), url, headers=headers,
                                         content=base64.b64decode(frame.get("body") or "")) as response:
                    await send({"type": "http_head", "id": fid, "status": response.status_code,
                                "headers": {k: v for k, v in response.headers.items() if k.lower() not in HOP_HEADERS}})
                    async for chunk in response.aiter_bytes():
                        await send({"type": "http_chunk", "id": fid, "data": base64.b64encode(chunk).decode()})
            await send({"type": "http_end", "id": fid})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await send({"type": "http_end", "id": fid, "error": _describe(exc)})


def _logged(callback: Callable[[], Any]) -> Callable[[], None]:
    def run() -> None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - one failed offer must not stop the link
            LOG.exception("Assistant node connect step %s failed", getattr(callback, "__name__", callback))
    return run


def _describe(exc: BaseException) -> str:
    children = getattr(exc, "exceptions", None)
    if children:
        return "; ".join(_describe(child) for child in children)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


link = NodeLink()


def main() -> int:
    """A device node: lend this machine's screen and hands to the assistant."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_router_key()
    from . import canvas, device_tools, signals
    link.on_connect.append(device_tools.offer_enabled)
    link.start()
    # This device's part of being ahead of the user: its activity signals (what is in focus, idle)
    # for the main node's clock, and its overlay showing the main node's canvas (reminders, cards).
    if not is_main():
        signals.start(node_name())
        canvas.start_overlay(settings.workspace_root, settings.herald_url.rstrip("/") + "/nodes/main/http", "",
                             router_key=os.environ.get("HERALD_API_KEY", ""))
    print(f"Assistant node {node_name()} ({node_label()}) -> {settings.herald_url}")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
