"""How the assistant looks deeper than its passive context (HERALD_FAMILY_SPEC.md, "Awareness").

Passive context needs no tool: every turn already carries the focused window, window titles and
workspaces of the user's devices (signals.py publishes them, activity.py keeps them, the turn's
"Desktop right now" block shows them).

Active context is what a tool has to go and get: an app's contents through its accessibility
tree (tier 2) or the screen itself (tier 3). Nothing here asks the user's permission or refuses
a call: the assistant may use every tool on its own, and is trusted to use these only when the
task in front of it needs them. What this module adds is accountability. Every active look is
logged on the device (tool, tier, focused app, time; never what was seen) and its result
carries a note telling the agent to mention that it looked.

A module marks its active-context tools in module.json, ``"context_tiers": {"ui_tree": 2,
"capture_screen": 3}``; the module manager hands that to ``NodeLink.offer`` and
``node_link.NodeLink._call`` calls :func:`observe` for each device tool call.
"""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .settings import settings

OBSERVATION_LOG = "context_observations.jsonl"
MAX_LOG_LINES = 2000
TIER_NAMES = {2: "an app's contents (its accessibility tree)", 3: "the screen"}
_LOCK = threading.Lock()


@dataclass
class Look:
    tool: str
    tier: int
    app: str


def parse_tiers(declared: Any) -> dict[str, int]:
    """A module.json ``context_tiers`` value as {tool: 2 or 3}; anything else is ignored."""
    if not isinstance(declared, dict):
        return {}
    return {str(tool): int(tier) for tool, tier in declared.items()
            if isinstance(tier, int) and not isinstance(tier, bool) and tier in (2, 3)}


def effective_tool(tool: str, arguments: Any, progressive: bool) -> str:
    """The tool that will really run: a progressive server runs it through ``<prefix>_run``."""
    if progressive and tool.endswith("_run") and isinstance(arguments, dict) and isinstance(arguments.get("tool"), str):
        return arguments["tool"]
    return tool


def focused_app() -> str:
    """The focused app's process name (signals.py's detection), '' when it can't be told."""
    from . import signals
    try:
        return (signals._foreground_process() if sys.platform == "win32"
                else signals._hyprland_foreground_process()) or ""
    except Exception:  # noqa: BLE001 - no compositor, no desktop: unknown app
        return ""


def log_path():
    return settings.user_settings_file.parent / OBSERVATION_LOG


def record(look: Look) -> dict[str, Any]:
    """Append what was looked at, never what was seen."""
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": look.tool,
             "tier": look.tier, "app": look.app}
    path = log_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > MAX_LOG_LINES:
                pending = path.with_name(path.name + ".pending")
                pending.write_text("\n".join(lines[-MAX_LOG_LINES // 2:]) + "\n", encoding="utf-8")
                pending.replace(path)
        except OSError:
            pass
    return entry


def recent_observations(limit: int = 20) -> list[dict[str, Any]]:
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, int(limit)):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def looked_note(look: Look) -> str:
    app = look.app or "an unknown app"
    return f"[looked: the assistant just used {TIER_NAMES[look.tier]} while {app} was focused. Mention that you looked.]"


def annotate(payload: Any, look: Look) -> Any:
    """Add the 'looked' note to an MCP result so the agent relays it."""
    if not isinstance(payload, dict):
        return payload
    note = looked_note(look)
    payload = dict(payload)
    content = payload.get("content")
    payload["content"] = [*(content if isinstance(content, list) else []), {"type": "text", "text": note}]
    payload["looked_note"] = note
    return payload


def observe(tiers: dict[str, int], tool: str, arguments: Any, progressive: bool) -> Look | None:
    """Log one device tool call that reaches for active context. None: not such a tool."""
    real = effective_tool(tool, arguments, progressive)
    tier = tiers.get(real)
    if not tier:
        return None
    look = Look(real, tier, focused_app())
    record(look)
    return look
