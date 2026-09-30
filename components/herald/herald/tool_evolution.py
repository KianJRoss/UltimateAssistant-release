"""Local-first synthesized tool artifacts and guarded upstream review."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

DEFAULT_TOOLS_DIR = Path.home() / ".herald" / "tools"
_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
_SECRET = re.compile(r"(api[_-]?key|token|secret|password|authorization|cookie)", re.I)
_PATH = re.compile(r"(?:[A-Za-z]:\\|/home/|/Users/)[^\s'\"]+")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if _SECRET.search(str(key)) else _sanitize(nested)) for key, nested in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _PATH.sub("[LOCAL_PATH]", _SECRET.sub("redacted", value))
    return value


def synthesize_local_tool(name: str, description: str, input_schema: dict[str, Any], *,
                          implementation: str = "", tools_dir: str | Path = DEFAULT_TOOLS_DIR) -> Path:
    """Create a reviewable local tool manifest; never sends it upstream."""
    if not _NAME.fullmatch(name):
        raise ValueError("tool name must be 1-64 alphanumeric, underscore, or hyphen characters")
    root = Path(tools_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 1, "name": name, "description": description,
        "input_schema": input_schema, "implementation": implementation,
        "local_only": True,
    }
    target = root / f"{name}.json"
    temporary = root / f".{name}.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)
    return target


def prepare_upstream_review(manifest: dict[str, Any], *,
                            confirm: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """Apply both sharing gates and return only a sanitized review payload."""
    if os.environ.get("HERALD_SHARE_TOOL_EVOLUTIONS") != "1":
        raise PermissionError("set HERALD_SHARE_TOOL_EVOLUTIONS=1 to opt in to upstream review")
    if confirm is None or not confirm(f"Share sanitized tool '{manifest.get('name', 'unnamed')}' for upstream review?"):
        raise PermissionError("per-tool upstream sharing confirmation was declined")
    allowed = {key: manifest.get(key) for key in ("version", "name", "description", "input_schema", "implementation") if key in manifest}
    return _sanitize(allowed)
