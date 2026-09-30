from __future__ import annotations

import re
from typing import Any


def match_voice_command(utterance: str) -> dict[str, Any] | None:
    raw = utterance.strip().lower()
    cleaned = re.sub(r"[^\w\s-]", "", raw).strip()

    # 1. Status / Health
    if re.search(r"\b(status|health|router status|system status|check status)\b", cleaned):
        return {
            "action": "status",
            "description": "Checking Herald router status and health",
        }

    # 2. Quota / Usage
    quota_match = re.search(r"\b(quota|usage|remaining|limits)\b", cleaned)
    if quota_match:
        target = None
        if "codex" in cleaned:
            target = "codex"
        elif "claude" in cleaned:
            target = "claude"
        elif "antigravity" in cleaned or "gemini" in cleaned:
            target = "antigravity"
        desc = "Checking quota usage" + (f" for {target}" if target else "")
        return {
            "action": "usage",
            "target": target,
            "description": desc,
        }

    # 3. Restart / Reload Router
    if re.search(r"\b(restart|reload|reboot)( the| my)? (router|herald)\b", cleaned):
        return {
            "action": "restart",
            "description": "Restarting Herald router service",
        }

    # 4. List Tools / Inspect Capabilities
    if re.search(r"\b(list|show)( all| my)? tools\b|\bwhat tools\b|\bavailable tools\b", cleaned):
        return {
            "action": "list_tools",
            "description": "Listing available Herald tools",
        }

    # 5. List Schedules
    if re.search(r"\b(list|show)( all| my)? schedules\b|\bwhat schedules\b|\bcron jobs\b", cleaned):
        return {
            "action": "list_schedules",
            "description": "Listing scheduled cron and event triggers",
        }

    # 6. Memory list / history search
    if re.search(r"\b(list( all)? memories|show( all)? memories|search history|session history)\b", cleaned):
        return {
            "action": "list_memories",
            "description": "Listing active agent memories and sessions",
        }

    return None


def execute_voice_command(cmd: dict[str, Any], *, client_or_none: Any = None) -> str:
    action = cmd.get("action")
    from herald.client import RouterClient
    client = client_or_none or RouterClient()

    if action == "status":
        try:
            stat = client.status()
            healthy = stat.get("healthy", True)
            backends = stat.get("backends", [])
            online = [b.get("name") for b in backends if b.get("enabled") and not b.get("circuit_open")]
            return f"Herald router is {'healthy' if healthy else 'degraded'}. {len(online)} backends online."
        except Exception as exc:
            return f"Unable to fetch status: {exc}"

    if action == "usage":
        try:
            from herald.router.cli_usage import all_usage
            rows = all_usage(refresh=False)
            target = cmd.get("target")
            summaries = []
            for r in rows:
                cli = r.get("cli", "")
                if target and target not in cli.lower():
                    continue
                limits = r.get("limits", [])
                for lim in limits:
                    pct = lim.get("remaining_percent")
                    if pct is not None:
                        summaries.append(f"{cli} has {pct}% remaining")
            if summaries:
                return ". ".join(summaries[:3]) + "."
            return "Usage data is currently unavailable."
        except Exception as exc:
            return f"Unable to read usage: {exc}"

    if action == "list_tools":
        try:
            tools = client.list_tools()
            names = [t.get("name") for t in tools if t.get("name")]
            count = len(names)
            preview = ", ".join(names[:5])
            return f"You have {count} tools registered, including {preview}."
        except Exception as exc:
            return f"Unable to list tools: {exc}"

    if action == "list_schedules":
        try:
            from herald.router.scheduler import ScheduleStore
            schedules = ScheduleStore().list_all()
            enabled = [s.name for s in schedules if s.enabled]
            return f"There are {len(schedules)} schedules configured, {len(enabled)} enabled."
        except Exception as exc:
            return f"Unable to fetch schedules: {exc}"

    if action == "list_memories":
        try:
            sessions = client.list_agent_sessions(limit=10)
            return f"There are {len(sessions)} active agent memory sessions."
        except Exception as exc:
            return f"Unable to list memories: {exc}"

    if action == "restart":
        return "Router restart command recognized. Please confirm in your terminal."

    return "Command recognized but no execution handler matched."
