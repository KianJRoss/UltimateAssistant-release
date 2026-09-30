"""Project scaffolding used by ``herald init``."""
from __future__ import annotations

import re
from pathlib import Path


def project_name_from_path(path: Path) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", path.name).strip("-").lower()
    return name or "herald-project"


def create_project_scaffold(
    path: str | Path, *, name: str | None = None, force: bool = False,
) -> list[Path]:
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_name = name or project_name_from_path(root)
    manifest = root / "router.yaml"
    if manifest.exists() and not force:
        raise FileExistsError(f"{manifest} already exists (use --force to replace it)")

    manifest.write_text(
        "# Herald project manifest\n"
        f"project: {project_name}\n"
        "description: \"\"\n\n"
        "tools: []\n"
        "global_tools: []\n\n"
        "parts:\n"
        "  main:\n"
        "    description: Primary software agent\n"
        "    tools: []\n"
        "    routing:\n"
        "      prefer: [claude-cli, codex-cli]\n",
        encoding="utf-8",
    )
    tools_dir = root / ".herald" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    keep = tools_dir / ".gitkeep"
    keep.touch(exist_ok=True)
    flows_dir = root / ".herald" / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    starter_flow = flows_dir / "feature.yaml"
    if force or not starter_flow.exists():
        starter_flow.write_text(
            "version: 1\n"
            "name: feature\n"
            "mode: efficiency\n"
            "agents:\n"
            "  Director: {model: auto, memory: director}\n"
            "  Reviewer: {model: g4f-gateway, memory: reviewer}\n"
            "flow:\n"
            "  - Director\n"
            "  - Reviewer\n"
            "  - Director\n"
            "  - output\n"
            "rules:\n"
            "  Director: Coordinate the work and return the final answer.\n"
            "  Reviewer: Find defects and missing tests.\n",
            encoding="utf-8",
        )
    return [manifest, keep, starter_flow]
