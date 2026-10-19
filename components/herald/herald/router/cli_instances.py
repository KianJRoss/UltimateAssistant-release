"""Whole CLI instances: a native CLI (Codex, Claude Code) living in its own workspace with its own sessions.

Herald is a harness over the CLI, not a replacement for its conversation. A caller (UltimateAssistant's
agents) names an *instance* (workspace) and a *session* (e.g. a conversation id):

- The instance gets its own directory, ``~/.herald/instances/<name>``, with the caller's stable
  instructions written as ``AGENTS.md`` (Codex, Antigravity) and ``CLAUDE.md`` (Claude Code), so they
  reach the CLI as real project instructions instead of text pasted into a prompt, and nothing from
  whatever checkout the router happens to run in (Herald's own AGENTS.md) leaks in.
- The session maps to the CLI's own native session. The first turn starts it with the history so far;
  later turns resume it (``codex exec resume <id>``, ``claude --resume <id>``) and send only the new
  message, so the CLI keeps everything it actually did (commands, tool results, plans) like it does when
  used directly.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path.home() / ".herald" / "instances"
RESUMABLE = {"codex", "claude"}
_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(ROOT / "sessions.db", timeout=10)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS sessions (instance TEXT, session TEXT, cli TEXT, native_id TEXT, "
        "updated_at REAL, PRIMARY KEY (instance, session, cli))")
    return connection


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip(".-")[:80]
    if not cleaned:
        raise ValueError("instance name is empty")
    return cleaned


def workspace(instance: str, instructions: str | None = None) -> Path:
    """The instance's directory, created on first use; instruction files rewritten only when they change."""
    path = ROOT / safe_name(instance)
    path.mkdir(parents=True, exist_ok=True)
    if instructions is not None:
        text = instructions.strip() + "\n"
        for filename in ("AGENTS.md", "CLAUDE.md"):
            target = path / filename
            try:
                current = target.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current != text:
                temporary = target.with_suffix(".tmp")
                temporary.write_text(text, encoding="utf-8")
                temporary.replace(target)
    return path


def native_session(instance: str, session: str, cli: str) -> str | None:
    with _lock, _db() as db:
        row = db.execute("SELECT native_id FROM sessions WHERE instance=? AND session=? AND cli=?",
                         (safe_name(instance), str(session), cli)).fetchone()
    return row[0] if row else None


def remember_session(instance: str, session: str, cli: str, native_id: str) -> None:
    with _lock, _db() as db:
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?)",
                   (safe_name(instance), str(session), cli, native_id, time.time()))


def forget_session(instance: str, session: str, cli: str) -> None:
    with _lock, _db() as db:
        db.execute("DELETE FROM sessions WHERE instance=? AND session=? AND cli=?",
                   (safe_name(instance), str(session), cli))


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def split_messages(messages: list[dict[str, Any]]) -> dict[str, str]:
    """OpenAI-style messages -> what an instance needs.

    instructions: the first system message (stable; becomes AGENTS.md / CLAUDE.md).
    context: the other system messages (memories, time, task list... for this turn).
    history: earlier user/assistant turns (only sent when a native session is started fresh).
    message: the new user message.
    """
    systems = [m for m in messages if m.get("role") == "system"]
    dialogue = [m for m in messages if m.get("role") in ("user", "assistant")]
    last_user = max((i for i, m in enumerate(dialogue) if m.get("role") == "user"), default=None)
    message = _text(dialogue[last_user]) if last_user is not None else ""
    history = dialogue[:last_user] if last_user is not None else dialogue
    return {
        "instructions": _text(systems[0]) if systems else "",
        "context": "\n\n".join(_text(m) for m in systems[1:] if _text(m).strip()),
        "history": "\n\n".join(f"{m['role']}: {_text(m)}" for m in history if _text(m).strip()),
        "message": message,
    }


def turn_prompt(parts: dict[str, str], *, fresh: bool) -> str:
    """The prompt sent to the CLI: just the new message (plus this turn's context) when resuming; the
    conversation so far as well when the native session is new."""
    sections = []
    if parts["context"]:
        sections.append("<turn_context>\n" + parts["context"] + "\n</turn_context>")
    if fresh and parts["history"]:
        sections.append("<earlier_conversation>\n" + parts["history"] + "\n</earlier_conversation>")
    sections.append(parts["message"])
    return "\n\n".join(sections)
