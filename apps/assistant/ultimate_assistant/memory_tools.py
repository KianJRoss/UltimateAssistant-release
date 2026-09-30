from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .memory_store import MEMORY_CATEGORIES, MemoryStore, ollama_embedding
from .settings import settings


mcp = FastMCP("ultimate-assistant-memory")


def _store() -> MemoryStore:
    path = Path(os.environ.get("ULTIMATE_ASSISTANT_MEMORY_DB", str(settings.memory_db)))
    return MemoryStore(path)


@mcp.tool()
def search_assistant_memory(query: str, limit: int = 8) -> str:
    """Find durable user preferences, rules, ongoing context, or relevant past episodes."""
    matches = _store().search(query, limit=max(1, min(limit, 20)), embedder=ollama_embedding)
    return json.dumps({"memories": matches}, ensure_ascii=False)


@mcp.tool()
def remember_for_user(
    content: str, category: str = "context", title: str = "", importance: int = 3,
    source_conversation_id: str = "", source_message_id: int = 0,
) -> str:
    """Save a durable user rule, preference, ongoing context, or useful past episode. Save explicit remember requests immediately; ask before saving inferred sensitive or uncertain details."""
    if category not in MEMORY_CATEGORIES:
        return json.dumps({"error": f"Category must be one of: {', '.join(sorted(MEMORY_CATEGORIES))}."})
    try:
        memory = _store().remember(
            content, category=category, title=title, importance=max(1, min(importance, 5)),
            source_conversation_id=source_conversation_id or None,
            source_message_id=source_message_id or None,
            embedder=ollama_embedding,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps({"saved": True, "memory": {key: memory[key] for key in (
        "id", "category", "title", "content", "importance", "updated_at",
    )}}, ensure_ascii=False)


@mcp.tool()
def forget_assistant_memory(memory_id: str) -> str:
    """Permanently delete one saved memory by its ID when the user asks to forget it."""
    removed = _store().delete(memory_id.strip())
    return json.dumps({"deleted": removed, "memory_id": memory_id})


@mcp.tool()
def update_assistant_memory(
    memory_id: str, content: str, category: str = "context",
    title: str = "", importance: int = 3,
) -> str:
    """Correct an existing saved memory when the user updates or clarifies it."""
    try:
        memory = _store().update(
            memory_id.strip(), content, category=category, title=title,
            importance=max(1, min(importance, 5)), embedder=ollama_embedding,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if memory is None:
        return json.dumps({"error": "Memory not found."})
    return json.dumps({"updated": True, "memory": {key: memory[key] for key in (
        "id", "category", "title", "content", "importance", "updated_at",
    )}}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
