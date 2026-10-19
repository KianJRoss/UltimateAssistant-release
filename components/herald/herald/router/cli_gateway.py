"""Attach Herald's scoped MCP gateway to every CLI the router launches.

Window-mode CLI calls (codex/claude/antigravity running their own native
session) used to see only whatever MCP servers that machine's CLI config
happened to list. Tools Herald resolves for the caller -- global groups, the
project/part an assistant runs in, groups bound to the CLI profile, and tools
on paired devices -- never reached them. This module builds the per-call
arguments that hand each CLI one `herald` MCP server: `herald.mcp_gateway`
pointed back at this router with the caller's scope.

codex and claude take the server per invocation (`-c mcp_servers.*` /
`--mcp-config`). antigravity only has persisted `agy mcp add`, so for it the
scope travels in HERALD_GATEWAY_* environment variables that a scope-less
`herald.mcp_gateway` entry reads (see mcp_gateway.main).

Set HERALD_CLI_GATEWAY=0 (or a backend config `"herald_gateway": false`) to
opt out.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

SERVER_NAME = "herald"
# Variables the gateway subprocess needs. Codex starts MCP servers with a
# filtered environment, so these must be passed through explicitly.
PASSTHROUGH_ENV = (
    "HERALD_API_KEY", "HERALD_URL", "HERALD_GATEWAY_PROJECT", "HERALD_GATEWAY_PART",
    "HERALD_GATEWAY_PROFILE", "HERALD_ASSISTANT_PARENT_ID", "HERALD_ASSISTANT_PROJECT",
    "HERALD_ASSISTANT_PART", "HERALD_ASSISTANT_ACTIVITY_ID", "HERALD_CONSULT_DEPTH",
)


def self_url() -> str:
    """URL CLIs on this machine use to reach this router."""
    explicit = os.environ.get("HERALD_SELF_URL") or os.environ.get("HERALD_URL")
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('HERALD_PORT', '8790')}"


def enabled(config: dict[str, Any]) -> bool:
    if os.environ.get("HERALD_CLI_GATEWAY", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return config.get("herald_gateway", True) is not False


def gateway_command(*, project: str | None, part: str | None, profile: str | None) -> list[str]:
    command = [sys.executable, "-m", "herald.mcp_gateway", "--url", self_url()]
    if project and part:
        command += ["--project", project, "--part", part]
    if profile:
        command += ["--profile", profile]
    return command


def scope_env(*, project: str | None, part: str | None, profile: str | None) -> dict[str, str]:
    env = {"HERALD_URL": self_url()}
    if project and part:
        env.update({"HERALD_GATEWAY_PROJECT": project, "HERALD_GATEWAY_PART": part})
    if profile:
        env["HERALD_GATEWAY_PROFILE"] = profile
    return env


def cli_args(cli_kind: str | None, *, project: str | None, part: str | None,
             profile: str | None) -> list[str]:
    """Extra argv that attaches the gateway for one CLI invocation."""
    command = gateway_command(project=project, part=part, profile=profile)
    if cli_kind == "codex":
        # Values are TOML; JSON string/array literals are valid TOML for these.
        prefix = f"mcp_servers.{SERVER_NAME}"
        return [
            "-c", f"{prefix}.command={json.dumps(command[0])}",
            "-c", f"{prefix}.args={json.dumps(command[1:])}",
            "-c", f"{prefix}.env_vars={json.dumps(list(PASSTHROUGH_ENV))}",
            "-c", f"{prefix}.startup_timeout_sec=60",
        ]
    if cli_kind == "claude":
        server = {"type": "stdio", "command": command[0], "args": command[1:]}
        return ["--mcp-config", json.dumps({"mcpServers": {SERVER_NAME: server}})]
    return []
