"""Use upstream Node/native programs and Herald's existing MCP implementation."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def local_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Module executable must stay in its release directory")
    return path


def prepare(release: Path, manifest: dict) -> None:
    if manifest.get("runtime") != "node":
        return
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("Node.js and npm are needed; use native tools to provision the supported runtime")
    result = subprocess.run([node, "--version"], capture_output=True, text=True, check=True, timeout=15, creationflags=FLAGS)
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+\s*", result.stdout)
    if not match or int(match[1]) < manifest.get("minimum_node_major", 22):
        raise RuntimeError("Installed Node.js is older than this module requires")
    source = release / "source"
    if not (source / "package-lock.json").is_file():
        raise ValueError("Node modules require a package-lock.json")
    subprocess.run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=source,
                   capture_output=True, check=True, timeout=900, creationflags=FLAGS)


def command(release: Path, manifest: dict) -> list[str]:
    runtime = manifest.get("runtime", "python")
    if runtime == "node":
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js is unavailable")
        executable = local_path(release / "source", manifest["entrypoint"])
        if not executable.is_file():
            raise RuntimeError("Module entrypoint is missing")
        return [node, str(executable), *manifest.get("arguments", [])]
    if runtime == "native":
        executable = local_path(release / "source", manifest["entrypoint"])
        if not executable.is_file():
            raise RuntimeError("Module executable is missing")
        return [str(executable), *manifest.get("arguments", [])]
    raise ValueError("Unsupported upstream runtime")


def configuration(data: Path, manifest: dict | None = None) -> dict:
    file = data / "configuration.json"
    values = json.loads(file.read_text("utf-8")) if file.exists() else {}
    manifest = manifest or {}
    # Fixed, nonsecret values the module always runs with (module.json "environment"), then
    # data-folder paths, then what the user configured.
    fixed = {str(k): str(v) for k, v in manifest.get("environment", {}).items()}
    defaults = {key: str(local_path(data, relative)) for key, relative in manifest.get("data_environment", {}).items()}
    return {"env": {**fixed, **defaults, **values.get("env", {})}, "env_refs": values.get("env_refs", {})}


def readiness(release: Path, manifest: dict, data: Path) -> dict:
    config = configuration(data, manifest)
    available = set(config["env"]) | set(config["env_refs"])
    missing = [key for key in manifest.get("required_environment", []) if key not in available]
    alternatives = manifest.get("authentication_alternatives", [])
    if alternatives and not any(key in available for key in alternatives):
        missing.append("one of: " + ", ".join(alternatives))
    if missing:
        return {"status": "needs_setup", "missing": missing}
    launch = command(release, manifest)
    if manifest.get("interface", "mcp") == "mcp":
        from herald.router.tool_executor import discover_tools, execute_tool
        spec = {"command": launch, "cwd": str(data), **config, "timeout": 45}
        discovery = discover_tools("stdio", spec)
        expected = manifest.get("required_tools", [])
        tools = {tool["name"] for tool in discovery.get("tools", [])}
        if not discovery.get("ok") or not tools or not set(expected).issubset(tools):
            raise RuntimeError("Upstream MCP discovery did not confirm required tools")
        check = manifest.get("verification_tool")
        if check:
            result = execute_tool(check["name"], "stdio", spec, check.get("arguments", {}))
            if not result.get("ok"):
                raise RuntimeError("Upstream MCP live read failed; complete account setup before enabling")
        return {"status": "ready"}
    from herald.router.account_registry import resolve_secret_ref
    env = {**os.environ, **config["env"]}
    for key, reference in config["env_refs"].items():
        env[key] = resolve_secret_ref(reference)
    result = subprocess.run([*launch, *manifest.get("verification_arguments", ["--help"])],
                            cwd=data, env=env, capture_output=True, text=True, timeout=60, creationflags=FLAGS)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Upstream CLI verification failed or returned no output")
    if manifest.get("verification_json"):
        payload = json.loads(result.stdout)
        if isinstance(payload, dict) and (payload.get("error") or payload.get("ok") is False):
            raise RuntimeError("Upstream CLI reported unsuccessful verification")
    return {"status": "ready"}


def validate_configuration(manifest: dict, values: dict, references: dict) -> None:
    allowed = set(manifest.get("allowed_environment", []))
    secret_names = set(manifest.get("secret_environment", []))
    if set(values) - allowed or set(references) - secret_names or set(values) & secret_names:
        raise ValueError("Unsupported configuration field; secret values must use vault references")
    if any(not isinstance(value, str) or not value for value in [*values.values(), *references.values()]):
        raise ValueError("Configuration values and references must be nonempty strings")
    from herald.router.account_registry import validate_secret_ref
    for reference in references.values():
        validate_secret_ref(reference)


def save_configuration(data: Path, manifest: dict, values: dict, references: dict) -> None:
    validate_configuration(manifest, values, references)
    existing = configuration(data)
    existing["env"].update(values)
    existing["env_refs"].update(references)
    data.mkdir(parents=True, exist_ok=True)
    pending = data / "configuration.pending.json"
    pending.write_text(json.dumps(existing, indent=2) + "\n", "utf-8")
    pending.replace(data / "configuration.json")


def run_cli(release: Path, manifest: dict, data: Path, arguments: list[str]) -> dict:
    from herald.router.account_registry import resolve_secret_ref
    config = configuration(data, manifest)
    env = {**os.environ, **config["env"]}
    secrets = []
    for key, reference in config["env_refs"].items():
        value = resolve_secret_ref(reference)
        env[key] = value
        secrets.append(value)
    result = subprocess.run([*command(release, manifest), *arguments], cwd=data, env=env,
                            capture_output=True, text=True, timeout=180, creationflags=FLAGS)
    if result.returncode:
        raise RuntimeError(f"Upstream CLI execution failed (exit {result.returncode}); no success confirmed")
    output = result.stdout
    for value in secrets:
        if value:
            output = output.replace(value, "[REDACTED]")
    return {"status": "completed" if output.strip() else "needs_verification", "exit_code": result.returncode,
            "output": output[:30000], "truncated": len(output) > 30000}
