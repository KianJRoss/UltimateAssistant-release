"""Install public HTTPS releases without a GitHub account or Git."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def check_update(data: Path, install: bool = False) -> dict:
    config_file = data / "updates.json"
    config = json.loads(config_file.read_text("utf-8")) if config_file.exists() else {}
    source = config.get("manifest_url", "https://raw.githubusercontent.com/KianJRoss/UltimateAssistant-release/main/release.json")
    if urlsplit(source).scheme != "https":
        raise ValueError("Use an HTTPS release manifest URL.")
    with tempfile.TemporaryDirectory(prefix="ultimate-assistant-update-") as temp:
        stage = Path(temp)
        response = httpx.get(source, follow_redirects=True, timeout=30)
        response.raise_for_status()
        manifest = response.json()
        version = manifest["version"]
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("Invalid release version")
        current_file = data / "current-release.json"
        current = json.loads(current_file.read_text("utf-8")) if current_file.exists() else {}
        version_file = Path(__file__).resolve().parents[1] / "VERSION"
        installed_version = current.get("version") or (version_file.read_text().strip() if version_file.exists() else "0.0.0")
        if tuple(map(int, version.split('.'))) <= tuple(map(int, installed_version.split('.'))):
            return {"status": "completed", "detail": "Already on the latest release.", "version": installed_version}
        if not install:
            return {"status": "available", "version": version}
        url = manifest["url"]
        if urlsplit(url).scheme != "https" or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
            raise ValueError("Release requires HTTPS download and SHA-256 checksum")
        archive = stage / "release.zip"
        digest = hashlib.sha256()
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with archive.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != manifest["sha256"]:
            raise ValueError("Update checksum mismatch; current installation retained")
        extracted = stage / "extracted"
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if not (extracted / name).resolve().is_relative_to(extracted.resolve()):
                    raise ValueError("Unsafe archive path")
            bundle.extractall(extracted)
        installers = list(extracted.rglob("Install-UltimateAssistant.ps1"))
        if len(installers) != 1:
            raise ValueError("Invalid installer bundle")
        target = data / "releases" / (version + "-" + manifest["sha256"][:12])
        if not (target / ".install-complete").exists():
            subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installers[0]),
                            "-SkipConfiguration", "-SkipLaunch"], check=True, capture_output=True, timeout=1800,
                           env={**os.environ, "ULTIMATE_ASSISTANT_INSTALL_DIR": str(target), "ULTIMATE_ASSISTANT_NO_SHORTCUT": "1"})
        python = target / ".venvs" / "assistant" / "Scripts" / "python.exe"
        subprocess.run([str(python), "-c", "import herald.router.server; import ultimate_assistant.web_ui"],
                       cwd=target / "apps" / "assistant", check=True, capture_output=True, timeout=120,
                       env={**os.environ, "HERALD_DATA_DIR": str(stage / "smoke-router"),
                            "LOCALAPPDATA": str(stage / "smoke-user"), "HERALD_SKIP_BACKEND_DISCOVERY": "1"})
        (target / ".install-complete").write_text(version, "utf-8")
        state = {"version": version, "path": str(target), "previous": current or None}
        data.mkdir(parents=True, exist_ok=True)
        pending = data / "current-release.pending.json"
        pending.write_text(json.dumps(state), "utf-8")
        pending.replace(current_file)
        return {"status": "completed", "version": version, "detail": "Update verified; starts on next launch."}


def rollback(data: Path) -> dict:
    path = data / "current-release.json"
    current = json.loads(path.read_text("utf-8"))
    previous = current.get("previous")
    if previous:
        pending = data / "current-release.pending.json"
        pending.write_text(json.dumps(previous), "utf-8")
        pending.replace(path)
    else:
        path.unlink()
    config_file = data / "updates.json"
    if config_file.exists():
        config = json.loads(config_file.read_text("utf-8"))
        config["automatic"] = False
        config_file.write_text(json.dumps(config), "utf-8")
    return {"status": "completed", "detail": "Previous installation selected for next launch."}
