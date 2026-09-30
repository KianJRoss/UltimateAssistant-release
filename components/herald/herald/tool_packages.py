"""Isolated installers for adapting open-source MCP servers to Herald."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path


def parse_source(source: str) -> tuple[str, str]:
    if ":" not in source:
        raise ValueError("source must use KIND:VALUE (npm:, pip:, git:, or local:)")
    kind, value = source.split(":", 1)
    if kind not in {"npm", "pip", "git", "local"} or not value:
        raise ValueError("source must use npm:, pip:, git:, or local:")
    return kind, value


def _safe_component(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip(".-")
    if not safe:
        raise ValueError(f"invalid installation path component: {value!r}")
    return safe


@dataclass(frozen=True)
class InstalledPackage:
    name: str
    package: str
    version: str
    root: Path
    command: list[str]
    source: dict[str, str]


class ToolPackageInstaller:
    def __init__(self, tools_root: str | Path) -> None:
        self.tools_root = Path(tools_root).resolve()
        self.tools_root.mkdir(parents=True, exist_ok=True)

    def _target(self, name: str, version: str) -> Path:
        target = (self.tools_root / _safe_component(name) / _safe_component(version)).resolve()
        if self.tools_root not in target.parents:
            raise ValueError("tool installation escaped its tools root")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def install(
        self, source: str, *, name: str, version: str, binary: str | None = None,
        args: list[str] | None = None,
    ) -> InstalledPackage:
        kind, value = parse_source(source)
        target = self._target(name, version)
        arguments = args or []
        if kind == "npm":
            npm = shutil.which("npm")
            if not npm:
                raise RuntimeError("npm is not installed")
            spec = f"{value}@{version}" if version != "unversioned" else value
            subprocess.run([npm, "install", "--prefix", str(target), "--no-save", spec], check=True)
            executable_name = binary or value.rsplit("/", 1)[-1]
            executable = target / "node_modules" / ".bin" / executable_name
            if os.name == "nt":
                executable = executable.with_suffix(".cmd")
        elif kind == "pip":
            environment = target / ".venv"
            if not environment.exists():
                venv.EnvBuilder(with_pip=True).create(environment)
            scripts = environment / ("Scripts" if os.name == "nt" else "bin")
            pip = scripts / ("pip.exe" if os.name == "nt" else "pip")
            spec = f"{value}=={version}" if version != "unversioned" else value
            subprocess.run([str(pip), "install", spec], check=True)
            executable_name = binary or value.replace("_", "-")
            executable = scripts / (f"{executable_name}.exe" if os.name == "nt" else executable_name)
        elif kind == "git":
            checkout = target / "source"
            if not checkout.exists():
                git = shutil.which("git")
                if not git:
                    raise RuntimeError("git is not installed")
                command = [git, "clone", "--depth", "1"]
                if version != "unversioned":
                    command.extend(["--branch", version])
                subprocess.run([*command, value, str(checkout)], check=True)
            if not binary:
                raise ValueError("git sources require --binary relative to the checkout")
            executable = (checkout / binary).resolve()
            if checkout.resolve() not in executable.parents:
                raise ValueError("git binary escaped its checkout")
        else:
            executable = Path(value).expanduser().resolve()
            if not executable.exists():
                raise FileNotFoundError(executable)

        command = [str(executable), *arguments]
        return InstalledPackage(
            name=name, package=value, version=version, root=target,
            command=command,
            source={"type": kind, "package": value, "version": version},
        )
