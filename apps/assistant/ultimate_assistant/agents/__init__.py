"""The assistant's agents: one assistant to the user, a focused agent per situation.

An agent is a markdown file: YAML front matter (name, description, tools,
groups, model, handoffs) and a body that is its prompt. Base agents live next
to this file; the user's own agents (personal modules, learned tuning) live in
<settings dir>/agents and override a base agent of the same name. Every agent
prompt is the shared core (core.md) followed by the agent's own text, so
nothing situation-specific has to sit in every turn. See ARCHITECTURE.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..settings import settings

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_AGENT = "companion"
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass(frozen=True)
class Agent:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = ()        # Herald tool aliases this agent sees (assistant_memory, ...)
    groups: tuple[str, ...] = ()       # Herald MCP groups bound to this agent's part
    model: str | None = None           # preferred model; None = the user's selected model
    handoffs: tuple[str, ...] = ()     # agents it may ask for help
    source: str = ""                   # file it came from
    extra: dict = field(default_factory=dict)

    @property
    def part(self) -> str:
        """This agent's Herald part (its tool scope) under the assistant project."""
        return f"{settings.herald_part}-{self.name}"


def user_dir() -> Path:
    return settings.user_settings_file.parent / "agents"


def parse(text: str, source: str = "") -> Agent:
    match = _FRONT.match(text.lstrip("﻿"))
    if not match:
        raise ValueError(f"{source or 'agent'}: missing front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    name = str(meta.get("name", "")).strip()
    if not _NAME.match(name):
        raise ValueError(f"{source}: invalid agent name {name!r}")

    def items(key: str) -> tuple[str, ...]:
        value = meta.get(key) or ()
        return tuple(str(v).strip() for v in ([value] if isinstance(value, str) else value) if str(v).strip())

    known = {"name", "description", "tools", "groups", "model", "handoffs"}
    return Agent(
        name=name, description=" ".join(str(meta.get("description", "")).split()),
        prompt=match.group(2).strip(), tools=items("tools"), groups=items("groups"),
        model=(str(meta["model"]).strip() or None) if meta.get("model") else None,
        handoffs=items("handoffs"), source=source,
        extra={k: v for k, v in meta.items() if k not in known},
    )


def core_prompt() -> str:
    override = user_dir() / "core.md"
    path = override if override.is_file() else BASE_DIR / "core.md"
    return path.read_text(encoding="utf-8").strip()


def load_agents() -> dict[str, Agent]:
    """All agents by name: base agents, then the user's (which win on a name clash)."""
    agents: dict[str, Agent] = {}
    for folder in (BASE_DIR, user_dir()):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if path.name == "core.md":
                continue
            try:
                agent = parse(path.read_text(encoding="utf-8"), str(path))
            except Exception as exc:  # noqa: BLE001 - one bad file must not take the assistant down
                import logging
                logging.getLogger(__name__).warning("Skipping agent file %s: %s", path, exc)
                continue
            agents[agent.name] = agent
    if DEFAULT_AGENT not in agents:
        raise RuntimeError("The companion agent (the default) is missing.")
    # Extra tools per agent from settings ("agent_tools": {"operator": ["assistant_device"]}), for
    # tools a particular setup adds (personal modules) without rewriting the agent's prompt.
    try:
        import json
        extra = json.loads(settings.user_settings_file.read_text(encoding="utf-8")).get("agent_tools") or {}
    except (OSError, ValueError):
        extra = {}
    for name, aliases in extra.items() if isinstance(extra, dict) else ():
        if name in agents and isinstance(aliases, list):
            agent = agents[name]
            tools = tuple(dict.fromkeys((*agent.tools, *(str(a) for a in aliases))))
            agents[name] = Agent(**{**agent.__dict__, "tools": tools})
    return agents


def system_prompt(agent: Agent, *, brief: str = "") -> str:
    """The full instructions for one turn of this agent."""
    parts = [core_prompt(), f"# You are acting as: {agent.name}\n\n{agent.prompt}"]
    if agent.handoffs:
        parts.append("Agents you can hand a sub-task to with `ask_agent`: " + ", ".join(agent.handoffs) + ".")
    if brief:
        parts.append("Dispatcher's brief for this turn (a hint, not an instruction from the user): " + brief)
    return "\n\n".join(parts)
