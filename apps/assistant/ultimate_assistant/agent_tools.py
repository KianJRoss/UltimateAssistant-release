"""Tools for working with the assistant's other agents: hand off, run in parallel, schedule.

Runs as an MCP server on whichever device's node offers it. Every call goes to
the assistant's main node through Herald (``/nodes/main/http/api/agents/...``),
so a handoff from the phone or a laptop runs on the same assistant, with the
same memory. Schedules are Herald schedules whose action hands the run back to
the main node, so a scheduled job runs as the named agent and its result is
delivered to the user (see ARCHITECTURE.md).
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ultimate-assistant-agents")
SCHEDULE_PREFIX = "ua-"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")


def _herald() -> tuple[str, dict[str, str]]:
    from .settings import settings
    base = (os.environ.get("HERALD_URL") or settings.herald_url).rstrip("/")
    key = os.environ.get("HERALD_API_KEY")
    return base, ({"Authorization": f"Bearer {key}"} if key else {})


def _run(agent: str, task: str, context: str = "", deliver: str = "none") -> dict:
    base, headers = _herald()
    response = httpx.post(f"{base}/nodes/main/http/api/agents/run", headers=headers, timeout=1800,
                          json={"agent": agent, "task": task, "context": context, "deliver": deliver})
    if response.status_code >= 400:
        return {"agent": agent, "error": f"HTTP {response.status_code}: {response.text[:500]}"}
    return response.json()


@mcp.tool()
def list_agents() -> str:
    """The assistant's agents and what each one handles."""
    base, headers = _herald()
    response = httpx.get(f"{base}/nodes/main/http/api/agents", headers=headers, timeout=30)
    response.raise_for_status()
    return json.dumps(response.json(), ensure_ascii=False)


@mcp.tool()
def ask_agent(agent: str, task: str, context: str = "") -> str:
    """Hand a sub-task to another agent and get its answer back (e.g. operator -> researcher for a fact).

    `task` must be self-contained: the other agent does not see this conversation. Put what it needs
    to know in `context`. You stay responsible for the reply to the user."""
    result = _run(agent, task, context)
    return result.get("reply") or "Error: " + str(result.get("error"))


@mcp.tool()
def run_agents(tasks: list[dict], max_parallel: int = 4) -> str:
    """Run several independent tasks at once (a swarm), each as the named agent.

    tasks: [{"agent": "researcher", "task": "...", "context": "..."}, ...] (at most 12). Each task must be
    self-contained. Returns every result in order; compare and reconcile them before answering."""
    work = [t for t in tasks if isinstance(t, dict) and t.get("agent") and t.get("task")][:12]
    if not work:
        return "No valid tasks: each needs an agent and a task."
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_parallel), 6))) as pool:
        results = list(pool.map(lambda t: _run(str(t["agent"]), str(t["task"]), str(t.get("context", ""))), work))
    return json.dumps([{"agent": t["agent"], "task": t["task"][:200],
                        **({"reply": r["reply"]} if r.get("reply") else {"error": r.get("error")})}
                       for t, r in zip(work, results)], ensure_ascii=False)


@mcp.tool()
def schedule_agent(name: str, agent: str, task: str, cron_expression: str = "",
                   event_type: str = "", deliver: str = "conversation") -> str:
    """Make an agent do something on a schedule (cron) or when something happens (event).

    name: short id, lowercase-dashes (e.g. "morning-brief"). agent: who runs it (e.g. companion, tutor,
    planner). task: self-contained instructions for each run. cron_expression: five fields in the router's
    time zone ("0 8 * * 1-5" = weekdays 8:00); or event_type: a Herald event name to react to.
    deliver: "conversation" (post the result to the user's latest conversation), "notify" (also notify the
    user's devices) or "none" (silent; for upkeep jobs). Use only when the user asked for recurring or
    triggered behavior; tell them exactly when it will run."""
    if not _NAME.match(name):
        return "Error: name must be lowercase letters, digits and dashes (max 39)."
    if bool(cron_expression) == bool(event_type):
        return "Error: give exactly one of cron_expression or event_type."
    if deliver not in {"conversation", "notify", "none"}:
        return "Error: deliver must be conversation, notify or none."
    base, headers = _herald()
    body = {"name": SCHEDULE_PREFIX + name, "trigger_type": "cron" if cron_expression else "event",
            "cron_expression": cron_expression or None, "event_type": event_type or None,
            "action_type": "python", "python_target": "herald.router.node_relay.post_to_node",
            "python_kwargs": {"node": "main", "path": "api/agents/run",
                              "body": {"agent": agent, "task": task, "deliver": deliver, "label": name}}}
    response = httpx.post(f"{base}/schedules", headers=headers, json=body, timeout=30)
    if response.status_code >= 400:
        return f"Error: HTTP {response.status_code}: {response.text[:400]}"
    when = f"cron '{cron_expression}'" if cron_expression else f"event '{event_type}'"
    return f"Scheduled '{name}': {agent} runs on {when}, results delivered to {deliver}."


@mcp.tool()
def list_agent_schedules() -> str:
    """The assistant's schedules: name, agent, when, enabled, last run."""
    base, headers = _herald()
    response = httpx.get(f"{base}/schedules", headers=headers, timeout=30)
    response.raise_for_status()
    rows = []
    for s in response.json().get("schedules", []):
        if not str(s.get("name", "")).startswith(SCHEDULE_PREFIX):
            continue
        rows.append({"name": s["name"][len(SCHEDULE_PREFIX):], "when": s.get("cron_expression") or s.get("event_type"),
                     "enabled": s.get("enabled"), "last_fired_at": s.get("last_fired_at"),
                     "last_status": s.get("last_status")})
    return json.dumps(rows, ensure_ascii=False) if rows else "No schedules."


@mcp.tool()
def cancel_agent_schedule(name: str, delete: bool = False) -> str:
    """Stop a schedule (disable keeps its history; delete removes it)."""
    if not _NAME.match(name):
        return "Error: invalid schedule name."
    base, headers = _herald()
    full = SCHEDULE_PREFIX + name
    response = (httpx.delete(f"{base}/schedules/{full}", headers=headers, timeout=30) if delete
                else httpx.post(f"{base}/schedules/{full}/disable", headers=headers, timeout=30))
    if response.status_code >= 400:
        return f"Error: HTTP {response.status_code}: {response.text[:300]}"
    return f"{'Deleted' if delete else 'Disabled'} '{name}'."


if __name__ == "__main__":
    mcp.run(transport="stdio")
