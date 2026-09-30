"""Install and launch Herald's managed coding environment."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import httpx


PI_VERSION = "0.84.2"
MINIMUM_NODE_MAJOR = 22
PRIVATE_NODE_VERSION = "22.19.0"
THEME_PALETTES: dict[str, dict[str, str]] = {
    "herald-midnight": {"cyan": "#7DD3FC", "blue": "#818CF8", "violet": "#C084FC", "green": "#34D399", "panel": "#080D18"},
    "herald-paper": {"cyan": "#0369A1", "blue": "#1D4ED8", "violet": "#7E22CE", "green": "#047857", "red": "#BE123C", "amber": "#A16207", "panel": "#E8EEF5", "panelGreen": "#DCFCE7", "panelRed": "#FFE4E6", "mutedGray": "#475569", "dimGray": "#64748B"},
    "herald-halloween": {"cyan": "#FF8C1A", "blue": "#7C3AED", "violet": "#C084FC", "green": "#A3E635", "red": "#FB3C3C", "amber": "#FDBA2D", "panel": "#1A1025"},
    "herald-winter": {"cyan": "#BDEBFF", "blue": "#60A5FA", "violet": "#DDD6FE", "green": "#86EFAC", "red": "#FCA5A5", "amber": "#FDE68A", "panel": "#0B1B33"},
    "herald-valentine": {"cyan": "#FB7185", "blue": "#E879F9", "violet": "#C084FC", "green": "#FDA4AF", "red": "#FF477E", "amber": "#F9A8D4", "panel": "#2A1020"},
    "herald-pride": {"cyan": "#22D3EE", "blue": "#3B82F6", "violet": "#A855F7", "green": "#22C55E", "red": "#EF4444", "amber": "#EAB308", "panel": "#171526"},
}


@dataclass(frozen=True)
class ShellRuntime:
    root: Path
    node: str
    cli: Path
    extension: Path
    subagent_extension: Path | None = None
    plan_mode_extension: Path | None = None


def _node_major(executable: str) -> int | None:
    try:
        output = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10,
        ).stdout.strip().lstrip("v")
        return int(output.split(".", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _private_node_layout() -> tuple[Path, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    runtime = Path.home() / ".herald" / "runtime" / f"node-v{PRIVATE_NODE_VERSION}"
    if system == "windows":
        archive = f"node-v{PRIVATE_NODE_VERSION}-win-{architecture}.zip"
        return runtime, archive, "node.exe"
    if system == "linux":
        archive = f"node-v{PRIVATE_NODE_VERSION}-linux-{architecture}.tar.xz"
        return runtime, archive, "bin/node"
    raise RuntimeError(f"automatic private Node installation is unsupported on {platform.system()}")


def _install_private_node() -> str:
    runtime, archive_name, relative_node = _private_node_layout()
    node = runtime / relative_node
    npm_link = node.with_name("npm.cmd" if os.name == "nt" else "npm")
    npm_layout_ok = npm_link.exists() and (os.name == "nt" or npm_link.is_symlink())
    if (_node_major(str(node)) or 0) >= MINIMUM_NODE_MAJOR and npm_layout_ok:
        return str(node)
    runtime.mkdir(parents=True, exist_ok=True)
    url = f"https://nodejs.org/dist/v{PRIVATE_NODE_VERSION}/{archive_name}"
    with tempfile.TemporaryDirectory(prefix="herald-node-") as temp_value:
        temp = Path(temp_value)
        archive_path = temp / archive_name
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with open(archive_path, "wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        extracted = temp / "extracted"
        extracted.mkdir()
        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extracted)
        else:
            with tarfile.open(archive_path, "r:xz") as archive:
                archive.extractall(extracted, filter="data")
        roots = [path for path in extracted.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RuntimeError("unexpected Node archive layout")
        # npm/npx/corepack are relative symlinks in Linux Node archives.
        # A previous install may have followed them into regular files, so
        # replace only those generated destinations before preserving links.
        for source in roots[0].rglob("*"):
            if not source.is_symlink():
                continue
            destination = runtime / source.relative_to(roots[0])
            if destination.exists() or destination.is_symlink():
                destination.unlink()
        shutil.copytree(roots[0], runtime, dirs_exist_ok=True, symlinks=True)
    if (_node_major(str(node)) or 0) < MINIMUM_NODE_MAJOR:
        raise RuntimeError("private Node installation completed but its executable is unusable")
    return str(node)


def _resolve_node() -> str:
    configured = os.environ.get("HERALD_NODE")
    candidates = [configured, shutil.which("node")]
    for candidate in candidates:
        if candidate and (_node_major(candidate) or 0) >= MINIMUM_NODE_MAJOR:
            return candidate
    return _install_private_node()


def _npm_for_node(node: str) -> tuple[str, dict[str, str]]:
    node_path = Path(node)
    sibling = node_path.with_name("npm.cmd" if os.name == "nt" else "npm")
    npm = str(sibling) if sibling.exists() else shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is required to install the Herald shell runtime")
    env = os.environ.copy()
    env["PATH"] = f"{node_path.parent}{os.pathsep}{env.get('PATH', '')}"
    return npm, env


def runtime_root() -> Path:
    override = os.environ.get("HERALD_SHELL_RUNTIME")
    return Path(override).expanduser().resolve() if override else Path.home() / ".herald" / "runtime" / f"pi-{PI_VERSION}"


def _apply_herald_runtime_overlay(root: Path) -> None:
    """Make the pinned terminal engine one Herald surface, not a nested product.

    The overlay is deliberately version-locked and sentinel-checked.  An
    upstream layout change therefore fails loudly instead of silently bringing
    back a second login system.
    """
    package_root = root / "node_modules" / "@earendil-works" / "pi-coding-agent"
    package_path = package_root / "package.json"
    metadata = json.loads(package_path.read_text(encoding="utf-8"))
    if metadata.get("version") != PI_VERSION:
        raise RuntimeError(
            f"Herald terminal overlay expects {PI_VERSION}, found {metadata.get('version')}"
        )
    metadata["piConfig"] = {"name": "Herald", "configDir": ".herald/agent"}
    package_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    slash_path = package_root / "dist" / "core" / "slash-commands.js"
    slash_source = slash_path.read_text(encoding="utf-8")
    for original in (
        '    { name: "model", description: "Select model (opens selector UI)", argumentHint: "<provider/model>" },\n',
        '    { name: "login", description: "Configure provider authentication", argumentHint: "<provider>" },\n',
        '    { name: "logout", description: "Remove provider authentication" },\n',
    ):
        slash_source = slash_source.replace(original, "")
    if any(f'name: "{name}"' in slash_source for name in ("model", "login", "logout")):
        raise RuntimeError("Herald could not claim the terminal model/authentication command list")
    slash_path.write_text(slash_source, encoding="utf-8")

    interactive_path = package_root / "dist" / "modes" / "interactive" / "interactive-mode.js"
    interactive_source = interactive_path.read_text(encoding="utf-8")
    model_marker = "            // Herald owns mode-first model routing.\n"
    model_block = '''            if (text === "/model" || text.startsWith("/model ")) {
                const searchTerm = text.startsWith("/model ") ? text.slice(7).trim() : undefined;
                this.editor.setText("");
                await this.handleModelCommand(searchTerm);
                return;
            }
'''
    if model_block in interactive_source:
        interactive_source = interactive_source.replace(model_block, model_marker, 1)
    elif model_marker not in interactive_source:
        raise RuntimeError("Herald could not claim the terminal model command")

    old_marker = "            // Herald owns authentication through its account registry.\n"
    provider_block = '''            // Herald layers native provider OAuth under /login provider.
            if (text === "/login provider" || text.startsWith("/login provider ")) {
                const providerRef = text.startsWith("/login provider ") ? text.slice(16).trim() : undefined;
                this.editor.setText("");
                await this.handleLoginCommand(providerRef);
                return;
            }
            if (text === "/logout provider") {
                this.showOAuthSelector("logout");
                this.editor.setText("");
                return;
            }
'''
    login_block = '''            if (text === "/login" || text.startsWith("/login ")) {
                const providerRef = text.startsWith("/login ") ? text.slice(7).trim() : undefined;
                this.editor.setText("");
                await this.handleLoginCommand(providerRef);
                return;
            }
            if (text === "/logout") {
                this.showOAuthSelector("logout");
                this.editor.setText("");
                return;
            }
'''
    if login_block in interactive_source:
        interactive_source = interactive_source.replace(login_block, provider_block, 1)
    elif old_marker in interactive_source:
        interactive_source = interactive_source.replace(old_marker, provider_block, 1)
    elif provider_block not in interactive_source:
        raise RuntimeError("Herald could not claim the terminal authentication commands")
    interactive_path.write_text(interactive_source, encoding="utf-8")

    # Gracefully handle directories when read tool is called on a folder instead of throwing EISDIR
    read_path = package_root / "dist" / "core" / "tools" / "read.js"
    if read_path.exists():
        read_source = read_path.read_text(encoding="utf-8")
        if 'import { access as fsAccess, readFile as fsReadFile } from "fs/promises";' in read_source:
            read_source = read_source.replace(
                'import { access as fsAccess, readFile as fsReadFile } from "fs/promises";',
                'import { access as fsAccess, readFile as fsReadFile, stat as fsStat, readdir as fsReaddir } from "fs/promises";',
            )
        dir_guard = '''                        // Check if file exists and is readable.
                        await ops.access(absolutePath);
                        if (aborted)
                            return;
                        const stat = await fsStat(absolutePath);
                        if (stat.isDirectory()) {
                            const entries = await fsReaddir(absolutePath, { withFileTypes: true });
                            const items = entries.slice(0, 100).map(e => e.isDirectory() ? `${e.name}/` : e.name).join("\\n");
                            const more = entries.length > 100 ? `\\n... and ${entries.length - 100} more items` : "";
                            const msg = `[Directory listing for: ${path}]\\nTotal items: ${entries.length}\\n${items}${more}\\n\\n(Tip: "${path}" is a directory. To read a specific file inside it, call read on that file path, or use bash to run commands.)`;
                            resolve({ content: [{ type: "text", text: msg }] });
                            return;
                        }'''
        target_access = '''                        // Check if file exists and is readable.
                        await ops.access(absolutePath);
                        if (aborted)
                            return;'''
        if target_access in read_source and 'if (stat.isDirectory())' not in read_source:
            read_source = read_source.replace(target_access, dir_guard, 1)
            read_path.write_text(read_source, encoding="utf-8")

    # Pi's example inherits a provider/model pair but starts a bare child pi
    # process, where Herald's custom provider has not been registered. Keep the
    # upstream implementation while making each child load Herald's extension
    # and send the route name through the Herald provider.
    subagent_path = package_root / "examples" / "extensions" / "subagent" / "index.ts"
    if not subagent_path.exists():
        return
    subagent_source = subagent_path.read_text(encoding="utf-8")
    bare_args = 'const args: string[] = ["--mode", "json", "-p", "--no-session"];'
    herald_args = '''const heraldExtension = process.env.HERALD_EXTENSION_PATH;
	if (!heraldExtension) throw new Error("HERALD_EXTENSION_PATH is required for Herald subagents");
	const args: string[] = ["--mode", "json", "-p", "--no-session", "--extension", heraldExtension, "--provider", "herald"];'''
    if bare_args in subagent_source:
        subagent_source = subagent_source.replace(bare_args, herald_args, 1)
    elif herald_args not in subagent_source:
        raise RuntimeError("Herald could not route pi subagents through its provider")
    qualified_model = 'model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined,'
    routed_model = 'model: ctx.model?.id,'
    if qualified_model in subagent_source:
        subagent_source = subagent_source.replace(qualified_model, routed_model, 1)
    elif routed_model not in subagent_source:
        raise RuntimeError("Herald could not adapt pi subagent model inheritance")
    subagent_path.write_text(subagent_source, encoding="utf-8")



def _write_themes(root: Path, source: str) -> Path:
    theme_dir = root / "themes"
    theme_dir.mkdir(parents=True, exist_ok=True)
    base = json.loads(source)
    (theme_dir / "herald.json").write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
    for name, palette in THEME_PALETTES.items():
        themed = json.loads(source)
        themed["name"] = name
        themed["vars"].update(palette)
        (theme_dir / f"{name}.json").write_text(json.dumps(themed, indent=2) + "\n", encoding="utf-8")
    return theme_dir


def install_shell(*, force: bool = False) -> ShellRuntime:
    root = runtime_root()
    node = _resolve_node()
    package_json = root / "package.json"
    extension = root / "herald.ts"
    subagent_extension = root / "node_modules" / "@earendil-works" / "pi-coding-agent" / "examples" / "extensions" / "subagent" / "index.ts"
    plan_mode_extension = root / "node_modules" / "@narumitw" / "pi-plan-mode" / "dist" / "index.ts"
    cli = root / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "cli.js"
    assets = files("herald.shell_assets")
    if force or not cli.exists() or not plan_mode_extension.exists():
        root.mkdir(parents=True, exist_ok=True)
        package_json.write_text(assets.joinpath("package.json").read_text(encoding="utf-8"), encoding="utf-8")
        npm, install_env = _npm_for_node(node)
        result = subprocess.run(
            [npm, "install", "--omit=dev", "--no-audit", "--no-fund"], cwd=root,
            env=install_env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"npm failed to install the Herald shell runtime ({result.returncode})")
    _apply_herald_runtime_overlay(root)
    # These are Herald-owned managed assets. Refresh them on every launch so an
    # editable/package upgrade never leaves an older command surface in place.
    extension.write_text(assets.joinpath("herald.ts").read_text(encoding="utf-8"), encoding="utf-8")
    agent_dir = Path.home() / ".herald" / "agent"
    for kind in ("agents", "prompts"):
        destination = agent_dir / kind
        destination.mkdir(parents=True, exist_ok=True)
        source_dir = assets.joinpath("subagent", kind)
        for source in source_dir.iterdir():
            if source.name.endswith(".md"):
                (destination / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    _write_themes(root, assets.joinpath("herald-theme.json").read_text(encoding="utf-8"))
    if not plan_mode_extension.is_file():
        raise RuntimeError("Herald's managed Plan Mode extension is missing")
    return ShellRuntime(
        root=root, node=node, cli=cli, extension=extension,
        subagent_extension=subagent_extension, plan_mode_extension=plan_mode_extension,
    )


def launch_shell(
    *, cwd: str | Path = ".", model: str = "balanced",
    project: str | None = None, part: str | None = None,
    prompt: str | None = None, extra_args: list[str] | None = None,
    memory_bank: bool = False,
) -> int:
    runtime = install_shell()
    workspace = Path(cwd).resolve()
    env = os.environ.copy()
    agent_dir = Path.home() / ".herald" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    settings_file = agent_dir / "settings.json"
    if settings_file.exists():
        try:
            settings_data = json.loads(settings_file.read_text(encoding="utf-8"))
            if settings_data.get("defaultModel") == "g4f-gateway":
                settings_data["defaultModel"] = "balanced"
                settings_file.write_text(json.dumps(settings_data, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
    env["HERALD_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)  # Compatibility with pre-overlay runtimes.
    env["HERALD_EXTENSION_PATH"] = str(runtime.extension)
    # Keep the shell extension on the same safe loopback address as the Python
    # CLI. In particular, do not rely on Node resolving `localhost` to IPv4.
    env.setdefault("HERALD_URL", "http://127.0.0.1:8790")
    if project and part:
        env.update({"HERALD_PROJECT": project, "HERALD_PART": part})
    command = [runtime.node, str(runtime.cli), "--extension", str(runtime.extension)]
    if runtime.subagent_extension is not None:
        command.extend(["--extension", str(runtime.subagent_extension)])
    if runtime.plan_mode_extension is not None:
        command.extend(["--extension", str(runtime.plan_mode_extension)])
    command.extend([
        "--theme", str(runtime.root / "themes"), "--use-theme", "herald",
        "--provider", "herald", "--model", model,
    ])
    command_dirs = [
        Path.home() / ".herald" / "commands",
        workspace / ".herald" / "commands",
        workspace / ".claude" / "commands",
        workspace / ".codex" / "prompts",
    ]
    for directory in command_dirs:
        if directory.is_dir():
            command.extend(["--prompt-template", str(directory)])
    supplied_args = extra_args or []
    if memory_bank and "--no-context-files" not in supplied_args and "-nc" not in supplied_args:
        from herald.memory_bank import load_memory_bank

        context = load_memory_bank(workspace, enabled=True)
        if context.text:
            command.extend(["--append-system-prompt", context.text])
    command.extend(supplied_args)
    if prompt:
        command.append(prompt)
    # A print-mode invocation already receives its prompt as an argument.  Do
    # not leave the terminal engine attached to an inherited pipe (notably `ssh host herald
    # code --print ...`), because an SSH client may keep that pipe open after
    # the response has completed and prevent the Node process from exiting.
    stdin = subprocess.DEVNULL if "--print" in (extra_args or []) else None
    return subprocess.run(command, cwd=workspace, env=env, stdin=stdin).returncode
