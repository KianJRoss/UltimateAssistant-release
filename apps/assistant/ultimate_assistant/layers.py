"""Where the assistant's parts live, so updates can replace the core without losing anyone's work.

- Core: the released app (apps/, components/ in the install folder). Each release ships
  apps/assistant/core-manifest.json with every file's SHA-256; updates replace the core whole.
  Nothing should edit it on a user's machine. A checkout without a manifest is a development
  copy (the developer's own, changed on purpose) and isn't checked.
- Device layer (user data/device): what setup and the assistant did to make this machine work,
  since every install differs (paths, drivers, missing tools, existing apps). Each fix is
  recorded with how to re-check it, and is re-checked after an update.
- User layer: what the user built or taught it. Their agents (agents/), skills (skills/),
  extensions (extensions/: scripts, automations, their own modules) and settings. Updates never
  touch it.
- Contributions (user data/contributions): a change the core needs, or something the user built
  that others could use, written up to send to the developer, only when the user says so.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .settings import settings

MANIFEST = Path("apps") / "assistant" / "core-manifest.json"
CORE_DIRS = ("apps", "components")
# Files setup adds inside the core folders (voice models, caches) aren't core changes.
IGNORED_PARTS = {"__pycache__", ".venv", "venv", "node_modules", ".runtime", ".playwright-mcp"}
IGNORED_PREFIXES = ("apps/assistant/models/",)
SOURCE_SUFFIXES = {".py", ".ps1", ".cmd", ".bat", ".json", ".md", ".html", ".js", ".css", ".toml", ".txt"}
FIX_KINDS = ("config", "dependency", "path", "driver", "workaround", "core-edit")
CONTRIBUTION_KINDS = ("core-change", "extension", "device-fix")
MAX_CONTRIBUTION_BYTES = 512 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def install_root() -> Path:
    return settings.workspace_root


def user_root() -> Path:
    import os
    # Tools started by the Router get the app's own location (assistant.py), whatever its environment.
    configured = os.environ.get("ULTIMATE_ASSISTANT_USER_ROOT")
    return Path(configured) if configured else settings.user_settings_file.parent


def device_root() -> Path:
    return user_root() / "device"


def extensions_root() -> Path:
    return user_root() / "extensions"


def contributions_root() -> Path:
    return user_root() / "contributions"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def core_files(root: Path) -> dict[str, str]:
    """Every shipped core file under root and its SHA-256 (what build-installer writes)."""
    out = {}
    for top in CORE_DIRS:
        for path in sorted((root / top).rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_file() and not IGNORED_PARTS & set(path.parts) and relative != MANIFEST.as_posix() \
                    and path.suffix not in {".pyc", ".pyo"}:
                out[relative] = _sha256(path)
    return out


def write_manifest(root: Path, version: str) -> Path:
    target = root / MANIFEST
    target.write_text(json.dumps({"version": version, "files": core_files(root)}, indent=1) + "\n", "utf-8")
    return target


def core_status(root: Path | None = None) -> dict[str, Any]:
    """{"status": "intact" | "modified" | "development", "version", "modified", "missing", "added"}."""
    root = root or install_root()
    manifest_file = root / MANIFEST
    if not manifest_file.exists():
        return {"status": "development", "detail": "A development copy: no release manifest, so it isn't checked."}
    manifest = json.loads(manifest_file.read_text("utf-8"))
    expected: dict[str, str] = manifest["files"]
    modified, missing = [], []
    for relative, digest in expected.items():
        path = root / relative
        if not path.is_file():
            missing.append(relative)
        elif _sha256(path) != digest:
            modified.append(relative)
    added = [relative for relative in core_files(root) if relative not in expected
             and Path(relative).suffix in SOURCE_SUFFIXES and not relative.startswith(IGNORED_PREFIXES)]
    changed = bool(modified or missing or added)
    return {"status": "modified" if changed else "intact", "version": manifest.get("version"),
            "modified": modified, "missing": missing, "added": added,
            **({"detail": "This copy of the assistant's core has been changed. Updates replace the core, so "
                          "these changes will be lost and may stop the update from working. Keep changes "
                          "in the device or user layer, and send the ones the core needs to the developer."}
               if changed else {})}


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", "utf-8")
    temporary.replace(path)


def device_fixes() -> list[dict[str, Any]]:
    return _read(device_root() / "fixes.json", [])


def record_device_fix(summary: str, *, reason: str, kind: str, recheck: str, touched: list[str] | None = None) -> dict[str, Any]:
    """Record something done to make this device work (kept and re-checked across updates)."""
    if kind not in FIX_KINDS:
        raise ValueError("kind: " + ", ".join(FIX_KINDS))
    if not summary.strip() or not recheck.strip():
        raise ValueError("a fix needs a summary and how to re-check it")
    fixes = device_fixes()
    fix = {"id": uuid.uuid4().hex[:12], "at": now(), "summary": summary.strip()[:300], "reason": reason.strip()[:1000],
           "kind": kind, "recheck": recheck.strip()[:1000], "touched": [str(t)[:300] for t in (touched or [])][:20],
           "core_version": _version()}
    _write(device_root() / "fixes.json", [*fixes, fix])
    return fix


def remove_device_fix(fix_id: str) -> bool:
    fixes = device_fixes()
    kept = [f for f in fixes if f["id"] != fix_id]
    _write(device_root() / "fixes.json", kept)
    return len(kept) != len(fixes)


def _version() -> str:
    version = install_root() / "apps" / "assistant" / "VERSION"
    return version.read_text("utf-8-sig").strip() if version.exists() else "development"


def after_start() -> dict[str, Any] | None:
    """Once per start: when the core version changed and this device has recorded fixes, add a
    task to re-check them (an update can make a fix unnecessary or break it). Returns the task."""
    state_file = device_root() / "state.json"
    state = _read(state_file, {})
    version, seen = _version(), state.get("core_version")
    _write(state_file, {**state, "core_version": version, "started_at": now()})
    fixes = device_fixes()
    if not seen or seen == version or not fixes:
        return None
    import os
    from .task_store import TaskStore
    store = TaskStore(Path(os.environ.get("ULTIMATE_ASSISTANT_TASKS_DB", str(settings.tasks_db))))
    steps = "\n".join(f"- {f['summary']}: {f['recheck']}" for f in fixes)
    return store.add_task(f"Re-check this device's setup after updating to {version}",
                          details=f"Updated from {seen}. Check each fix still works and is still needed; remove "
                                  f"ones the new version made unnecessary (record_device_fix / remove_device_fix):\n{steps}",
                          domain="assistant", kind="setup", source="assistant", deduplicate=True)


def layers_status() -> dict[str, Any]:
    def count(path: Path) -> int:
        return sum(1 for p in path.rglob("*") if p.is_file()) if path.exists() else 0
    return {"core": core_status(), "device": {"folder": str(device_root()), "fixes": device_fixes()},
            "user": {"agents": count(user_root() / "agents"), "skills": count(user_root() / "skills"),
                     "extensions": count(extensions_root()), "extensions_folder": str(extensions_root())},
            "contributions": [{k: c.get(k) for k in ("id", "kind", "title", "created_at", "sent_at")}
                              for c in contributions()]}


def contributions() -> list[dict[str, Any]]:
    folder = contributions_root()
    return sorted((_read(p, {}) for p in folder.glob("*.json")), key=lambda c: c.get("created_at", "")) if folder.exists() else []


def propose(kind: str, title: str, why: str, files: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Write up a change for the developer: kind core-change (what the core needs and why, with a
    diff or the changed text), extension (something the user built), or device-fix (what this kind
    of device needed). Kept locally until the user chooses to send it."""
    if kind not in CONTRIBUTION_KINDS:
        raise ValueError("kind: " + ", ".join(CONTRIBUTION_KINDS))
    if not title.strip() or not why.strip():
        raise ValueError("a contribution needs a title and why")
    cleaned = [{"path": str(f.get("path", ""))[:300], "content": str(f.get("content", ""))} for f in (files or [])][:20]
    item = {"schema_version": 1, "id": uuid.uuid4().hex[:12], "created_at": now(), "kind": kind,
            "title": title.strip()[:200], "why": why.strip()[:4000], "app_version": _version(), "files": cleaned}
    if len(json.dumps(item).encode()) > MAX_CONTRIBUTION_BYTES:
        raise ValueError("too large to send (512 KB): send a diff or the changed parts")
    _write(contributions_root() / f"{item['id']}.json", item)
    return item


def contribution(contribution_id: str) -> dict[str, Any]:
    path = contributions_root() / f"{contribution_id}.json"
    if not path.exists():
        raise ValueError("no contribution has that id")
    return _read(path, {})


def payload(item: dict[str, Any]) -> dict[str, Any]:
    """Exactly what is sent: the write-up and files, never the user's data, settings or history."""
    return {k: item[k] for k in ("schema_version", "id", "created_at", "kind", "title", "why", "app_version", "files")}


def digest(item: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload(item), sort_keys=True).encode()).hexdigest()[:16]


def send(contribution_id: str, confirmation: str) -> dict[str, Any]:
    """Send a contribution the user has seen (preview) and agreed to send."""
    import io
    import zipfile

    import httpx
    from .diagnostics import destination
    item = contribution(contribution_id)
    if confirmation != digest(item):
        raise ValueError("show the user the preview and pass its confirmation code once they agree")
    url = destination().get("url")
    if not url:
        raise ValueError("no developer address is configured for this build")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("contribution.json", json.dumps(payload(item), indent=2, ensure_ascii=False))
    response = httpx.post(url, content=output.getvalue(), headers={"Content-Type": "application/zip"}, timeout=30)
    response.raise_for_status()
    item.update(sent_at=now(), receipt=response.json().get("receipt"))
    _write(contributions_root() / f"{contribution_id}.json", item)
    return {"status": "sent", "receipt": item["receipt"]}
