"""Secure, deterministic ttyd/tmux deployment support.

This module deliberately does not download programs, create users, configure a
proxy, or handle credentials.  It renders public configuration only; secrets
belong in the proxy or a systemd credential store.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:  # render is intentionally supported on non-Linux development hosts
    import pwd
except ImportError:  # pragma: no cover - exercised by Windows users
    pwd = None  # type: ignore[assignment]

UNSAFE_FLAGS = {
    "--dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
}
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ACCOUNT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
LOOPBACKS = {"lo", "localhost", "127.0.0.1", "::1"}
ARTIFACT_PATHS = {
    "launcher": "usr/lib/herald/terminal/launcher",
    "session_unit": "usr/lib/systemd/system/herald-terminal@.service",
    "gateway_unit": "usr/lib/systemd/system/herald-terminal-gateway.service",
}


class TerminalDeployError(ValueError):
    """A configuration or host cannot satisfy the security contract."""


@dataclass(frozen=True)
class Artifact:
    path: str
    content: str
    mode: int


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as stream:
        return tomllib.load(stream)


def _sessions(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("sessions", config.get("session", []))
    if not isinstance(raw, list) or not raw:
        raise TerminalDeployError("at least one [[sessions]] record is required")
    return raw


def validate_config(config: Mapping[str, Any]) -> None:
    user = config.get("user")
    if not isinstance(user, str) or not ACCOUNT_RE.fullmatch(user) or user == "root":
        raise TerminalDeployError("user must name an existing unprivileged account")
    group = config.get("group", user)
    if not isinstance(group, str) or not ACCOUNT_RE.fullmatch(group):
        raise TerminalDeployError("group must be a safe local account name")
    for key in ("ttyd_path", "tmux_path"):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or not value.startswith("/") or "\n" in value):
            raise TerminalDeployError(f"{key} must be an absolute path")
    gateway = config.get("gateway", {})
    bind = str(gateway.get("bind", "lo"))
    port = gateway.get("port", 7681)
    clients = gateway.get("max_clients", 1)
    writable = gateway.get("writable", True)
    check_origin = gateway.get("check_origin", True)
    one_client = gateway.get("one_client", True)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise TerminalDeployError("gateway.port must be between 1 and 65535")
    if not isinstance(clients, int) or clients < 1:
        raise TerminalDeployError("gateway.max_clients must be positive")
    for key, value in (("writable", writable), ("check_origin", check_origin),
                       ("one_client", one_client)):
        if not isinstance(value, bool):
            raise TerminalDeployError(f"gateway.{key} must be a boolean")
    if one_client and clients != 1:
        raise TerminalDeployError("gateway.one_client=true requires gateway.max_clients=1")
    if bind not in LOOPBACKS and not bind.startswith("/"):
        raise TerminalDeployError(
            "non-loopback gateway exposure is not implemented; bind to loopback or a Unix socket"
        )
    elif gateway.get("exposure_strategy") == "authenticated-proxy":
        header = gateway.get("proxy_auth_header")
        if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header):
            raise TerminalDeployError("authenticated-proxy requires a valid gateway.proxy_auth_header")
    for key in gateway:
        if key.lower() in {"password", "token", "secret", "api_key", "basic_auth"}:
            raise TerminalDeployError("secrets are not permitted in deployment configuration")
    seen: set[str] = set()
    for session in _sessions(config):
        name, argv, cwd = session.get("name"), session.get("argv"), session.get("cwd")
        if not isinstance(name, str) or not SESSION_RE.fullmatch(name):
            raise TerminalDeployError(f"unsafe tmux session name: {name!r}")
        if name in seen:
            raise TerminalDeployError(f"duplicate tmux session name: {name}")
        seen.add(name)
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise TerminalDeployError(f"session {name}: argv must be a non-empty string array")
        if session.get("shell"):
            raise TerminalDeployError(f"session {name}: shell execution is not supported by secure deployment")
        # Target paths are POSIX paths even when rendering on Windows.
        if not isinstance(cwd, str) or not cwd.startswith("/") or any(c in cwd for c in "\r\n"):
            raise TerminalDeployError(f"session {name}: cwd must be absolute")
        bad = [arg for arg in argv if arg.lower() in UNSAFE_FLAGS or "dangerously-" in arg.lower()]
        if bad:
            raise TerminalDeployError(f"session {name}: dangerous permission-bypass argument rejected: {bad[0]}")
        if any(k.lower() in {"password", "token", "secret", "api_key"} for k in session):
            raise TerminalDeployError(f"session {name}: secrets are not permitted in deployment configuration")


def _launcher(config: Mapping[str, Any]) -> str:
    # JSON is embedded as data and passed directly to subprocess; no command is
    # evaluated by a shell, so spaces/metacharacters in paths remain literal.
    sessions = [{"name": s["name"], "argv": s["argv"], "cwd": s["cwd"]} for s in _sessions(config)]
    data = json.dumps(sessions, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    tmux_path = str(config.get("tmux_path", "/usr/bin/tmux"))
    return f'''#!/usr/bin/python3
import json, os, subprocess, sys, time
SESSIONS = {{x["name"]: x for x in json.loads({data!r})}}
SOCKET = os.environ.get("HERALD_TMUX_SOCKET", f"/run/user/{{os.getuid()}}/herald-terminal/tmux.sock")
def tmux(*args, check=True):
    return subprocess.run([{tmux_path!r}, "-S", SOCKET, *args], check=check)
def choose():
    names = sorted(SESSIONS)
    if len(names) == 1: return names[0]
    print("Herald persistent sessions:")
    for i, name in enumerate(names, 1): print(f"  {{i}}) {{name}}")
    try: value = input("Choose: ").strip()
    except EOFError: raise SystemExit(1)
    if value.isdigit() and 1 <= int(value) <= len(names): return names[int(value)-1]
    if value in SESSIONS: return value
    raise SystemExit("unknown session")
if len(sys.argv) < 2 or sys.argv[1] not in ("start", "attach"):
    raise SystemExit("usage: launcher start NAME | launcher attach [NAME]")
name = sys.argv[2] if len(sys.argv) > 2 else choose()
if name not in SESSIONS: raise SystemExit("session is not configured")
s = SESSIONS[name]
os.makedirs(os.path.dirname(SOCKET), mode=0o700, exist_ok=True)
if sys.argv[1] == "start":
    if tmux("has-session", "-t", name, check=False).returncode:
        tmux("new-session", "-d", "-s", name, "-c", s["cwd"], "--", *s["argv"])
    # Monitor without becoming the persistence boundary or capturing agent I/O.
    while tmux("has-session", "-t", name, check=False).returncode == 0: time.sleep(2)
    raise SystemExit(0)
tmux("attach-session", "-t", name)
'''


def render_artifacts(config: Mapping[str, Any]) -> dict[str, Artifact]:
    validate_config(config)
    user = config["user"]
    gateway = config.get("gateway", {})
    bind, port, clients = gateway.get("bind", "lo"), gateway.get("port", 7681), gateway.get("max_clients", 1)
    launcher = "/usr/lib/herald/terminal/launcher"
    def unit_quote(value: object) -> str:
        value = str(value)
        return value if re.fullmatch(r"[A-Za-z0-9_./:@%+-]+", value) else json.dumps(value, ensure_ascii=True)
    common = "\n".join([
        f"User={user}", f"Group={config.get('group', user)}", "UMask=0077", "NoNewPrivileges=yes",
        "PrivateDevices=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes", "ProtectControlGroups=yes", "RestrictSUIDSGID=yes",
        "LockPersonality=yes", "CapabilityBoundingSet=",
    ])
    workspaces = sorted({s["cwd"] for s in _sessions(config)})
    session_unit = f'''[Unit]
Description=Herald persistent terminal session %i
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
{common}
Type=simple
Environment=HERALD_TMUX_SOCKET=%t/herald-terminal/tmux.sock
RuntimeDirectory=herald-terminal
RuntimeDirectoryMode=0700
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={' '.join(unit_quote(p) for p in workspaces)} %t/herald-terminal
ExecStart={launcher} start %i
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
'''
    ttyd = str(config.get("ttyd_path", "/usr/bin/ttyd"))
    ttyd_args = [ttyd, "-i", str(bind), "-p", str(port)]
    if gateway.get("writable", True): ttyd_args.append("-W")
    if gateway.get("check_origin", True): ttyd_args.append("-O")
    if gateway.get("one_client", True): ttyd_args.extend(["-m", "1"])
    else: ttyd_args.extend(["-m", str(clients)])
    strategy = gateway.get("exposure_strategy")
    if strategy == "authenticated-proxy":
        ttyd_args.extend(["-H", gateway["proxy_auth_header"]])
    ttyd_args.extend([launcher, "attach"])
    exec_start = " ".join(unit_quote(arg) for arg in ttyd_args)
    gateway_unit = f'''[Unit]
Description=Herald ttyd attachment gateway
After=network.target {' '.join('herald-terminal@' + s['name'] + '.service' for s in _sessions(config))}

[Service]
{common}
Type=simple
Environment=HERALD_TMUX_SOCKET=%t/herald-terminal/tmux.sock
RuntimeDirectory=herald-terminal
RuntimeDirectoryMode=0700
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
'''
    return {
        "launcher": Artifact(ARTIFACT_PATHS["launcher"], _launcher(config), 0o755),
        "session_unit": Artifact(ARTIFACT_PATHS["session_unit"], session_unit, 0o644),
        "gateway_unit": Artifact(ARTIFACT_PATHS["gateway_unit"], gateway_unit, 0o644),
    }


def render(config_path: str | Path, output: str | Path) -> list[Path]:
    artifacts = render_artifacts(load_config(config_path))
    root = Path(output)
    written = []
    for artifact in artifacts.values():
        target = root / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(artifact.content, encoding="utf-8", newline="\n")
        target.chmod(artifact.mode)
        written.append(target)
    return written


def check_prerequisites(config: Mapping[str, Any], *, which=shutil.which,
                        run=subprocess.run) -> dict[str, str]:
    if os.name != "posix" or pwd is None or not Path("/run/systemd/system").exists():
        raise TerminalDeployError("installation requires Linux with systemd running")
    try:
        account = pwd.getpwnam(str(config["user"]))
    except KeyError as exc:
        raise TerminalDeployError(f"user does not exist: {config['user']}") from exc
    if account.pw_uid == 0:
        raise TerminalDeployError("terminal services may not run as root")
    found = {}
    configured = {"ttyd": config.get("ttyd_path"), "tmux": config.get("tmux_path")}
    for program in ("ttyd", "tmux", "systemctl", "systemd-analyze"):
        path = configured.get(program) or which(program)
        if not path or not Path(path).is_absolute():
            raise TerminalDeployError(f"required executable not found: {program}")
        if not Path(path).is_file() or not os.access(path, os.X_OK):
            raise TerminalDeployError(f"required executable is not executable: {path}")
        found[program] = path
    version = run([found["ttyd"], "--version"], capture_output=True, text=True, check=False)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version.stdout + version.stderr)
    if not match or tuple(map(int, match.groups())) < (1, 7, 4):
        raise TerminalDeployError("ttyd >= 1.7.4 is required")
    for s in _sessions(config):
        if not Path(s["cwd"]).is_dir():
            raise TerminalDeployError(f"workspace does not exist: {s['cwd']}")
        if not s["argv"][0].startswith("/"):
            raise TerminalDeployError(f"session {s['name']}: argv[0] must be absolute for installation")
    return found


def manifest_for(artifacts: Mapping[str, Artifact], source_config: str,
                 unsafe_acknowledged: bool = False) -> dict[str, Any]:
    return {"schema": 1, "source_config": str(Path(source_config).resolve()),
            "unsafe_flags_acknowledged": unsafe_acknowledged,
            "artifacts": {a.path: {"sha256": hashlib.sha256(a.content.encode()).hexdigest(), "mode": a.mode}
                          for a in artifacts.values()}}


def install(config_path: str | Path, *, root: str | Path = "/", dry_run: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    artifacts = render_artifacts(config)
    check_prerequisites(config)
    manifest = manifest_for(artifacts, str(config_path), False)
    if dry_run:
        return manifest
    if os.geteuid() != 0:
        raise TerminalDeployError("installation requires root")
    root = Path(root).resolve()
    for artifact in artifacts.values():
        target = root / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".herald-")
        try:
            os.write(fd, artifact.content.encode()); os.close(fd); fd = -1
            os.chmod(temp_name, artifact.mode); os.replace(temp_name, target)
        finally:
            if fd >= 0: os.close(fd)
            if os.path.exists(temp_name): os.unlink(temp_name)
    mp = root / "var/lib/herald/terminal/install-manifest.json"
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["systemd-analyze", "verify", str(root / ARTIFACT_PATHS["session_unit"]),
                    str(root / ARTIFACT_PATHS["gateway_unit"])], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    for s in _sessions(config): subprocess.run(["systemctl", "enable", "--now", f"herald-terminal@{s['name']}.service"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "herald-terminal-gateway.service"], check=True)
    return manifest


def remove(*, root: str | Path = "/", stop_sessions: bool = False) -> list[str]:
    root = Path(root).resolve(); mp = root / "var/lib/herald/terminal/install-manifest.json"
    if not mp.exists(): return []
    manifest = json.loads(mp.read_text(encoding="utf-8")); removed = []
    subprocess.run(["systemctl", "disable", "--now", "herald-terminal-gateway.service"], check=False)
    if stop_sessions:
        subprocess.run(["systemctl", "stop", "herald-terminal@*.service"], check=False)
    leftovers = []
    for rel, meta in manifest.get("artifacts", {}).items():
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            leftovers.append(rel)
            continue
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == meta["sha256"]:
            target.unlink(); removed.append(rel)
        elif target.exists():
            leftovers.append(rel)
    if leftovers:
        manifest["retained_modified_artifacts"] = sorted(leftovers)
        mp.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    else:
        mp.unlink()
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    return removed
