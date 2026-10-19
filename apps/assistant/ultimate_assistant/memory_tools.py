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


@mcp.tool()
def supersede_assistant_memory(memory_id: str, replacement_id: str) -> str:
    """After the user corrects an old fact, retire it in favor of an already saved replacement. Retains the old record for audit. Do not infer which of conflicting memories is correct."""
    try:
        changed = _store().supersede(memory_id, replacement_id)
        return json.dumps({"superseded": changed, "memory_id": memory_id, "replacement_id": replacement_id})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


def _conversations():
    from .conversation_store import ConversationStore
    return ConversationStore(Path(os.environ.get("ULTIMATE_ASSISTANT_CONVERSATIONS_DB", str(settings.conversations_db))))


@mcp.tool()
def list_conversations(limit: int = 30) -> str:
    """Your earlier conversations with this user (oldest first): id, when, title, and summary if one was saved. Earlier conversations are not in your context; use this, then read_conversation or search_conversations, only when the current request needs them."""
    rows = _conversations().conversations(limit=max(1, min(limit, 200)))
    return "\n".join(
        f"- [{c['id']}] {str(c['started_at'])[:16]} -> {str(c['last_at'])[:16]} UTC, {c['messages']} messages: {c['title']}"
        + (f"\n  {c['summary']}" if c.get("summary") else "")
        for c in rows) or "No conversations yet."


@mcp.tool()
def read_conversation(conversation_id: str) -> str:
    """The full, timestamped transcript of one conversation by its id."""
    history = _conversations().history(conversation_id)
    return "\n\n".join(f"[{str(m['created_at'])[:16]} UTC] {m['role'].title()}:\n{m['content']}"
                       for m in history) or "No conversation with that id."


@mcp.tool()
def search_conversations(query: str, limit: int = 10) -> str:
    """Messages from any conversation that mention some text (a name, topic, assignment, decision), newest first."""
    hits = _conversations().search(query, limit=max(1, min(limit, 50)))
    return "\n\n".join(f"[{h['conversation_id']}] {str(h['created_at'])[:16]} UTC {h['role'].title()}:\n{h['content']}"
                       for h in hits) or "No matches."


if __name__ == "__main__":
    mcp.run(transport="stdio")
