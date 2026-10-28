"""Environment tools: the device's environment (OS, settings, apps, files outside the assistant)
for every agent, run on the device itself and offered over the node link (device_tools.py).
Every change made through them is recorded in the environment layer (environment.py) and can be
undone. The device's shell is offered beside them (Herald's native shell, rooted at home)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import environment

mcp = FastMCP("ultimate-assistant-environment")


def _out(value) -> str:
    return json.dumps(value, ensure_ascii=False)


@mcp.tool()
def environment_profile() -> str:
    """What this device's environment is and how to change it the right way: OS, distribution,
    desktop/compositor, shells, package managers, where settings live, the rules for changing them
    (on Omarchy: its own conventions), the change kinds, and which kinds the user wants confirmed.
    Read it before changing anything here."""
    return _out(environment.profile())


@mcp.tool()
def read_environment_file(path: str, max_chars: int = 20000) -> str:
    """Read a file anywhere in the environment (~ and $VARS expand)."""
    target = Path(os.path.expandvars(os.path.expanduser(path)))
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _out({"error": str(exc)})
    return _out({"path": str(target.resolve()), "chars": len(text), "text": text[:max(0, max_chars)],
                 "truncated": len(text) > max_chars})


@mcp.tool()
def list_environment_dir(path: str = "~") -> str:
    """List a folder in the environment: names, kinds and sizes."""
    target = Path(os.path.expandvars(os.path.expanduser(path)))
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return _out({"error": str(exc)})
    return _out({"path": str(target.resolve()), "entries": [
        {"name": p.name, "dir": p.is_dir(), **({} if p.is_dir() else {"bytes": p.stat().st_size})}
        for p in entries[:500]], "truncated": len(entries) > 500})


@mcp.tool()
def write_environment_file(path: str, content: str, why: str, kind: str, confirm: bool = False) -> str:
    """Create or replace a file in the environment. The previous version is kept so the change can
    be undone; Hyprland config is checked and put back if Hyprland rejects it. kind: one of the
    profile's change_kinds (appearance, keybindings, windows, input, desktop, apps, packages,
    automation, files, system). why: the user's request, in a sentence."""
    held = environment.needs_confirmation(kind, confirm)
    if held:
        return _out(held)
    try:
        return _out(environment.change_file(path, content, why=why, kind=kind))
    except (OSError, ValueError) as exc:
        return _out({"error": str(exc)})


@mcp.tool()
def edit_environment_file(path: str, old_text: str, new_text: str, why: str, kind: str, confirm: bool = False) -> str:
    """Replace one exact piece of text in an environment file (old_text must appear exactly once).
    Recorded and undoable like write_environment_file."""
    held = environment.needs_confirmation(kind, confirm)
    if held:
        return _out(held)
    try:
        target = environment.resolve(path)
        text = target.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            return _out({"error": f"old_text appears {count} times in {target}; it must appear exactly once"})
        return _out(environment.change_file(str(target), text.replace(old_text, new_text), why=why, kind=kind))
    except (OSError, ValueError) as exc:
        return _out({"error": str(exc)})


@mcp.tool()
def delete_environment_file(path: str, why: str, kind: str, confirm: bool = False) -> str:
    """Delete a file in the environment; kept so it can be restored with undo_environment_change."""
    held = environment.needs_confirmation(kind, confirm)
    if held:
        return _out(held)
    try:
        return _out(environment.change_file(path, None, why=why, kind=kind))
    except (OSError, ValueError) as exc:
        return _out({"error": str(exc)})


@mcp.tool()
def record_environment_change(summary: str, kind: str, why: str, recheck: str, undo: str, confirm: bool = False) -> str:
    """Record a change you made another way (a shell command, a package install, an app's own
    settings), with how to check it and how to undo it, so it is part of the environment layer
    like the file changes. Record it right after making it."""
    held = environment.needs_confirmation(kind, confirm)
    if held:
        return _out(held)
    try:
        return _out({"change": environment.record(summary, kind=kind, why=why, recheck=recheck, undo=undo)})
    except ValueError as exc:
        return _out({"error": str(exc)})


@mcp.tool()
def environment_changes(limit: int = 30, include_undone: bool = False) -> str:
    """The changes made to this device's environment, newest first: what, why, kind, when."""
    entries = [c for c in reversed(environment.changes()) if include_undone or not c.get("undone_at")]
    return _out({"changes": entries[:max(1, limit)]})


@mcp.tool()
def undo_environment_change(change_id: str, force: bool = False) -> str:
    """Undo one recorded change: a file goes back exactly to how it was; for a command, its recorded
    undo step is returned to run. A file edited again since won't be overwritten without force."""
    try:
        return _out(environment.undo(change_id, force=force))
    except (OSError, ValueError) as exc:
        return _out({"error": str(exc)})


if __name__ == "__main__":
    mcp.run()
