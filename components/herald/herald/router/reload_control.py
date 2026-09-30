"""Allowlisted, supervisor-backed Herald component reloads."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any


RELOAD_TARGETS = {
    "router": {"unit": "herald.service", "scope": "user",
               "description": "Router API and mobile/account web clients"},
    "terminal": {"unit": "agent-terminal.service", "scope": "system",
                 "description": "Advanced ttyd terminal"},
    "sessions": {"unit": "agent-sessions.service", "scope": "system",
                 "description": "Persistent native CLI tmux sessions"},
}


def targets() -> list[dict[str, str]]:
    return [{"name": name, **value} for name, value in RELOAD_TARGETS.items()]


def schedule_reload(requested: list[str], *, delay_seconds: int = 3) -> dict[str, Any]:
    """Schedule reload outside the router process so its response can finish."""
    names = list(dict.fromkeys(requested or ["router"]))
    if "all" in names:
        names = list(RELOAD_TARGETS)
    unknown = [name for name in names if name not in RELOAD_TARGETS]
    if unknown:
        raise ValueError(f"unknown reload target(s): {', '.join(unknown)}")
    systemd_run = shutil.which("systemd-run")
    if os.name == "nt" or not systemd_run:
        raise RuntimeError("automatic reload requires systemd-run on the Herald host")

    commands = []
    for name in names:
        target = RELOAD_TARGETS[name]
        if target["scope"] == "user":
            commands.append(f"systemctl --user restart {target['unit']}")
        else:
            commands.append(f"sudo -n systemctl restart {target['unit']}")
    unit = f"herald-reload-{uuid.uuid4().hex[:10]}"
    delay = min(max(int(delay_seconds), 1), 60)
    result = subprocess.run(
        [systemd_run, "--user", "--collect", "--unit", unit,
         f"--on-active={delay}s", "--timer-property=AccuracySec=1s",
         "/bin/sh", "-c", " && ".join(commands)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "could not schedule reload")
    return {"scheduled": names, "delay_seconds": delay, "unit": unit,
            "detail": result.stdout.strip()}


def source_status(root: str | Path) -> dict[str, Any]:
    root = str(Path(root).resolve())

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    return {
        "pid": os.getpid(), "root": root,
        "commit": git("rev-parse", "--short", "HEAD") or None,
        "dirty": bool(git("status", "--porcelain")),
        "reload_targets": targets(),
    }
