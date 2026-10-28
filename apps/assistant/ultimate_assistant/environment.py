"""The environment layer: where the assistant lives and what it controls.

The other layers are the assistant itself (core, the owner's device layer, outside users'
user layer). The environment is everything outside the assistant's own folders: the OS, its
settings, apps and files. Controlling it is what the assistant is for, so each device offers
its environment to the agents (environment_tools.py over the node link, beside the device's
shell), and every change made through it is recorded here, on the device it changed:

  <user data>/environment/changes.json   one entry per change: what, why, kind, how to undo
  <user data>/environment/snapshots/     the file as it was before each file change

File changes are undone exactly from their snapshot. Changes made another way (a command, a
setting changed through an app) are recorded with how to re-check and undo them.

What may change without asking is the user's choice, per kind of change ("environment_policy" in
settings.json: {"<kind>": "allow" | "confirm"}); a change of a kind set to confirm returns
needs_confirmation until it is repeated with confirm=true.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from . import layers
from .settings import settings

KINDS = ("appearance", "keybindings", "windows", "input", "desktop", "apps", "packages",
         "automation", "files", "system")
# Until the user draws the line: everything is allowed (recorded and undoable) except system-level
# changes (root / sudo).
DEFAULT_POLICY = {kind: "allow" for kind in KINDS} | {"system": "confirm"}
MAX_FILE_BYTES = 4 * 1024 * 1024


def root() -> Path:
    return layers.user_root() / "environment"


def _journal() -> Path:
    return root() / "changes.json"


def changes() -> list[dict[str, Any]]:
    return layers._read(_journal(), [])


def policy() -> dict[str, str]:
    try:
        configured = json.loads(settings.user_settings_file.read_text(encoding="utf-8")).get("environment_policy") or {}
    except (OSError, ValueError):
        configured = {}
    return DEFAULT_POLICY | {k: v for k, v in configured.items() if k in KINDS and v in ("allow", "confirm")}


def needs_confirmation(kind: str, confirm: bool) -> dict[str, Any] | None:
    if kind not in KINDS:
        raise ValueError("kind: " + ", ".join(KINDS))
    if policy()[kind] == "confirm" and not confirm:
        return {"status": "needs_confirmation", "kind": kind,
                "note": "The user asked to confirm this kind of change: ask them, then repeat with confirm=true."}
    return None


def _own_folders() -> list[Path]:
    """The assistant's own folders: changes there belong to its layers, not the environment."""
    return [layers.install_root().resolve(), layers.user_root().resolve()]


def resolve(path: str) -> Path:
    target = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    for own in _own_folders():
        if target == own or own in target.parents:
            raise ValueError(f"{target} is the assistant's own folder, not the environment "
                             "(use the layer tools: record_device_fix, the user layer, propose_change)")
    omarchy_pkg = Path("/usr/share/omarchy")
    if target == omarchy_pkg or omarchy_pkg in target.parents:
        raise ValueError(f"{target} is read-only Omarchy package data overwritten on update; "
                         "edit user copies under ~/.config/ (hypr/, omarchy/, foot/) or clone/overlay instead")
    return target


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _record(entry: dict[str, Any]) -> dict[str, Any]:
    entry = {"id": uuid.uuid4().hex[:12], "at": layers.now(), "device": platform.node(), **entry}
    layers._write(_journal(), [*changes(), entry])
    return entry


def change_file(path: str, content: str | None, *, why: str, kind: str, agent: str = "") -> dict[str, Any]:
    """Write (or with content None, delete) a file in the environment, snapshotting it first.
    Hyprland config is validated after the write and restored if the change broke it."""
    target = resolve(path)
    before = target.read_bytes() if target.is_file() else None
    if target.exists() and not target.is_file():
        raise ValueError(f"{target} is not a file")
    if before is not None and len(before) > MAX_FILE_BYTES:
        raise ValueError(f"{target} is too large to change through the environment layer")
    snapshot = None
    if before is not None:
        snapshot = root() / "snapshots" / f"{uuid.uuid4().hex[:12]}{target.suffix}"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(before)
    if content is None:
        if before is None:
            raise ValueError(f"{target} does not exist")
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    problem = _validate(target)
    if problem:
        _restore(target, before)
        _validate(target)  # reload the restored version
        if snapshot:
            snapshot.unlink(missing_ok=True)
        return {"status": "reverted", "path": str(target), "error": problem}
    after = None if content is None else target.read_bytes()
    entry = _record({"type": "file", "action": "delete" if content is None else ("edit" if before is not None else "create"),
                     "path": str(target), "kind": kind, "why": why.strip()[:600], "agent": agent,
                     "snapshot": str(snapshot) if snapshot else None, "after_sha256": _sha(after)})
    return {"status": "changed", "change": entry}


def _restore(target: Path, before: bytes | None) -> None:
    if before is None:
        target.unlink(missing_ok=True)
    else:
        target.write_bytes(before)


def _validate(target: Path) -> str | None:
    """Environment-specific checks after a file change; an error message when it broke something."""
    hypr = Path.home() / ".config" / "hypr"
    omarchy_cfg = Path.home() / ".config" / "omarchy"
    if (target == hypr or hypr in target.parents) and target.suffix == ".lua" and shutil.which("hyprctl"):
        subprocess.run(["hyprctl", "reload"], capture_output=True, text=True, timeout=20)
        errors = subprocess.run(["hyprctl", "configerrors"], capture_output=True, text=True, timeout=20).stdout.strip()
        if errors:
            return "Hyprland rejected the change, so it was put back: " + errors[:1500]
    if (target == hypr or hypr in target.parents) and target.name == "hyprsunset.conf" and target.is_file() and shutil.which("omarchy"):
        subprocess.run(["omarchy", "restart", "hyprsunset"], capture_output=True, text=True, timeout=20)
    if (target == omarchy_cfg or omarchy_cfg in target.parents) and target.suffix == ".json" and target.is_file():
        try:
            json.loads(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            return f"Omarchy JSON config ({target.name}) is invalid JSON, so it was put back: {exc}"
    return None


def record(summary: str, *, kind: str, why: str, recheck: str, undo: str, agent: str = "") -> dict[str, Any]:
    """Record a change made another way (a command, an app's own settings): how to check and undo it."""
    if kind not in KINDS:
        raise ValueError("kind: " + ", ".join(KINDS))
    if not summary.strip() or not undo.strip():
        raise ValueError("a change needs a summary and how to undo it")
    return _record({"type": "action", "summary": summary.strip()[:300], "kind": kind, "why": why.strip()[:600],
                    "recheck": recheck.strip()[:600], "undo": undo.strip()[:1000], "agent": agent})


def undo(change_id: str, *, force: bool = False) -> dict[str, Any]:
    entry = next((c for c in changes() if c["id"] == change_id), None)
    if entry is None:
        raise ValueError(f"no change {change_id}")
    if entry.get("undone_at"):
        return {"status": "already_undone", "change": entry}
    if entry["type"] == "action":
        return {"status": "manual", "undo": entry["undo"],
                "note": "Run this to undo it, check it worked, then call undo_environment_change(id, force=true) to mark it undone."
                if not force else "Marked undone."} | (_mark_undone(change_id) if force else {})
    target = Path(entry["path"])
    current = target.read_bytes() if target.is_file() else None
    if _sha(current) != entry.get("after_sha256") and not force:
        return {"status": "changed_since", "path": str(target),
                "note": "The file changed after this change was made; undoing would also discard those later edits. "
                        "Repeat with force=true only if that's intended."}
    before = Path(entry["snapshot"]).read_bytes() if entry.get("snapshot") else None
    _restore(target, before)
    problem = _validate(target)
    return {"status": "undone", "path": str(target), **({"warning": problem} if problem else {})} | _mark_undone(change_id)


def _mark_undone(change_id: str) -> dict[str, Any]:
    entries = changes()
    for c in entries:
        if c["id"] == change_id:
            c["undone_at"] = layers.now()
    layers._write(_journal(), entries)
    return {"undone_at": layers.now()}


# -- what this environment is --------------------------------------------------------

def _run(command: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _os_release() -> dict[str, str]:
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return {k: v.strip('"') for k, _, v in (line.partition("=") for line in lines) if k}


def profile() -> dict[str, Any]:
    """The environment on this device: OS, desktop, tools, where its settings live and how to apply them."""
    info: dict[str, Any] = {"device": platform.node(), "os": platform.system(), "release": platform.release(),
                            "home": str(Path.home()), "assistant_folders": [str(p) for p in _own_folders()],
                            "policy": policy(), "change_kinds": list(KINDS)}
    if sys.platform == "win32":
        info["package_managers"] = [m for m in ("winget", "choco", "scoop") if shutil.which(m)]
        info["shells"] = [s for s in ("powershell", "pwsh", "cmd") if shutil.which(s)]
        return info
    release = _os_release()
    info["distribution"] = release.get("PRETTY_NAME") or release.get("NAME", "")
    info["shells"] = [s for s in ("bash", "zsh", "fish") if shutil.which(s)]
    info["package_managers"] = [m for m in ("pacman", "yay", "paru", "apt", "dnf", "zypper", "flatpak") if shutil.which(m)]
    info["desktop"] = os.environ.get("XDG_CURRENT_DESKTOP", "")
    info["session"] = os.environ.get("XDG_SESSION_TYPE", "")
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or shutil.which("hyprctl"):
        try:
            version = json.loads(_run(["hyprctl", "version", "-j"]) or "{}").get("version", "")
        except ValueError:
            version = ""
        info["compositor"] = {"name": "Hyprland", "version": version,
                              "config": "~/.config/hypr/ (validated with `hyprctl configerrors` after each change)"}
    if shutil.which("omarchy"):
        modules = Path(os.environ.get("ULTIMATE_ASSISTANT_MODULES_DIR") or settings.user_data_dir / "modules")
        try:
            state = json.loads((modules / "omarchy" / "state.json").read_text(encoding="utf-8"))
            installed = bool(state.get("release")) and state.get("status") in {"ready", "needs_setup"}
        except (OSError, ValueError):
            installed = False
        info["omarchy"] = {"module_installed": installed}
    return info
