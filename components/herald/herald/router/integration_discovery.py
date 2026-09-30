"""Read-only inventory of MCP/plugin connections exposed by other AI CLIs."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from herald.router.secret_vault import SecretVault

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10
    tomllib = None


def _candidate_files(home: Path) -> list[tuple[str, Path, str]]:
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        ("claude-code", home / ".claude.json", "json"),
        ("claude-desktop", appdata / "Claude" / "claude_desktop_config.json", "json"),
        ("codex", home / ".codex" / "config.toml", "toml"),
        ("gemini", home / ".gemini" / "settings.json", "json"),
        ("qwen", home / ".qwen" / "settings.json", "json"),
        ("antigravity", home / ".config" / "agy" / "mcp_config.json", "json"),
        ("antigravity", home / ".antigravity" / "mcp_config.json", "json"),
    ]


def _mcp_maps(data: dict[str, Any]) -> list[dict[str, Any]]:
    maps = []
    for key in ("mcpServers", "mcp_servers", "mcp"):
        value = data.get(key)
        if isinstance(value, dict):
            maps.append(value)
    projects = data.get("projects")
    if isinstance(projects, dict):
        for project in projects.values():
            if isinstance(project, dict):
                maps.extend(_mcp_maps(project))
    return maps


def discover_cli_integrations(home: str | Path | None = None) -> list[dict[str, Any]]:
    """Return connection metadata only; never return commands, env, or secret values."""
    root = Path(home).expanduser() if home else Path.home()
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for owner, path, encoding in _candidate_files(root):
        if not path.is_file():
            continue
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle) if encoding == "toml" and tomllib else json.load(handle)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for servers in _mcp_maps(data):
            for name, config in servers.items():
                if not isinstance(config, dict):
                    continue
                identity = (owner, str(path), str(name))
                if identity in seen:
                    continue
                seen.add(identity)
                transport = "http" if config.get("url") else "stdio"
                found.append({
                    "owner": owner, "kind": "mcp", "name": str(name),
                    "transport": transport, "source": str(path),
                    "enabled": config.get("enabled", not config.get("disabled", False)),
                    "imported": False,
                })
    return found


def _read_config(path: Path, encoding: str) -> dict[str, Any]:
    with open(path, "rb") as handle:
        data = tomllib.load(handle) if encoding == "toml" and tomllib else json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("integration config is not an object")
    return data


def find_cli_integration(
    owner: str, name: str, *, source: str | None = None, home: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    root = Path(home).expanduser() if home else Path.home()
    for candidate_owner, path, encoding in _candidate_files(root):
        if candidate_owner != owner or not path.is_file():
            continue
        if source and str(path) != source:
            continue
        try:
            data = _read_config(path, encoding)
        except (OSError, ValueError, TypeError):
            continue
        for servers in _mcp_maps(data):
            config = servers.get(name)
            if isinstance(config, dict):
                return config, path
    raise ValueError(f"integration '{owner}/{name}' was not found in known CLI config locations")


def preview_cli_integration(
    owner: str, name: str, *, source: str | None = None, home: str | Path | None = None,
) -> dict[str, Any]:
    config, path = find_cli_integration(owner, name, source=source, home=home)
    transport = "http" if config.get("url") else "stdio"
    env = config.get("env") if isinstance(config.get("env"), dict) else {}
    headers = config.get("headers") if isinstance(config.get("headers"), dict) else {}
    command = config.get("command")
    args = config.get("args") if isinstance(config.get("args"), list) else []
    safe_args, secret_args = _redact_args([str(item) for item in args])
    safe_command = command
    command_secret_args: list[str] = []
    if isinstance(command, list):
        safe_command, command_secret_args = _redact_args([str(item) for item in command])
    return {
        "owner": owner, "name": name, "source": str(path), "transport": transport,
        "command": safe_command if isinstance(command, (str, list)) else None,
        "args": safe_args,
        "url": _safe_url(str(config.get("url"))) if config.get("url") else None,
        "cwd": config.get("cwd"),
        "secret_inputs": {
            "env": sorted(env), "headers": sorted(headers),
            "args": [*command_secret_args, *secret_args],
        },
    }


def import_cli_integration(
    owner: str, name: str, *, vault: SecretVault, source: str | None = None,
    home: str | Path | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    config, path = find_cli_integration(owner, name, source=source, home=home)
    preview = preview_cli_integration(owner, name, source=str(path), home=home)
    imported: dict[str, Any] = {}
    if preview["transport"] == "stdio":
        command = config.get("command")
        args = config.get("args") if isinstance(config.get("args"), list) else []
        _, secret_args = _redact_args([str(item) for item in args])
        if secret_args:
            raise ValueError("integration has credentials embedded in command arguments; move them to env before import")
        if isinstance(command, list):
            combined = [str(item) for item in command] + [str(item) for item in args]
            _, combined_secrets = _redact_args(combined)
            if combined_secrets:
                raise ValueError("integration has credentials embedded in command arguments; move them to env before import")
            imported["command"] = combined
        elif isinstance(command, str) and command:
            imported["command"] = [command, *[str(item) for item in args]]
        else:
            raise ValueError("stdio integration has no usable command")
        if config.get("cwd"):
            imported["cwd"] = str(config["cwd"])
        imported["env_refs"] = _vault_mapping(
            vault, f"integration/{owner}/{name}/env", config.get("env") or {},
        )
    else:
        imported["url"] = _safe_url(str(config["url"]), reject_sensitive=True)
        imported["header_refs"] = _vault_mapping(
            vault, f"integration/{owner}/{name}/header", config.get("headers") or {},
        )
    imported["timeout"] = min(max(float(config.get("timeout", 30)), 1), 120)
    metadata = {
        "type": "cli-import", "owner": owner, "source": str(path),
        "imported_at": datetime.now(UTC).isoformat(),
    }
    return preview["transport"], imported, metadata


def _vault_mapping(vault: SecretVault, prefix: str, values: Any) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    refs: dict[str, str] = {}
    for key, value in values.items():
        text = str(value)
        if text.startswith("${") and text.endswith("}"):
            refs[str(key)] = f"env:{text[2:-1]}"
        else:
            refs[str(key)] = vault.put(
                f"{prefix}/{key}", text.encode(), metadata={"kind": "integration-secret"},
            )
    return refs


def _safe_url(value: str, *, reject_sensitive: bool = False) -> str:
    parsed = urlsplit(value)
    sensitive_query = [
        key for key, _ in parse_qsl(parsed.query)
        if any(marker in key.lower() for marker in ("key", "token", "secret", "password"))
    ]
    if reject_sensitive and (parsed.username or parsed.password or sensitive_query):
        raise ValueError("integration URL contains embedded credentials; move them to headers before import")
    if parsed.username or parsed.password or sensitive_query:
        return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}?[redacted]"
    return value


def _redact_args(args: list[str]) -> tuple[list[str], list[str]]:
    safe, secret_labels = list(args), []
    previous_sensitive = False
    for index, item in enumerate(args):
        lower = item.lower()
        if previous_sensitive:
            safe[index] = "[redacted]"
            secret_labels.append(f"arg:{index}")
            previous_sensitive = False
            continue
        if item.startswith("-") and any(marker in lower for marker in ("key", "token", "secret", "password")):
            if "=" in item:
                safe[index] = item.split("=", 1)[0] + "=[redacted]"
                secret_labels.append(f"arg:{index}")
            else:
                previous_sensitive = True
    return safe, secret_labels
