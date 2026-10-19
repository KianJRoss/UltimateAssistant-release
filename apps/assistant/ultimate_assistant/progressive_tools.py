"""Progressive discovery for a device MCP session owned by node_link."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any


def _json(value: Any) -> dict:
    return value if isinstance(value, dict) else value.model_dump(mode="json", by_alias=True, exclude_none=True)


def _result(value: Any, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": error}


class _DiscoveryError(ValueError):
    """Invalid local wrapper input; does not invalidate the upstream session."""


class ProgressiveTools:
    """Catalog rules only affect discovery; the supplied session owns execution."""

    def __init__(self, path: str | Path, allowed_tools: list[str] | None = None):
        self.catalog = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        self.prefix = self.catalog["prefix"]
        self.categories = self.catalog["categories"]
        self.allowed = None if allowed_tools is None else frozenset(allowed_tools)
        self.patterns = {c["name"]: [re.compile(p) for p in c.get("patterns", [])] for c in self.categories}
        self._session = None
        self._inventory: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def schemas(self) -> list[dict]:
        def tool(suffix, description, properties=None, required=None):
            return {"name": f"{self.prefix}_{suffix}", "description": description,
                    "inputSchema": {"type": "object", "properties": properties or {},
                                    "required": required or [], "additionalProperties": False}}
        return [
            tool("guide", "Start here: concise capability categories and tool counts. Discover tools before describing and running one."),
            tool("tools", "List a bounded page of tools with short summaries. Filter by category and/or search; use describe for full arguments.",
                 {"category": {"type": "string"}, "query": {"type": "string"},
                  "page": {"type": "integer", "minimum": 1, "default": 1},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 15}}),
            tool("describe", "Read one tool's complete description, argument schema and annotations before invoking it.",
                 {"tool": {"type": "string"}}, ["tool"]),
            tool("run", "Execute one previously described tool using its exact arguments. Returns its complete MCP result; calls are never automatically retried.",
                 {"tool": {"type": "string"}, "arguments": {"type": "object", "default": {}}}, ["tool"]),
        ]

    def _category(self, name: str) -> str:
        for category in self.categories:
            if name in category.get("tools", []) or any(p.search(name) for p in self.patterns[category["name"]]):
                return category["name"]
        return "other"

    async def _tools(self, session: Any) -> dict[str, dict]:
        async with self._lock:
            if self._session is session:
                return self._inventory
            inventory = {}
            cursor = None
            seen = set()
            while True:
                page = _json(await session.list_tools(cursor=cursor)) if cursor else _json(await session.list_tools())
                for raw in page.get("tools", []):
                    tool = _json(raw)
                    if self.allowed is None or tool["name"] in self.allowed:
                        inventory[tool["name"]] = tool
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise ValueError("Upstream tools/list repeated its pagination cursor")
                seen.add(cursor)
            self._session, self._inventory = session, inventory
            return inventory

    async def call(self, name: str, arguments: dict | None, session: Any) -> dict:
        """Return a JSON MCP CallToolResult, including unchanged upstream run results."""
        try:
            schemas = {s["name"]: s["inputSchema"] for s in self.schemas()}
            if name not in schemas:
                raise _DiscoveryError(f"Unknown discovery tool: {name}")
            arguments = {} if arguments is None else arguments
            if not isinstance(arguments, dict):
                raise _DiscoveryError("arguments must be an object")
            schema = schemas[name]
            if set(arguments) - set(schema["properties"]):
                raise _DiscoveryError("Unknown arguments: " + ", ".join(sorted(set(arguments) - set(schema["properties"]))))
            for key in schema["required"]:
                if key not in arguments:
                    raise _DiscoveryError(f"Missing argument: {key}")
            for key, value in arguments.items():
                kind = schema["properties"][key]["type"]
                if ((kind == "string" and not isinstance(value, str)) or
                    (kind == "object" and not isinstance(value, dict)) or
                    (kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)))):
                    raise _DiscoveryError(f"{key} must be {kind}")
            action = name[len(self.prefix) + 1:]
            if action == "tools":
                page, limit = arguments.get("page", 1), arguments.get("limit", 15)
                if page < 1 or not 1 <= limit <= 30:
                    raise _DiscoveryError("page must be >= 1 and limit must be between 1 and 30")
                category = arguments.get("category")
                if category is not None and category not in {c["name"] for c in self.categories} | {"other"}:
                    raise _DiscoveryError("Unknown category; use guide for category names")
            inventory = await self._tools(session)
            if action == "guide":
                counts: dict[str, int] = {}
                for tool_name in inventory:
                    category = self._category(tool_name)
                    counts[category] = counts.get(category, 0) + 1
                categories = [{"name": c["name"], "description": c.get("description", ""), "count": counts.get(c["name"], 0)}
                              for c in self.categories if counts.get(c["name"])]
                if counts.get("other"):
                    categories.append({"name": "other", "description": "Additional upstream capabilities", "count": counts["other"]})
                return _result({"title": self.catalog.get("title", self.prefix), "about": self.catalog.get("about", ""),
                                "start": self.catalog.get("start", ""), "total": len(inventory), "categories": categories})
            if action == "tools":
                query = arguments.get("query", "").casefold()
                matches = [t for n, t in sorted(inventory.items())
                           if (category is None or self._category(n) == category)
                           and (not query or query in (n + " " + (t.get("description") or "")).casefold())]
                start = (page - 1) * limit
                def summary(tool):
                    description = " ".join((tool.get("description") or "").split())
                    return {"name": tool["name"], "category": self._category(tool["name"]),
                            "summary": description if len(description) <= 180 else description[:179] + "…"}
                return _result({"total": len(matches), "page": page, "limit": limit,
                                "next_page": page + 1 if start + limit < len(matches) else None,
                                "tools": [summary(t) for t in matches[start:start + limit]]})
            selected = arguments["tool"]
            if selected not in inventory:
                raise _DiscoveryError("Unknown or unavailable tool; use tools to discover accessible names")
            if action == "describe":
                return _result(inventory[selected])
            # Do not retry mutations or flatten/truncate images, resources or structured content.
            return _json(await session.call_tool(selected, arguments.get("arguments", {})))
        # Upstream failures propagate to the node, which resets the session for
        # a subsequent request. Never retry the current tool invocation here.
        except _DiscoveryError as exc:
            return _result({"error": str(exc)}, error=True)
