"""Protocol-correct execution for registered MCP tool instances."""
from __future__ import annotations

import asyncio
import os
import shlex
import httpx
from datetime import timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from herald.router.account_registry import resolve_secret_ref


def _resolved_config(config: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(config)
    env = {str(k): str(v) for k, v in (config.get("env") or {}).items()}
    for key, reference in (config.get("env_refs") or {}).items():
        env[str(key)] = resolve_secret_ref(str(reference))
    if env:
        resolved["env"] = env
    headers = {str(k): str(v) for k, v in (config.get("headers") or {}).items()}
    for key, reference in (config.get("header_refs") or {}).items():
        headers[str(key)] = resolve_secret_ref(str(reference))
    if headers:
        resolved["headers"] = headers
    return resolved


def _result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json", exclude_none=True)
    return {"content": str(result)}


def _only_broken_resource_errors(exc: BaseException) -> bool:
    """Recognize the harmless stdio-close race emitted by some Go MCP servers."""
    children = getattr(exc, "exceptions", None)
    if children:
        return all(_only_broken_resource_errors(child) for child in children)
    return type(exc).__name__ == "BrokenResourceError"


async def _call_session(read: Any, write: Any, tool_name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    response = None
    try:
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=timeout)
        ) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            payload = _result_dict(result)
            response = {"ok": not bool(payload.get("isError")), "result": payload}
            # Some Go servers emit list-changed notifications immediately
            # after the response. Give the reader task one scheduling turn
            # before closing its in-memory channel.
            await asyncio.sleep(0.05)
    except BaseException as exc:
        if response is None or not _only_broken_resource_errors(exc):
            raise
    return response


async def _list_session(read: Any, write: Any, timeout: float) -> dict[str, Any]:
    response = None
    try:
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=timeout)
        ) as session:
            await session.initialize()
            result = await session.list_tools()
            payload = _result_dict(result)
            response = {"ok": True, "tools": payload.get("tools", [])}
            await asyncio.sleep(0.05)
    except BaseException as exc:
        if response is None or not _only_broken_resource_errors(exc):
            raise
    return response


async def _run_stdio(name: str, config: dict[str, Any], arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    command_value = config.get("command")
    if isinstance(command_value, str):
        command = shlex.split(command_value, posix=os.name != "nt")
    else:
        command = list(command_value or [])
    if not command:
        raise ValueError("stdio tool config requires a non-empty 'command'")
    env = {**os.environ, **{str(k): str(v) for k, v in (config.get("env") or {}).items()}}
    params = StdioServerParameters(
        command=command[0], args=command[1:], env=env, cwd=config.get("cwd")
    )
    result = None
    try:
        async with stdio_client(params) as (read, write):
            result = await _call_session(read, write, config.get("tool_name", name), arguments, timeout)
    except BaseException as exc:
        if result is None or not _only_broken_resource_errors(exc):
            raise
    return result


async def _run_http(name: str, config: dict[str, Any], arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = config.get("url")
    if not url:
        raise ValueError("HTTP MCP tool config requires 'url'")
    async with httpx.AsyncClient(headers=config.get("headers"), timeout=timeout, follow_redirects=True) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            return await _call_session(read, write, config.get("tool_name", name), arguments, timeout)


async def _run_sse(name: str, config: dict[str, Any], arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = config.get("url")
    if not url:
        raise ValueError("SSE MCP tool config requires 'url'")
    async with sse_client(url, headers=config.get("headers"), timeout=timeout) as (read, write):
        return await _call_session(read, write, config.get("tool_name", name), arguments, timeout)


async def _list_stdio(config: dict[str, Any], timeout: float) -> dict[str, Any]:
    command_value = config.get("command")
    if isinstance(command_value, str):
        command = shlex.split(command_value, posix=os.name != "nt")
    else:
        command = list(command_value or [])
    if not command:
        raise ValueError("stdio tool config requires a non-empty 'command'")
    env = {**os.environ, **{str(k): str(v) for k, v in (config.get("env") or {}).items()}}
    params = StdioServerParameters(
        command=command[0], args=command[1:], env=env, cwd=config.get("cwd")
    )
    result = None
    try:
        async with stdio_client(params) as (read, write):
            result = await _list_session(read, write, timeout)
    except BaseException as exc:
        if result is None or not _only_broken_resource_errors(exc):
            raise
    return result


async def _list_http(config: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = config.get("url")
    if not url:
        raise ValueError("HTTP MCP tool config requires 'url'")
    async with httpx.AsyncClient(headers=config.get("headers"), timeout=timeout, follow_redirects=True) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            return await _list_session(read, write, timeout)


async def _list_sse(config: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = config.get("url")
    if not url:
        raise ValueError("SSE MCP tool config requires 'url'")
    async with sse_client(url, headers=config.get("headers"), timeout=timeout) as (read, write):
        return await _list_session(read, write, timeout)


def execute_tool(name: str, transport: str, config: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        config = _resolved_config(config)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    timeout = float(config.get("timeout", 60))
    runners = {"stdio": _run_stdio, "http": _run_http, "sse": _run_sse}
    runner = runners.get(transport)
    if runner is None:
        raise ValueError(f"unsupported transport '{transport}'")
    try:
        return asyncio.run(runner(name, config, arguments, timeout))
    except Exception as exc:  # one tool failure must not crash the router
        return {"ok": False, "error": str(exc)}


def discover_tools(transport: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        config = _resolved_config(config)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    timeout = float(config.get("timeout", 15))
    runners = {"stdio": _list_stdio, "http": _list_http, "sse": _list_sse}
    runner = runners.get(transport)
    if runner is None:
        return {"ok": False, "error": f"unsupported transport '{transport}'"}
    try:
        return asyncio.run(runner(config, timeout))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
