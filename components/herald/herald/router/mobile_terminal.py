"""Mobile-safe access to allowlisted persistent CLI sessions.

The browser never talks directly to a PTY. Text is composed in a normal HTML
textarea, then pasted atomically into a tmux session. This avoids Android IME
composition corruption while preserving each real CLI's slash commands,
history, tools, and persistent state.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionSpec:
    id: str
    label: str
    session: str
    command: str
    description: str
    accent: str


HOME = Path.home()
LOCAL_BIN = HOME / ".local" / "bin"
HERALD = Path("/usr/local/bin/herald")

SESSIONS: dict[str, SessionSpec] = {
    "herald": SessionSpec(
        "herald", "Herald", "herald-mobile",
        f"{HERALD} code {HOME}", "Unified router, MCP tools, and agents", "cyan",
    ),
    "claude": SessionSpec(
        "claude", "Claude", "claude-mobile",
        f"{LOCAL_BIN / 'claude'}",
        "Claude Code", "orange",
    ),
    "codex": SessionSpec(
        "codex", "Codex", "codex-mobile",
        f"{LOCAL_BIN / 'codex'}",
        "Codex CLI primary profile", "green",
    ),
    "opencode": SessionSpec(
        "opencode", "OpenCode", "opencode-mobile",
        f". {HOME / '.config/opencode-secrets/env'} && exec {LOCAL_BIN / 'opencode'}",
        "OpenCode free-model session", "purple",
    ),
    "antigravity": SessionSpec(
        "antigravity", "Antigravity", "agy-mobile",
        f"{LOCAL_BIN / 'agy'}",
        "Antigravity profiles", "blue",
    ),
    "shell": SessionSpec(
        "shell", "Shell", "shell-mobile", "/bin/bash -l",
        "Persistent local shell", "gray",
    ),
}

KEYS = {
    "enter": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "interrupt": "C-c",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "backspace": "BSpace",
    "page_up": "PPage",
    "page_down": "NPage",
}


def _tmux() -> str:
    executable = shutil.which("tmux")
    if not executable:
        raise RuntimeError("tmux is not installed on this Herald host")
    return executable


def _run(args: list[str], *, input_text: str | None = None, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "TERM": "xterm-256color"},
    )


def _spec(session_id: str) -> SessionSpec:
    try:
        return SESSIONS[session_id]
    except KeyError as exc:
        raise ValueError(f"unknown mobile session '{session_id}'") from exc


def is_running(spec: SessionSpec) -> bool:
    return _run([_tmux(), "has-session", "-t", spec.session]).returncode == 0


def ensure_session(session_id: str) -> dict[str, Any]:
    spec = _spec(session_id)
    if not is_running(spec):
        result = _run([
            _tmux(), "new-session", "-d", "-s", spec.session,
            "-c", str(HOME), spec.command,
        ])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"could not launch {spec.label}")
    return {**asdict(spec), "running": True}


def list_sessions() -> list[dict[str, Any]]:
    tmux_available = shutil.which("tmux") is not None
    rows = []
    for spec in SESSIONS.values():
        running = tmux_available and is_running(spec)
        rows.append({**asdict(spec), "running": running, "available": tmux_available})
    return rows


def capture_screen(session_id: str, *, lines: int = 240) -> dict[str, Any]:
    spec = _spec(session_id)
    ensure_session(session_id)
    lines = max(40, min(lines, 1000))
    result = _run([
        _tmux(), "capture-pane", "-p", "-J", "-t", spec.session,
        "-S", f"-{lines}",
    ])
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"could not read {spec.label}")
    return {"session": session_id, "label": spec.label, "screen": result.stdout.rstrip(), "running": True}


def send_text(session_id: str, text: str, *, submit: bool = True) -> dict[str, Any]:
    spec = _spec(session_id)
    ensure_session(session_id)
    if not text:
        raise ValueError("text cannot be empty")
    if len(text) > 100_000:
        raise ValueError("text is limited to 100,000 characters")
    buffer_name = f"herald-mobile-{uuid.uuid4().hex}"
    loaded = _run([_tmux(), "load-buffer", "-b", buffer_name, "-"], input_text=text)
    if loaded.returncode:
        raise RuntimeError(loaded.stderr.strip() or "could not stage mobile input")
    pasted = _run([_tmux(), "paste-buffer", "-d", "-b", buffer_name, "-t", spec.session])
    if pasted.returncode:
        _run([_tmux(), "delete-buffer", "-b", buffer_name])
        raise RuntimeError(pasted.stderr.strip() or "could not paste mobile input")
    if submit:
        pressed = _run([_tmux(), "send-keys", "-t", spec.session, "Enter"])
        if pressed.returncode:
            raise RuntimeError(pressed.stderr.strip() or "could not submit mobile input")
    return {"ok": True, "session": session_id, "submitted": submit}


def send_key(session_id: str, key: str) -> dict[str, Any]:
    spec = _spec(session_id)
    ensure_session(session_id)
    try:
        tmux_key = KEYS[key]
    except KeyError as exc:
        raise ValueError(f"unsupported mobile key '{key}'") from exc
    result = _run([_tmux(), "send-keys", "-t", spec.session, tmux_key])
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"could not send {key}")
    return {"ok": True, "session": session_id, "key": key}
