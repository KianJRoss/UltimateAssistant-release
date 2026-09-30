"""Safe, idempotent discovery of locally available Herald backends."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

from herald.router.registry import Registry


CLI_BACKENDS: tuple[dict[str, Any], ...] = (
    {"name": "claude-cli", "cli": "claude", "priority": 20, "capabilities": {"code": True, "reasoning": True}},
    {"name": "antigravity", "cli": "antigravity", "priority": 30, "capabilities": {"reasoning": True}},
    {"name": "antigravity-gemini", "cli": "profile_gemini", "priority": 31, "capabilities": {"reasoning": True, "fast": True}},
    {"name": "antigravity-claude", "cli": "profile_claude", "priority": 32, "capabilities": {"code": True, "reasoning": True}},
    {"name": "antigravity-gpt", "cli": "profile_gpt", "priority": 33, "capabilities": {"reasoning": True}},
)

# Path to the built-in coding tools MCP server.  Resolved relative to this
# file so it works regardless of cwd.
_CODING_TOOLS_PATH = Path(__file__).resolve().parent.parent / "coding_tools.py"
_CODING_TOOLS_INSTANCE_NAME = "herald-coding-tools"

# Path to the built-in file/picture ingestion MCP server.
_INGEST_TOOLS_PATH = Path(__file__).resolve().parent.parent / "ingest_tools.py"
_INGEST_TOOLS_INSTANCE_NAME = "herald-ingest-tools"

# Path to the built-in math/science calculation MCP server.
_MATH_TOOLS_PATH = Path(__file__).resolve().parent.parent / "math_tools.py"
_MATH_TOOLS_INSTANCE_NAME = "herald-math-tools"

# Path to the built-in model-consultation (parallel/nested delegation) MCP server.
_CONSULT_TOOLS_PATH = Path(__file__).resolve().parent.parent / "consult_tools.py"
_CONSULT_TOOLS_INSTANCE_NAME = "herald-consult-tools"

# Path to the built-in web-search MCP server (no API key -- DuckDuckGo HTML).
_WEBSEARCH_TOOLS_PATH = Path(__file__).resolve().parent.parent / "websearch_tools.py"
_WEBSEARCH_TOOLS_INSTANCE_NAME = "herald-websearch-tools"


def _path_with_cli_locations() -> str:
    locations = [
        Path.home() / "AppData" / "Roaming" / "npm",
        Path.home() / "AppData" / "Local" / "agy" / "bin",
        Path.home() / ".local" / "bin",
        Path("/usr/local/bin"),
    ]
    return os.pathsep.join([*(str(path) for path in locations), os.environ.get("PATH", "")])


def _cli_is_available(clink_name: str) -> bool:
    try:
        from herald.router.adapters import cfg_dir  # noqa: F401 - configures clink path
        from clink.registry import ClinkRegistry

        executable = ClinkRegistry().get_client(clink_name).executable[0]
    except Exception:
        return False
    return shutil.which(executable, path=_path_with_cli_locations()) is not None


def _g4f_models(url: str) -> list[str]:
    try:
        response = httpx.get(f"{url.rstrip('/')}/models", timeout=2)
        response.raise_for_status()
        return [row["id"] for row in response.json().get("data", []) if row.get("id")]
    except Exception:
        return []


def _register_coding_tools(registry_obj: Any) -> str | None:
    """Register the built-in coding-tools MCP server as a global tool instance.

    Uses the ToolRegistry imported lazily to avoid circular imports at module
    load time.  Returns the instance name on success, None if registration fails.
    """
    if not _CODING_TOOLS_PATH.exists():
        return None
    try:
        from herald.router.tool_registry import ToolRegistry
        tr = ToolRegistry()
        python_exe = sys.executable
        workspace = os.environ.get("HERALD_WORKSPACE", str(Path.cwd()))
        tr.register_tool_instance(
            name=_CODING_TOOLS_INSTANCE_NAME,
            transport="stdio",
            config={
                "command": [python_exe, str(_CODING_TOOLS_PATH)],
                "env": {"HERALD_WORKSPACE": workspace},
                "timeout": 30,
            },
            description=(
                "Built-in Herald coding tools: read_file, write_file, edit_file, "
                "list_directory, run_command, search_code, git_run.  "
                "Available to all agentic sessions in global scope."
            ),
            tags=["coding", "filesystem", "shell", "git", "built-in"],
            scope="global",
        )
        return _CODING_TOOLS_INSTANCE_NAME
    except Exception:  # noqa: BLE001 — bootstrap must never crash the router
        return None


def _register_ingest_tools(registry_obj: Any) -> str | None:
    """Register the built-in file/picture ingestion MCP server as a global
    tool instance. Same pattern as _register_coding_tools, separate process
    since its extraction dependencies (RapidOCR, PyMuPDF, etc.) are heavier."""
    if not _INGEST_TOOLS_PATH.exists():
        return None
    try:
        from herald.router.tool_registry import ToolRegistry
        tr = ToolRegistry()
        python_exe = sys.executable
        workspace = os.environ.get("HERALD_WORKSPACE", str(Path.cwd()))
        tr.register_tool_instance(
            name=_INGEST_TOOLS_INSTANCE_NAME,
            transport="stdio",
            config={
                "command": [python_exe, str(_INGEST_TOOLS_PATH)],
                "env": {"HERALD_WORKSPACE": workspace},
                "timeout": 60,
            },
            description=(
                "Built-in Herald file/picture ingestion: ingest_file extracts "
                "text, structure, and a summary from images, PDFs, Office docs, "
                "text/CSV/JSON, and archives. Available to all agentic sessions "
                "in global scope."
            ),
            tags=["ingestion", "ocr", "vision", "documents", "built-in"],
            scope="global",
        )
        return _INGEST_TOOLS_INSTANCE_NAME
    except Exception:  # noqa: BLE001 — bootstrap must never crash the router
        return None


def _register_math_tools(registry_obj: Any) -> str | None:
    """Register the built-in math/science calculation MCP server as a
    global tool instance. Same pattern as _register_coding_tools/
    _register_ingest_tools, separate process since sympy/matplotlib are
    only needed here."""
    if not _MATH_TOOLS_PATH.exists():
        return None
    try:
        from herald.router.tool_registry import ToolRegistry
        tr = ToolRegistry()
        python_exe = sys.executable
        workspace = os.environ.get("HERALD_WORKSPACE", str(Path.cwd()))
        tr.register_tool_instance(
            name=_MATH_TOOLS_INSTANCE_NAME,
            transport="stdio",
            config={
                "command": [python_exe, str(_MATH_TOOLS_PATH)],
                "env": {"HERALD_WORKSPACE": workspace},
                "timeout": 30,
            },
            description=(
                "Built-in Herald math/science tools: solve_math (calculus/"
                "algebra, symbolic, LaTeX-formatted), sketch_graph (curve "
                "analysis + plot image), chemistry_calc (molar mass, "
                "stoichiometry, Beer's law, dilution). Available to all "
                "agentic sessions in global scope."
            ),
            tags=["math", "chemistry", "calculus", "sympy", "built-in"],
            scope="global",
        )
        return _MATH_TOOLS_INSTANCE_NAME
    except Exception:  # noqa: BLE001 — bootstrap must never crash the router
        return None


def _register_consult_tools(registry_obj: Any) -> str | None:
    """Register the built-in model-consultation MCP server as a global tool
    instance. Same pattern as _register_coding_tools/_register_ingest_tools/
    _register_math_tools -- gives any agentic session (native CLI or Herald
    harness) a consult_models tool for parallel/nested delegation to other
    Herald-routed models, mirroring the PAL MCP server's nested-consult
    pattern."""
    if not _CONSULT_TOOLS_PATH.exists():
        return None
    try:
        from herald.router.tool_registry import ToolRegistry
        tr = ToolRegistry()
        python_exe = sys.executable
        workspace = os.environ.get("HERALD_WORKSPACE", str(Path.cwd()))
        tr.register_tool_instance(
            name=_CONSULT_TOOLS_INSTANCE_NAME,
            transport="stdio",
            config={
                "command": [python_exe, str(_CONSULT_TOOLS_PATH)],
                "env": {"HERALD_WORKSPACE": workspace},
                "timeout": 1800,
            },
            description=(
                "Built-in Herald model-consultation tool: consult_models "
                "delegates one or more tasks to other Herald-routed models, "
                "in parallel or as a sequential refinement chain, with a "
                "depth cap against runaway recursion. Available to all "
                "agentic sessions in global scope."
            ),
            tags=["consult", "delegation", "multi-model", "built-in"],
            scope="global",
        )
        return _CONSULT_TOOLS_INSTANCE_NAME
    except Exception:  # noqa: BLE001 — bootstrap must never crash the router
        return None


def _register_websearch_tools(registry_obj: Any) -> str | None:
    """Register the built-in web-search MCP server as a global tool
    instance. Same pattern as the other built-in tool registrations. No
    API key needed (DuckDuckGo HTML endpoint) -- gives agentic sessions a
    real way to search outward instead of only introspecting the local
    workspace, for the "search broad, then condense" research pattern."""
    if not _WEBSEARCH_TOOLS_PATH.exists():
        return None
    try:
        from herald.router.tool_registry import ToolRegistry
        tr = ToolRegistry()
        python_exe = sys.executable
        workspace = os.environ.get("HERALD_WORKSPACE", str(Path.cwd()))
        tr.register_tool_instance(
            name=_WEBSEARCH_TOOLS_INSTANCE_NAME,
            transport="stdio",
            config={
                "command": [python_exe, str(_WEBSEARCH_TOOLS_PATH)],
                "env": {"HERALD_WORKSPACE": workspace},
                "timeout": 30,
            },
            description=(
                "Built-in Herald web search: web_search(query, num_results) "
                "searches the live web (no API key) and returns title/url/"
                "snippet results. Available to all agentic sessions in "
                "global scope."
            ),
            tags=["search", "web", "research", "built-in"],
            scope="global",
        )
        return _WEBSEARCH_TOOLS_INSTANCE_NAME
    except Exception:  # noqa: BLE001 — bootstrap must never crash the router
        return None


def bootstrap_registry(registry: Registry) -> dict[str, list[str]]:
    """Discover usable local CLIs, G4F gateways, and built-in coding tools."""
    registered: list[str] = []
    skipped: list[str] = []
    if os.environ.get("HERALD_SKIP_BACKEND_DISCOVERY") == "1":
        return {"registered": [], "skipped": ["automatic backend discovery disabled"]}
    registry.remove("qwen-cli")

    for spec in CLI_BACKENDS:
        if not _cli_is_available(spec["cli"]):
            skipped.append(spec["name"])
            continue
        registry.register(
            backend_type="cli",
            name=spec["name"],
            config={"cli_name": spec["cli"]},
            capabilities=spec["capabilities"],
            priority=spec["priority"],
        )
        registered.append(spec["name"])

    if _cli_is_available("codex"):
        primary_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        registry.register(
            backend_type="cli", name="codex-primary",
            config={"cli_name": "codex", "env": {"CODEX_HOME": str(primary_home)}},
            capabilities={"code": True, "reasoning": True}, priority=15,
            pool_name="codex-cli",
        )
        registered.append("codex-primary")

        backup_home = Path(os.environ.get("HERALD_CODEX_BACKUP_HOME", Path.home() / ".codex_backup"))
        registry.register(
            backend_type="cli", name="codex-backup",
            config={"cli_name": "codex", "env": {"CODEX_HOME": str(backup_home)}},
            capabilities={"code": True, "reasoning": True}, priority=16,
            enabled=backup_home.is_dir(), pool_name="codex-cli",
        )
        registered.append("codex-backup")

    gateway_url = os.environ.get("HERALD_G4F_GATEWAY_URL", "http://127.0.0.1:4900/v1")
    for priority, model in enumerate(_g4f_models(gateway_url), start=10):
        account = model.split("/", 1)[0].removeprefix("g4f-")
        name = f"g4f-{account}"
        registry.register(
            backend_type="browser_session", name=name,
            config={"gateway_url": gateway_url, "model": model},
            capabilities={"chat": True, "free": True, "browser_session": True},
            priority=priority, pool_name="g4f-gateway",
        )
        registered.append(name)

    free_url = os.environ.get("HERALD_G4F_FREE_URL", "http://127.0.0.1:4901/v1")
    free_models = _g4f_models(free_url)
    if free_models:
        preferred = next((m for m in free_models if m == "gemini-2.5-flash"), free_models[0])
        registry.register(
            backend_type="browser_session", name="g4f-free",
            config={"gateway_url": free_url, "model": preferred},
            capabilities={"chat": True, "fast": True, "free": True},
            priority=90, pool_name="g4f-gateway",
        )
        registered.append("g4f-free")

    # Always register the built-in coding tools so agentic sessions can
    # read/write files, run shell commands, search code, and use git without
    # any manual tool-instance configuration.
    coding_name = _register_coding_tools(registry)
    if coding_name:
        registered.append(coding_name)
    else:
        skipped.append(_CODING_TOOLS_INSTANCE_NAME)

    # Always register the built-in ingestion tool so agentic sessions can
    # extract text/structure from images, PDFs, and documents without any
    # manual tool-instance configuration.
    ingest_name = _register_ingest_tools(registry)
    if ingest_name:
        registered.append(ingest_name)
    else:
        skipped.append(_INGEST_TOOLS_INSTANCE_NAME)

    # Always register the built-in math/science tools so agentic sessions
    # can solve calculus/algebra and chemistry problems without any manual
    # tool-instance configuration.
    math_name = _register_math_tools(registry)
    if math_name:
        registered.append(math_name)
    else:
        skipped.append(_MATH_TOOLS_INSTANCE_NAME)

    # Always register the built-in model-consultation tool so any agentic
    # session (native CLI or Herald's own harness) can delegate to other
    # models in parallel or nested depth without manual configuration.
    consult_name = _register_consult_tools(registry)
    if consult_name:
        registered.append(consult_name)
    else:
        skipped.append(_CONSULT_TOOLS_INSTANCE_NAME)

    # Always register the built-in web-search tool so agentic sessions can
    # search outward for real, current information instead of relying only
    # on the local workspace or training-data guesses.
    websearch_name = _register_websearch_tools(registry)
    if websearch_name:
        registered.append(websearch_name)
    else:
        skipped.append(_WEBSEARCH_TOOLS_INSTANCE_NAME)

    return {"registered": registered, "skipped": skipped}
