"""One assistant on every surface: the phone (and voice) talk through the same
chat pipeline as the desktop UI and Obsidian -- this runtime's own /api/chat.

That pipeline owns memory, conversations (one objective each, with switches)
and time awareness, so a conversation started on the phone continues on the
desktop or in Obsidian and vice versa. Each surface keeps only the
conversation it is currently in, and follows the assistant when a message
moves it to another one.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

import httpx

_lock = threading.Lock()


def _state_file() -> Path:
    from . import phone_root
    return phone_root() / "surface-conversations.json"


def current_conversation(surface: str) -> str:
    # The canonical conversation store owns continuity, not the last device used.
    # A desktop turn can change the active objective without touching this file.
    from .. import web_ui as shared
    latest = shared._conversations.latest_conversation()
    if latest:
        return latest
    with _lock:
        try:
            values = json.loads(_state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
        return values.get('_active') or values.get(surface) or str(uuid.uuid4())


def _remember(surface: str, conversation_id: str) -> None:
    with _lock:
        path = _state_file()
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
        values[surface] = conversation_id
        values['_active'] = conversation_id
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        pending.write_text(json.dumps(values, indent=2), encoding="utf-8")
        pending.replace(path)


def chat(prompt: str, *, surface: str, interface: str, web_search: bool = False, progress=None) -> str:
    """Send one message from a surface; returns the reply (raises on failure)."""
    from .. import web_ui as shared
    from ..node_link import link
    base = link.http_base or "http://127.0.0.1:8841"
    conversation_id = current_conversation(surface)
    body = {"message": prompt[:20000], "conversation_id": conversation_id,
            "interface": interface[:20000], "web_search": web_search}
    headers = {"x-ultimate-assistant-token": shared._APP_TOKEN}
    timeout = httpx.Timeout(connect=10, read=1800, write=30, pool=10)
    kind = None
    with httpx.stream("POST", base + "/api/chat", json=body, headers=headers, timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event:"):
                kind = line[6:].strip()
                continue
            if not line.startswith("data:") or not kind:
                continue
            data = json.loads(line[5:].strip())
            if kind == "conversation":
                conversation_id = data["conversation_id"]
                _remember(surface, conversation_id)
                if progress:
                    note = "New conversation" if data.get("kind") == "new" else "Back to an earlier conversation"
                    progress({"state": "responding", "label": note})
            elif kind == "ack" and progress and data.get("text"):
                # The assistant's immediate first line, shown (and spoken) while the work runs.
                progress({"state": "acknowledged", "label": str(data["text"])[:160]})
            elif kind == "progress" and progress and data.get("label"):
                progress({"state": "responding", "label": str(data["label"])[:160]})
            elif kind == "complete":
                _remember(surface, data.get("conversation_id") or conversation_id)
                return str(data.get("reply") or "")
            elif kind == "error":
                raise RuntimeError(str(data)[:2000])
            kind = None
    raise RuntimeError("The assistant stopped without a reply.")
