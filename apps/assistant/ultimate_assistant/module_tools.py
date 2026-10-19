"""Assistant-facing lifecycle tools; catalog discovery has no installation effects."""
import json
from contextlib import contextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .module_manager import ModuleManager

mcp = FastMCP("ultimate-assistant-modules")


@mcp.tool()
def discover_optional_integrations(query: str = "") -> str:
    """Find researched upstream projects and setup recipes without installing. Candidates are not packaged modules or verified live connections. Recheck upstream docs and pin versions before native CLI setup."""
    catalog = json.loads(Path(__file__).with_name("integration_recipes.json").read_text("utf-8"))
    terms = query.casefold().split()
    candidates = [entry for entry in catalog["integrations"]
                  if all(term in json.dumps(entry).casefold() for term in terms)]
    return json.dumps({"reviewed_on": catalog["reviewed_on"], "integrations": candidates,
                       "installed_by_discovery": False})


@contextmanager
def _locked(manager):
    manager.root.mkdir(parents=True, exist_ok=True)
    lock = manager.root / "operation.lock"
    handle = lock.open("a+b")
    handle.seek(0, 2)
    if not handle.tell():
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    import os
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another module operation is running; retry when it finishes") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@mcp.tool()
def discover_optional_modules(query: str = "") -> str:
    """Find installable modules and their status without installing anything. Empty catalog means no published modules yet."""
    manager = ModuleManager()
    items = []
    for entry in manager.catalog():
        if query.casefold() in json.dumps(entry).casefold():
            state = manager.state(entry["id"])
            items.append({**entry, "status": state["status"], "enabled": state.get("enabled", False)})
    return json.dumps({"modules": items})


@mcp.tool()
def manage_optional_module(module_id: str, action: str) -> str:
    """Install/update/enable/disable/remove/rollback a module only for a user-requested capability. Explain costs and account access before setup. Removal keeps user data. A new turn discovers enabled tools."""
    manager = ModuleManager()
    operations = {"install": manager.install, "update": manager.install, "enable": manager.enable,
                  "disable": manager.disable, "remove": manager.remove, "rollback": manager.rollback}
    if action == "status":
        state = manager.state(module_id)
        return json.dumps({"module": module_id, "status": state["status"], "enabled": state.get("enabled", False),
                           "installation": state.get("release"), "data_directory": str(manager.path(module_id) / "data"),
                           "setup": state.get("manifest", {}).get("setup", [])})
    if action not in operations:
        raise ValueError("Use install, update, enable, disable, remove, rollback, or status")
    with _locked(manager):
        return json.dumps(operations[action](module_id))


@mcp.tool()
def configure_optional_module(module_id: str, values_json: str = "{}", secret_references_json: str = "{}") -> str:
    """Configure an installed requested module. Provide only allowed nonsecret environment values and supported vault references; never put passwords or tokens in these arguments. Enable afterward for live verification."""
    values, references = json.loads(values_json), json.loads(secret_references_json)
    if not isinstance(values, dict) or not isinstance(references, dict):
        raise ValueError("Configuration must contain JSON objects")
    manager = ModuleManager()
    with _locked(manager):
        return json.dumps(manager.configure(module_id, values, references))


@mcp.tool()
def run_optional_module(module_id: str, arguments_json: str) -> str:
    """Use the installed upstream CLI for a requested workflow with its configured environment and vault references. Supply a JSON string array of native arguments, never shell code or authentication values. Empty successful output needs further verification; nonzero exit is failure."""
    arguments = json.loads(arguments_json)
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise ValueError("Arguments must be a JSON string array")
    manager = ModuleManager()
    with _locked(manager):
        state = manager.state(module_id)
        if not state.get("release") or state["manifest"].get("interface") != "cli":
            raise ValueError("Install a CLI module before running it")
        from .module_runtime import run_cli
        return json.dumps(run_cli(Path(state["release"]), state["manifest"], manager.path(module_id) / "data", arguments))


if __name__ == "__main__":
    mcp.run()
