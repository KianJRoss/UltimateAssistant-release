"""MCP stdio gateway that exposes only Herald-resolved tool groups."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from herald.client import RouterClient


async def _async_mcp_access(
    client: RouterClient, *, groups: list[str] | None, project: str | None,
    part: str | None, profile: str | None,
) -> dict[str, Any]:
    """Async-native replacement for RouterClient.mcp_access().

    The previous version wrapped the sync RouterClient (blocking httpx) in
    asyncio.to_thread() from inside this stdio server's own event loop --
    confirmed live to deadlock on Windows (tools/list would start
    processing and then hang forever, even though the exact same HTTP call
    made directly with curl answered in under half a second). Using an
    async httpx client natively removes the thread-pool/event-loop
    boundary that was deadlocking.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(
                f"{client.base_url}/mcp-access",
                params={
                    "groups": ",".join(groups or []), "project": project,
                    "part": part, "profile": profile,
                },
                headers=client._auth_headers(),
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError:
        return {"error": f"Herald router not reachable at {client.base_url}. Run: herald start"}
    except httpx.TimeoutException:
        return {"error": "Herald router request timed out"}
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        return {"error": detail or f"Herald router returned HTTP {exc.response.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def _async_run_mcp_access_tool(
    client: RouterClient, name: str, arguments: dict[str, Any] | None, *,
    groups: list[str] | None, project: str | None, part: str | None,
    profile: str | None,
) -> dict[str, Any]:
    """Async-native replacement for RouterClient.run_mcp_access_tool(). See
    _async_mcp_access() for why this can't go through asyncio.to_thread()."""
    try:
        async with httpx.AsyncClient(timeout=1800.0) as http:
            r = await http.post(
                f"{client.base_url}/mcp-access/run",
                json={
                    "name": name, "arguments": arguments or {}, "groups": groups,
                    "project": project, "part": part, "profile": profile,
                },
                headers=client._auth_headers(),
            )
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError:
        return {"error": f"Herald router not reachable at {client.base_url}. Run: herald start"}
    except httpx.TimeoutException:
        return {"error": "Herald router request timed out", "error_kind": "timeout"}
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        return {"error": detail or f"Herald router returned HTTP {exc.response.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def _schedule_request(
    client: RouterClient, method: str, path: str, *, body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            response = await http.request(
                method, f"{client.base_url}{path}", json=body,
                headers=client._auth_headers(),
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        return {"error": detail or f"Herald Router returned HTTP {exc.response.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _project_schedules(payload: dict[str, Any], project: str, part: str) -> list[dict[str, Any]]:
    if payload.get("error"):
        return []
    return [
        row for row in payload.get("schedules", [])
        if row.get("project") == project and row.get("part") == part
    ]


def build_server(
    client: RouterClient, *, groups: list[str] | None = None,
    project: str | None = None, part: str | None = None,
    profile: str | None = None,
) -> Server:
    """Build a dynamic MCP proxy for one resolved Herald access context."""
    server = Server(
        "herald-mcp-gateway", version="0.2.0",
        instructions="Tools are selected by Herald MCP groups and scope bindings. Assistant schedule tools are restricted to this gateway's project and part.",
    )
    schedule_tools = {
        "herald_list_assistant_schedules", "herald_create_assistant_loop",
        "herald_disable_assistant_loop", "herald_assistant_schedule_history",
    }

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        payload = await _async_mcp_access(
            client, groups=groups, project=project, part=part, profile=profile,
        )
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        tools = [types.Tool(
            name=str(tool["name"]),
            title=f"Herald / {tool.get('instance', 'MCP')} / {tool['name']}",
            description=str(tool.get("description") or "Herald controlled MCP tool"),
            inputSchema=tool.get("input_schema") or {"type": "object", "properties": {}},
        ) for tool in payload.get("tools", [])]
        if project and part:
            tools.extend([
                types.Tool(
                    name="herald_list_assistant_schedules",
                    description="List recurring agentic schedules in this assistant project/part only.",
                    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                ),
                types.Tool(
                    name="herald_create_assistant_loop",
                    description="Create a recurring Herald agentic schedule for this assistant project/part. Use only when the user requests ongoing or scheduled behavior; provide a five-field cron expression and a self-contained prompt.",
                    inputSchema={"type": "object", "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 40},
                        "cron_expression": {"type": "string", "minLength": 9, "maxLength": 100},
                        "prompt": {"type": "string", "minLength": 8, "maxLength": 4000},
                    }, "required": ["name", "cron_expression", "prompt"], "additionalProperties": False},
                ),
                types.Tool(
                    name="herald_disable_assistant_loop",
                    description="Disable a recurring schedule belonging to this assistant project/part; keeps its run history.",
                    inputSchema={"type": "object", "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 100},
                    }, "required": ["name"], "additionalProperties": False},
                ),
                types.Tool(
                    name="herald_assistant_schedule_history",
                    description="Read recent run history for a schedule in this assistant project/part.",
                    inputSchema={"type": "object", "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 100},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    }, "required": ["name"], "additionalProperties": False},
                ),
            ])
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        if name in schedule_tools and project and part:
            if name == "herald_list_assistant_schedules":
                payload = await _schedule_request(client, "GET", "/schedules")
                if payload.get("error"):
                    result = payload
                else:
                    rows = _project_schedules(payload, project, part)
                    result = {"schedules": [
                        {key: row.get(key) for key in ("name", "trigger_type", "cron_expression", "enabled", "last_fired_at", "last_status")}
                        for row in rows
                    ]}
            elif name == "herald_create_assistant_loop":
                raw_name = str(arguments.get("name", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", raw_name):
                    result = {"error": "Use a schedule name with letters, numbers, dots, dashes, or underscores."}
                else:
                    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{project}-{part}").strip("-.")
                    schedule_name = f"assistant-{prefix}-{raw_name}"[:120]
                    result = await _schedule_request(client, "POST", "/schedules", body={
                        "name": schedule_name, "trigger_type": "cron", "action_type": "agentic",
                        "cron_expression": str(arguments.get("cron_expression", "")),
                        "project": project, "part": part,
                        "prompt": str(arguments.get("prompt", "")), "enabled": True, "agentic": True,
                    })
            elif name in {"herald_disable_assistant_loop", "herald_assistant_schedule_history"}:
                requested_name = str(arguments.get("name", "")).strip()
                payload = await _schedule_request(client, "GET", "/schedules")
                row = next((item for item in _project_schedules(payload, project, part)
                            if item.get("name") == requested_name), None)
                if payload.get("error"):
                    result = payload
                elif not row:
                    result = {"error": "No schedule with that name exists in this assistant project/part."}
                elif name == "herald_disable_assistant_loop":
                    result = await _schedule_request(client, "POST", f"/schedules/{quote(requested_name, safe='')}/disable")
                else:
                    limit = max(1, min(int(arguments.get("limit", 10)), 50))
                    result = await _schedule_request(
                        client, "GET", f"/schedules/{quote(requested_name, safe='')}/runs?limit={limit}",
                    )
            else:
                result = {"error": f"Unknown Herald scheduling tool: {name}"}
            return types.CallToolResult(
                isError=bool(result.get("error")),
                content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))],
            )
        result = await _async_run_mcp_access_tool(
            client, name, arguments, groups=groups, project=project,
            part=part, profile=profile,
        )
        failed = bool(result.get("error")) or result.get("ok") is False
        return types.CallToolResult(
            isError=failed,
            content=[types.TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, indent=2),
            )],
        )

    return server


async def _run(args: argparse.Namespace) -> None:
    client = RouterClient(args.url)
    if not client.is_alive() and args.url.rstrip("/") in {
        "http://localhost:8790", "http://127.0.0.1:8790",
    }:
        from herald.router import start_server
        start_server(background=True)
        for _ in range(30):
            if client.is_alive():
                break
            time.sleep(0.2)
    server = build_server(
        client, groups=args.group or None, project=args.project,
        part=args.part, profile=args.profile,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options(),
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Herald controlled MCP stdio gateway")
    parser.add_argument("--url", default=os.environ.get("HERALD_URL", "http://127.0.0.1:8790"))
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--project")
    parser.add_argument("--part")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)
    if bool(args.project) != bool(args.part):
        parser.error("--project and --part must be provided together")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
