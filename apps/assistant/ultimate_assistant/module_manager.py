"""On-demand, isolated Python MCP modules from an owner-selected catalog."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from .node_link import link
from .settings import settings


class ModuleManager:
    def __init__(self):
        self.root = settings.user_data_dir / "modules"
        self.catalog_path = settings.user_settings_file.parent / "module-catalog.json"

    def catalog(self) -> list[dict]:
        # Installing the catalog never installs the listed software.
        source = self.catalog_path if self.catalog_path.exists() else Path(__file__).with_name("module_catalog.json")
        if not source.exists():
            return []
        data = json.loads(source.read_text("utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("modules"), list):
            raise ValueError("Unsupported module catalog")
        return data["modules"]

    def manifest(self, name: str) -> dict:
        self.path(name)
        matches = [m for m in self.catalog() if m.get("id") == name]
        if len(matches) != 1:
            raise ValueError("Module must have exactly one entry in the owner-selected catalog")
        m = matches[0]
        if not re.fullmatch(r"\d+\.\d+\.\d+", str(m.get("version", ""))):
            raise ValueError("Module needs a three-part numeric version")
        if (not m.get("bundled_archive") and urlsplit(m.get("url", "")).scheme != "https") or not re.fullmatch(r"[a-f0-9]{64}", m.get("sha256", "")):
            raise ValueError("Module needs an HTTPS archive and SHA-256 checksum")
        runtime = m.get("runtime", "python")
        if runtime == "python":
            for key in ("entrypoint", "healthcheck"):
                if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", m.get(key, "")):
                    raise ValueError(f"Invalid Python {key}")
        elif runtime in {"node", "native"}:
            from .module_runtime import local_path
            local_path(Path("."), m["entrypoint"])
            if not all(isinstance(arg, str) for arg in m.get("arguments", [])):
                raise ValueError("Module arguments must be strings")
        else:
            raise ValueError("Unsupported module runtime")
        return m

    def path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise ValueError("Invalid module ID")
        return self.root / name

    def state(self, name: str) -> dict:
        file = self.path(name) / "state.json"
        return json.loads(file.read_text("utf-8")) if file.exists() else {"status": "not_installed"}

    def save(self, name: str, state: dict) -> None:
        folder = self.path(name)
        folder.mkdir(parents=True, exist_ok=True)
        pending = folder / "state.pending.json"
        pending.write_text(json.dumps(state, indent=2) + "\n", "utf-8")
        pending.replace(folder / "state.json")

    @staticmethod
    def python(release: Path) -> Path:
        return release / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def environment(self, name: str, release: Path) -> dict:
        data = self.path(name) / "data"
        data.mkdir(parents=True, exist_ok=True)
        return {**os.environ, "ULTIMATE_ASSISTANT_MODULE_DATA": str(data),
                "PYTHONPATH": str(release / "source")}

    def verify(self, name: str, release: Path, manifest: dict) -> dict:
        if manifest.get("runtime", "python") != "python":
            from .module_runtime import readiness
            data = self.path(name) / "data"
            data.mkdir(parents=True, exist_ok=True)
            return readiness(release, manifest, data)
        result = subprocess.run([str(self.python(release)), "-m", manifest["healthcheck"]],
                                cwd=release / "source", env=self.environment(name, release),
                                capture_output=True, text=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError(f"Module health check failed (exit {result.returncode}); inspect module configuration locally")
        try:
            report = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError("Module health check returned no valid JSON report") from exc
        if report.get("status") not in {"ready", "needs_setup"}:
            raise RuntimeError("Module health check did not confirm readiness or required setup")
        return report

    def install(self, name: str) -> dict:
        m = self.manifest(name)
        previous = self.state(name)
        if previous.get("manifest", {}).get("sha256") == m["sha256"]:
            return self.enable(name)
        release = self.path(name) / "releases" / (m["version"] + "-" + m["sha256"][:12])
        if not release.exists():
            release.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=release.parent) as temp:
                stage = Path(temp)
                archive = stage / "module.zip"
                digest = hashlib.sha256()
                if m.get("bundled_archive"):
                    from .module_runtime import local_path
                    source_archive = local_path(Path(__file__).parent / "module_packages", m["bundled_archive"])
                    shutil.copyfile(source_archive, archive)
                    digest.update(archive.read_bytes())
                else:
                    self.download(m["url"], archive, digest)
                if digest.hexdigest() != m["sha256"]:
                    raise ValueError("Module checksum mismatch")
                source = stage / "source"
                self.extract(archive, source)
                if m.get("runtime", "python") == "python":
                    subprocess.run([sys.executable, "-m", "venv", str(stage / "venv")], check=True,
                                   capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    requirements = source / "requirements.txt"
                    if requirements.exists():
                        subprocess.run([str(self.python(stage)), "-m", "pip", "install", "--require-hashes", "-r", str(requirements)],
                                       check=True, capture_output=True, timeout=600,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    from .module_runtime import prepare
                    prepare(stage, m)
                shutil.move(str(stage), str(release))
            if m.get("runtime", "python") == "python":
                try:
                    subprocess.run([sys.executable, "-m", "venv", str(release / "venv")], check=True,
                                   capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    shutil.rmtree(release)
                    raise
        report = self.verify(name, release, m)
        if previous.get("enabled"):
            self.bind(name, previous, False)
        state = {"status": report["status"], "enabled": False, "release": str(release),
                 "manifest": m, "previous": previous if previous.get("release") else None}
        self.save(name, state)
        if report["status"] == "ready":
            return self.enable(name)
        return {"status": "needs_setup", "module": name, "missing": report.get("missing", []), "setup": m.get("setup", [])}

    @staticmethod
    def download(url: str, archive: Path, digest) -> None:
        size = 0
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            if response.url.scheme != "https":
                raise ValueError("Module download redirected away from HTTPS")
            with archive.open("wb") as output:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 256 * 1024 * 1024:
                        raise ValueError("Module archive exceeds download limit")
                    digest.update(chunk)
                    output.write(chunk)
    @staticmethod
    def extract(archive: Path, source: Path) -> None:
        with zipfile.ZipFile(archive) as bundle:
            if sum(i.file_size for i in bundle.infolist()) > 512 * 1024 * 1024:
                raise ValueError("Module exceeds extraction limit")
            for item in bundle.infolist():
                parts = item.filename.replace("\\", "/").split("/")
                if any(p in {".."} or ":" in p for p in parts) or not (source / item.filename).resolve().is_relative_to(source.resolve()):
                    raise ValueError("Unsafe module archive path")
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Module archive cannot contain symlinks")
            bundle.extractall(source)

    def bind(self, name: str, state: dict, enabled: bool) -> None:
        if state["manifest"].get("interface", "mcp") == "cli":
            return
        base = settings.herald_url.rstrip("/")
        if httpx.URL(base).host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Optional local modules require the local Router")
        headers = {"Authorization": "Bearer " + os.environ["HERALD_API_KEY"]} if os.environ.get("HERALD_API_KEY") else {}
        tool = "assistant-module-" + name
        binding = f"{base}/projects/{quote(settings.herald_project, safe='')}/parts/{quote(settings.herald_part, safe='')}/tools"
        if not enabled:
            response = httpx.delete(binding + "/" + tool, headers=headers, timeout=30)
            if response.status_code != 404:
                response.raise_for_status()
            from .agents import load_agents
            for agent in load_agents().values():
                httpx.delete(f"{base}/projects/{quote(settings.herald_project, safe='')}/parts/"
                             f"{quote(agent.part, safe='')}/tools/{tool}", headers=headers, timeout=30)
            response = httpx.delete(base + "/tool-instances/" + tool, headers=headers, timeout=30)
            if response.status_code != 404:
                response.raise_for_status()
            return
        release = Path(state["release"])
        manifest = state["manifest"]
        if manifest.get("runtime", "python") == "python":
            config = {"command": [str(self.python(release)), "-m", manifest["entrypoint"]],
                      "env": {"ULTIMATE_ASSISTANT_MODULE_DATA": str(self.path(name) / "data"),
                              "PYTHONPATH": str(release / "source"),
                              # The assistant itself, for modules that launch it (Obsidian's ACP pane).
                              "ULTIMATE_ASSISTANT_PYTHON": sys.executable,
                              "ULTIMATE_ASSISTANT_APP": str(Path(__file__).resolve().parents[1])}, "timeout": 60}
        else:
            from .module_runtime import command, configuration
            config = {"command": command(release, manifest), "cwd": str(self.path(name) / "data"),
                      **configuration(self.path(name) / "data", manifest), "timeout": 60}
        response = httpx.post(base + "/tool-instances", headers=headers, timeout=30, json=link.localize({
            "name": tool, "transport": "stdio", "scope": "project", "project": settings.herald_project,
            "description": state["manifest"].get("description", name), "tags": ["assistant", "optional-module"],
            "config": config}))
        response.raise_for_status()
        response = httpx.post(binding, headers=headers, timeout=30, json={"tool": tool, "alias": "module_" + name.replace("-", "_")})
        response.raise_for_status()
        # every agent can use an installed module (a calendar module serves the planner and the companion)
        from .agents import load_agents
        for agent in load_agents().values():
            agent_binding = f"{base}/projects/{quote(settings.herald_project, safe='')}/parts/{quote(agent.part, safe='')}/tools"
            httpx.post(agent_binding, headers=headers, timeout=30,
                       json={"tool": tool, "alias": "module_" + name.replace("-", "_")})

    def enable(self, name: str) -> dict:
        state = self.state(name)
        if not state.get("release"):
            raise ValueError("Module is not installed")
        report = self.verify(name, Path(state["release"]), state["manifest"])
        if report["status"] != "ready":
            if state.get("enabled"):
                self.disable(name)
            state.update(status="needs_setup", enabled=False)
            self.save(name, state)
            return {"status": "needs_setup", "module": name, "setup": state["manifest"].get("setup", [])}
        self.bind(name, state, True)
        state.update(status="ready", enabled=True)
        self.save(name, state)
        return {"status": "ready", "module": name, "enabled": True, "fresh_turn_required": True}

    def configure(self, name: str, values: dict, references: dict) -> dict:
        state = self.state(name)
        if not state.get("release"):
            raise ValueError("Install the module before configuring it")
        from .module_runtime import save_configuration, validate_configuration
        validate_configuration(state["manifest"], values, references)
        if state.get("enabled"):
            self.disable(name)
        save_configuration(self.path(name) / "data", state["manifest"], values, references)
        state.update(status="needs_setup", enabled=False)
        self.save(name, state)
        return {"status": "needs_setup", "module": name, "next_action": "enable to verify configuration"}

    def disable(self, name: str) -> dict:
        state = self.state(name)
        if state.get("release"):
            self.bind(name, state, False)
            state["enabled"] = False
            self.save(name, state)
        return {"status": "disabled", "module": name}

    def remove(self, name: str) -> dict:
        self.disable(name)
        folder = self.path(name)
        # Keep user records and account configuration for possible reinstallation.
        releases = folder / "releases"
        if releases.exists():
            shutil.rmtree(releases)
        self.save(name, {"status": "not_installed", "enabled": False})
        return {"status": "removed", "module": name, "data_preserved": True}

    def rollback(self, name: str) -> dict:
        current = self.state(name)
        previous = current.get("previous")
        if not previous or not Path(previous["release"]).is_dir():
            raise ValueError("No previous module version is available")
        report = self.verify(name, Path(previous["release"]), previous["manifest"])
        if report["status"] != "ready":
            return {"status": "needs_setup", "module": name, "setup": previous["manifest"].get("setup", [])}
        self.disable(name)
        previous["enabled"] = False
        self.save(name, previous)
        return self.enable(name)

    def restore_enabled(self) -> None:
        if self.root.exists():
            for folder in self.root.iterdir():
                if folder.is_dir() and self.state(folder.name).get("enabled"):
                    report = self.enable(folder.name)
                    if report["status"] != "ready":
                        self.disable(folder.name)
