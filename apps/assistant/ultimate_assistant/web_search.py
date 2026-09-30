from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


async def _call_web_search(server_path: Path, query: str, limit: int) -> str:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_path)],
        env=os.environ.copy(),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "web_search",
                {"query": query, "num_results": max(1, min(int(limit), 8))},
            )
    if result.isError:
        raise RuntimeError("Herald web-search MCP returned an error")
    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


def search_web(server_path: Path, query: str, *, limit: int = 6) -> str:
    if not server_path.is_file():
        raise FileNotFoundError(f"Herald web-search MCP not found: {server_path}")
    if not query.strip():
        raise ValueError("Enter a web-search query.")
    return asyncio.run(_call_web_search(server_path, query.strip(), limit))

