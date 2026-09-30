"""Pinned specifications for Herald's first-party MCP integration catalog."""
from __future__ import annotations

import os
import hashlib
import io
import platform
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import httpx


FILESYSTEM_VERSION = "2026.7.10"
GITHUB_MCP_VERSION = "0.31.0"


def ensure_github_mcp_binary(version: str = GITHUB_MCP_VERSION) -> Path:
    """Install GitHub's checksum-verified official release in Herald's cache."""
    existing = shutil.which("github-mcp-server")
    if existing:
        return Path(existing)
    system = {"Windows": "Windows", "Linux": "Linux", "Darwin": "Darwin"}.get(platform.system())
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    if not system:
        raise RuntimeError(f"GitHub MCP has no supported binary for {platform.system()}")
    suffix = "zip" if system == "Windows" else "tar.gz"
    asset = f"github-mcp-server_{system}_{arch}.{suffix}"
    base = f"https://github.com/github/github-mcp-server/releases/download/v{version}"
    try:
        checksums = httpx.get(
            f"{base}/github-mcp-server_{version}_checksums.txt",
            follow_redirects=True, timeout=60,
        )
        checksums.raise_for_status()
        expected = next(
            line.split()[0] for line in checksums.text.splitlines()
            if line.split() and line.split()[-1].lstrip("*") == asset
        )
        response = httpx.get(f"{base}/{asset}", follow_redirects=True, timeout=180)
        response.raise_for_status()
    except (httpx.HTTPError, StopIteration) as exc:
        raise RuntimeError(f"could not download verified GitHub MCP release {version}: {exc}") from exc
    if hashlib.sha256(response.content).hexdigest().casefold() != expected.casefold():
        raise RuntimeError("GitHub MCP release checksum did not match")
    if suffix == "zip":
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            member = next(name for name in archive.namelist() if Path(name).name == "github-mcp-server.exe")
            payload = archive.read(member)
        executable_name = "github-mcp-server.exe"
    else:
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            member = next(item for item in archive.getmembers() if Path(item.name).name == "github-mcp-server" and item.isfile())
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError("GitHub MCP archive did not contain its executable")
            payload = extracted.read()
        executable_name = "github-mcp-server"
    install_root = Path.home() / ".herald" / "tools" / "github-mcp-server" / version
    install_root.mkdir(parents=True, exist_ok=True)
    executable = install_root / executable_name
    temporary = executable.with_suffix(executable.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, executable)
    if os.name != "nt":
        executable.chmod(0o700)
    return executable


def native_integration_spec(
    kind: str, *, root: str | Path = ".", github_token_env: str = "GITHUB_PERSONAL_ACCESS_TOKEN",
) -> dict[str, Any]:
    root_path = str(Path(root).resolve())
    if kind == "filesystem":
        package = f"@modelcontextprotocol/server-filesystem@{FILESYSTEM_VERSION}"
        if os.name == "nt":
            command = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/s", "/c", "npx", "-y", package, root_path]
        else:
            command = [shutil.which("npx") or "npx", "-y", package, root_path]
        return {
            "name": "filesystem", "transport": "stdio",
            "config": {"command": command, "timeout": 120},
            "description": f"Official MCP filesystem server rooted at {root_path}",
            "tags": ["native", "filesystem", "mcp"],
            "package_name": "@modelcontextprotocol/server-filesystem",
            "version": FILESYSTEM_VERSION,
            "source": {"type": "herald-native", "repository": "https://github.com/modelcontextprotocol/servers"},
        }
    if kind == "shell":
        return {
            "name": "shell", "transport": "stdio",
            "config": {"command": [sys.executable, "-m", "herald.native_mcp", "--root", root_path], "timeout": 600},
            "description": f"Herald Bash/PowerShell command server rooted at {root_path}",
            "tags": ["native", "shell", "bash", "powershell", "mcp"],
            "package_name": "herald-native-shell", "version": "0.2.0",
            "source": {"type": "herald-native"},
        }
    if kind == "github":
        executable = ensure_github_mcp_binary()
        return {
            "name": "github", "transport": "stdio",
            "config": {
                "command": [str(executable), "stdio"],
                "env_refs": {github_token_env: f"env:{github_token_env}"},
                "timeout": 120,
            },
            "description": "GitHub's official MCP server",
            "tags": ["native", "github", "mcp"],
            "package_name": "github-mcp-server", "version": GITHUB_MCP_VERSION,
            "source": {"type": "herald-native", "repository": "https://github.com/github/github-mcp-server"},
        }
    raise ValueError("native integration must be filesystem, shell, or github")
