"""Herald CLI — agents, models, and tools through one router.

Usage:
    herald ask "Explain the code" [-m codex] [--code] [--reason] [--fast]
    herald run "Execute task"
    herald agent [--model antigravity]
    herald tools
    herald tool <name> [key=value ...] [--json '{...}']
    herald clink <cli_name> "prompt"
    herald models
    herald auth
    herald start [--port 8790] [--foreground]
    herald stop
    herald status
    herald register [path]
    herald logs [--limit 20]
"""
from __future__ import annotations

from herald.logging_config import configure_logging

configure_logging()

# See herald/router/server.py for why this matters: `herald setup` writes
# HERALD_URL, provider keys, and preferences to storage_paths.data_dir()/
# ".env", and nothing loaded that path before this change -- every CLI
# invocation silently ignored everything the wizard collected. Must run
# before ROUTER_URL is computed from os.environ below.
from dotenv import load_dotenv as _load_dotenv
from herald.router.storage_paths import data_dir as _data_dir

_load_dotenv(_data_dir() / ".env", override=False)

from herald.router.env_check import hydrate_provider_keys_from_keyring

hydrate_provider_keys_from_keyring()

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
from rich import box, print as rprint
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


app = typer.Typer(
    name="herald",
    help="Herald - one router for models, tools, agents, and applications",
    invoke_without_command=True,
    no_args_is_help=False,
)
tool_app = typer.Typer(help="Inspect and execute registered tools.", no_args_is_help=True)
account_app = typer.Typer(help="Manage named CLI, API, G4F, and local-model accounts.", no_args_is_help=True)
flow_app = typer.Typer(help="Validate and run declarative multi-model flows.", no_args_is_help=True)
capture_app = typer.Typer(help="Capture and refresh browser-session authentication.", no_args_is_help=True)
integration_app = typer.Typer(help="Discover and import MCP connections from other CLIs.", no_args_is_help=True)
hook_app = typer.Typer(help="Manage sanitized router event hooks.", no_args_is_help=True)
memory_app = typer.Typer(help="Manage encrypted stateful-agent memories.", no_args_is_help=True)
mcp_app = typer.Typer(help="Manage controlled MCP groups and the shared Herald gateway.", no_args_is_help=True)
mcp_group_app = typer.Typer(help="Create groups, choose tools, and bind access levels.", no_args_is_help=True)
mcp_native_app = typer.Typer(help="Register Herald's pinned filesystem, shell, and GitHub integrations.", no_args_is_help=True)
connect_app = typer.Typer(help="Auto-discover and connect Ollama, LM Studio, OpenRouter, and custom APIs.", no_args_is_help=True)
schedule_app = typer.Typer(help="Manage cron/event-triggered scheduled runs.", no_args_is_help=True)
capability_app = typer.Typer(help="Review self-drafted tool proposals for missing capabilities.", no_args_is_help=True)
history_app = typer.Typer(help="Search prompt and response history across stored sessions.", no_args_is_help=True)
terminal_app = typer.Typer(help="Render and manage secure persistent ttyd/tmux terminals.", no_args_is_help=True)
admin_app = typer.Typer(help="Review, approve, or deny autonomous Admin work.", no_args_is_help=True)
device_app = typer.Typer(help="Discover, approve, and manage other Herald nodes on the LAN.", no_args_is_help=True)
app.add_typer(tool_app, name="tool")
app.add_typer(account_app, name="account")
app.add_typer(flow_app, name="flow")
app.add_typer(capture_app, name="capture")
app.add_typer(integration_app, name="integration")
app.add_typer(hook_app, name="hook")
app.add_typer(memory_app, name="memory")
app.add_typer(connect_app, name="connect")
app.add_typer(schedule_app, name="schedule")
app.add_typer(capability_app, name="capability")
app.add_typer(history_app, name="history")
app.add_typer(terminal_app, name="terminal")
app.add_typer(admin_app, name="admin")
app.add_typer(device_app, name="devices")
mcp_app.add_typer(mcp_group_app, name="group")
mcp_app.add_typer(mcp_native_app, name="native")
app.add_typer(mcp_app, name="mcp")
console = Console()


def _pending_admin_reviews() -> list[dict[str, Any]]:
    reviews = _client().admin_reviews()
    if isinstance(reviews, list):
        return reviews
    return []


def _resolve_admin_review_id(dispatch_id: str | None) -> str:
    if dispatch_id:
        return dispatch_id
    reviews = _pending_admin_reviews()
    if not reviews:
        rprint("[dim]No Admin work is awaiting review.[/dim]")
        raise typer.Exit(1)
    if len(reviews) > 1:
        rprint("[yellow]Multiple reviews are pending; provide a dispatch ID.[/yellow]")
        raise typer.Exit(1)
    return str(reviews[0]["dispatch_id"])


@admin_app.command("reviews")
def admin_reviews(as_json: bool = typer.Option(False, "--json")):
    """Show the concise review inbox for completed autonomous work."""
    reviews = _pending_admin_reviews()
    if as_json:
        rprint(json.dumps({"count": len(reviews), "reviews": reviews}, indent=2))
        return
    if not reviews:
        rprint("[dim]No Admin work is awaiting review.[/dim]")
        return
    for review in reviews:
        rprint(Panel(
            "\n".join([
                f"[bold]{review.get('objective') or 'Untitled Admin bundle'}[/bold]",
                f"Dispatch: {review['dispatch_id']}",
                f"Backend: {review.get('backend') or 'unknown'}",
                f"Commit: {str(review.get('head_commit') or '')[:12]}",
                f"Files: {len(review.get('changed_files') or [])}",
                *[f"  - {task}" for task in review.get("tasks") or []],
            ]),
            title="Awaiting approval",
            border_style="yellow",
        ))


@admin_app.command("status")
def admin_status(as_json: bool = typer.Option(False, "--json")):
    """Show live Admin activity and work awaiting your decision."""
    payload = _client()._get("/admin/status")
    if as_json:
        rprint(json.dumps(payload, indent=2))
        return
    active = payload.get("active_dispatch")
    rprint(f"Admin: [bold]{payload.get('control', 'unknown')}[/bold]")
    if active:
        rprint(f"Active: {active.get('objective')} ({active.get('status')})")
    else:
        rprint("Active: none")
    admin_reviews(as_json=False)


@admin_app.command("approve")
def admin_approve(dispatch_id: Optional[str] = typer.Argument(None)):
    """Approve one reviewed bundle and merge it into the configured workspace."""
    selected = _resolve_admin_review_id(dispatch_id)
    result = _client().approve_admin_review(selected)
    if result.get("error"):
        rprint(f"[red]Approval failed:[/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Approved {selected}; workspace is at {str(result.get('commit') or '')[:12]}.")


@admin_app.command("deny")
def admin_deny(
    dispatch_id: Optional[str] = typer.Argument(None),
    reason: str = typer.Option(..., "--reason", "-r"),
):
    """Deny one bundle, remove it from active work, and retain an audit ref."""
    selected = _resolve_admin_review_id(dispatch_id)
    result = _client().deny_admin_review(selected, reason)
    if result.get("error"):
        rprint(f"[red]Denial failed:[/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Denied {selected}; preserved as {result.get('audit_ref')}.")


@device_app.command("pending")
def devices_pending(as_json: bool = typer.Option(False, "--json")):
    """List Herald nodes discovered on the LAN that aren't trusted yet.

    Works the same over SSH on a GUI-less server as it does locally --
    approval doesn't require sitting at the device itself.
    """
    _ensure_router()
    rows = _client().pending_devices()
    if as_json:
        rprint(json.dumps(rows, indent=2))
        return
    if not rows:
        rprint("No new devices discovered.")
        return
    rprint(Panel.fit(
        "\n".join([f"[bold]{row['short_code']}[/bold]  {row['display_name']}" for row in rows]),
        title="Pending devices -- approve with: herald devices approve <code>",
        border_style="cyan",
    ))


@device_app.command("approve")
def devices_approve(code: str = typer.Argument(..., help="The short code shown on the new device")):
    """Trust a pending device and hand it a bearer token, verified by its own signature."""
    _ensure_router()
    result = _client().approve_device(code)
    if result.get("error"):
        rprint(f"[red]Approval failed:[/red] {result['error']}")
        raise typer.Exit(1)
    device = result.get("device", {})
    rprint(f"[green][OK][/green] Trusted {device.get('display_name', code)} ({device.get('short_code', code)}).")
    if result.get("push_warning"):
        rprint(f"[yellow]Note:[/yellow] {result['push_warning']}")


@device_app.command("list")
def devices_list(as_json: bool = typer.Option(False, "--json")):
    """List Herald nodes this one already trusts."""
    _ensure_router()
    rows = _client().trusted_devices()
    if as_json:
        rprint(json.dumps(rows, indent=2))
        return
    if not rows:
        rprint("No trusted devices yet.")
        return
    rprint(Panel.fit(
        "\n".join([f"[bold]{row['short_code']}[/bold]  {row['display_name']}" for row in rows]),
        title="Trusted devices",
        border_style="green",
    ))


@device_app.command("revoke")
def devices_revoke(node_id: str = typer.Argument(..., help="node_id from 'herald devices list --json'")):
    """Revoke a trusted device's access."""
    _ensure_router()
    result = _client().revoke_device(node_id)
    if result.get("error"):
        rprint(f"[red]Revoke failed:[/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Revoked {node_id}.")


@admin_app.command("open")
def admin_open(
    port: int = typer.Option(0, "--port", help="Local console port; 0 chooses an available port"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the console in your browser"),
    repo: Optional[Path] = typer.Option(None, "--repo", file_okay=False, help="Development checkout to update after approval"),
):
    """Open the private Admin review console (development-build only)."""
    try:
        from herald.admin_gui import configured_connection, find_repo_root, launch
    except ImportError:
        rprint("[red][X][/red] The Admin console is a development-build-only feature.")
        raise typer.Exit(1) from None

    router_url, api_key = configured_connection()
    if not api_key:
        rprint("[red][X][/red] HERALD_API_KEY is not configured for the Admin console's Router connection.")
        raise typer.Exit(1)
    launch(
        router_url=router_url,
        api_key=api_key,
        repo=(repo or find_repo_root()),
        port=port,
        open_browser=open_browser,
    )


@terminal_app.command("render")
def terminal_render(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    output: Path = typer.Option(..., "--output", file_okay=False),
):
    """Deterministically render inspectable launcher and systemd artifacts."""
    from herald.terminal_deploy import TerminalDeployError, render
    try:
        paths = render(config, output)
    except TerminalDeployError as exc:
        raise typer.BadParameter(str(exc)) from None
    for path in paths:
        rprint(path)


@terminal_app.command("install")
def terminal_install(
    config: Path = typer.Option(..., "--config", exists=True, dir_okay=False),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Validate prerequisites and idempotently install Herald-owned files."""
    from herald.terminal_deploy import TerminalDeployError, install
    try:
        result = install(config, dry_run=dry_run)
    except (TerminalDeployError, subprocess.CalledProcessError) as exc:
        rprint(f"[red]terminal install failed:[/red] {exc}")
        raise typer.Exit(1) from None
    rprint(json.dumps(result, indent=2, sort_keys=True))


@terminal_app.command("status")
def terminal_status(as_json: bool = typer.Option(False, "--json")):
    """Report gateway and persistent-session unit state without changing it."""
    units = ["herald-terminal-gateway.service", "herald-terminal@*.service"]
    result = subprocess.run(["systemctl", "show", "--property=Id,ActiveState,SubState", *units],
                            capture_output=True, text=True, check=False)
    if as_json:
        rprint(json.dumps({"returncode": result.returncode, "units": result.stdout}, sort_keys=True))
    else:
        rprint(result.stdout.rstrip() or result.stderr.rstrip())
    if result.returncode: raise typer.Exit(result.returncode)


@terminal_app.command("remove")
def terminal_remove(stop_sessions: bool = typer.Option(False, "--stop-sessions")):
    """Remove only unchanged manifest-owned files; sessions survive by default."""
    from herald.terminal_deploy import remove
    removed = remove(stop_sessions=stop_sessions)
    rprint(f"Removed {len(removed)} Herald-owned artifact(s).")


ROUTER_URL = os.environ.get("HERALD_URL", "http://127.0.0.1:8790")


@app.callback()
def root_command(ctx: typer.Context):
    """Launch the coding environment when Herald is called without a subcommand."""
    if ctx.invoked_subcommand is None:
        _run_coding_shell(
            path=".", model="balanced", project=None, part="main",
            prompt=None, system_prompt=None, reinstall=False, print_mode=False,
        )


def _client():
    from herald.client import RouterClient
    return RouterClient(ROUTER_URL)


def _router_alive() -> bool:
    try:
        key = os.environ.get("HERALD_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        httpx.get(f"{ROUTER_URL}/health", headers=headers, timeout=2).raise_for_status()
        return True
    except Exception:
        return False


def _ensure_router(silent: bool = False) -> bool:
    """Ensure Herald router server is running. Auto-starts if down."""
    if _router_alive():
        return True
    if not silent:
        rprint("[yellow]![/yellow] Herald router daemon is down. Auto-starting router...")
    port = int(ROUTER_URL.split(":")[-1].split("/")[0])
    cmd = [
        sys.executable, "-m", "uvicorn", "herald.router.server:app",
        "--host", "127.0.0.1", "--port", str(port)
    ]
    try:
        flags = 0
        if sys.platform == "win32":
            flags = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        for _ in range(15):
            time.sleep(0.3)
            if _router_alive():
                if not silent:
                    rprint(f"[green][OK][/green] Herald router auto-started at [bold]{ROUTER_URL}[/bold]")
                return True
    except Exception as exc:
        rprint(f"[red][X][/red] Auto-starting router failed: {exc}")
    return False


@app.command(name="setup")
def cli_setup():
    """Run the interactive onboarding wizard to configure models, deployment roles, and safety."""
    from herald.cli.wizard import run_setup_wizard
    run_setup_wizard()


@app.command(name="dashboard")
def open_dashboard(
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the local default browser"),
):
    """Open the Herald Account Console."""
    _ensure_router()
    url = f"{ROUTER_URL.rstrip('/')}/ui"
    remote_shell = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
    opened = open_browser and not remote_shell and webbrowser.open(url)
    if remote_shell and open_browser:
        rprint(f"[yellow]![/yellow] Remote shell detected; open [bold]{url}[/bold] through SSH forwarding or Tailscale.")
    elif opened:
        rprint(f"[green][OK][/green] Opened [bold]{url}[/bold]")
    else:
        rprint(f"[cyan]Herald Account Console:[/cyan] {url}")


# ---------------------------------------------------------------------------
# herald start & stop
# ---------------------------------------------------------------------------

@app.command()
def start(
    port: int = typer.Option(8790, "--port", "-p", help="Port to run on"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address; 0.0.0.0 requires HERALD_API_KEY"),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Block instead of backgrounding"),
):
    """Start the Herald router daemon."""
    if _router_alive() and not foreground:
        rprint(f"[green][OK][/green] Herald router already running at {ROUTER_URL}")
        return

    if host == "0.0.0.0" and not os.environ.get("HERALD_API_KEY"):
        raise typer.BadParameter("HERALD_API_KEY is required when --host 0.0.0.0", param_hint="--host")
    child_env = os.environ.copy()
    child_env["HERALD_BIND_HOST"] = host
    cmd = [
        sys.executable, "-m", "uvicorn", "herald.router.server:app",
        "--host", host, "--port", str(port)
    ]

    if foreground:
        rprint(f"[bold]Starting Herald router on port {port}...[/bold]")
        subprocess.run(cmd, env=child_env)
    else:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            env=child_env,
        )
        for _ in range(20):
            time.sleep(0.5)
            if _router_alive():
                rprint(f"[green][OK][/green] Herald router started at [bold]{ROUTER_URL}[/bold]")
                return
        rprint("[red][X][/red] Router didn't respond within 10s — check logs")


@app.command()
def stop():
    """Stop the Herald router daemon."""
    if sys.platform == "win32":
        port = ROUTER_URL.split(":")[-1].split("/")[0]
        result = subprocess.run(
            f"netstat -ano | findstr :{port}", shell=True, capture_output=True, text=True
        )
        pids = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
        for pid in pids:
            subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
        rprint(f"[green][OK][/green] Stopped {len(pids)} process(es) on router port")
    else:
        try:
            subprocess.run(["pkill", "-f", "herald.router.server"], capture_output=True)
            rprint("[green][OK][/green] Router stopped")
        except FileNotFoundError:
            # pkill isn't installed on every base image (minimal/slim
            # containers, for one) -- fall back to a pure-stdlib /proc scan
            # instead of failing outright.
            killed = 0
            proc_root = Path("/proc")
            for entry in proc_root.iterdir() if proc_root.is_dir() else []:
                if not entry.name.isdigit():
                    continue
                try:
                    cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
                except OSError:
                    continue
                if b"herald.router.server" in cmdline:
                    try:
                        os.kill(int(entry.name), signal.SIGTERM)
                        killed += 1
                    except OSError:
                        pass
            if killed:
                rprint(f"[green][OK][/green] Stopped {killed} process(es)")
            else:
                rprint("[yellow]![/yellow] No running Router process found (and 'pkill' isn't installed to search by name)")


@app.command(name="update")
def cli_update(
    restart: bool = typer.Option(True, "--restart/--no-restart", help="Automatically restart router if running"),
    auto: bool | None = typer.Option(None, "--auto/--no-auto", help="Enable or disable automatic background updates (opt-in)"),
):
    """Update Herald to the latest release (via Git or PyPI) and manage opt-in auto-updates."""
    config_dir = Path.home() / ".herald"
    env_file = config_dir / ".env"

    if auto is not None:
        config_dir.mkdir(parents=True, exist_ok=True)
        lines = []
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if not line.startswith("HERALD_AUTO_UPDATE="):
                    lines.append(line)
        lines.append(f"HERALD_AUTO_UPDATE={'1' if auto else '0'}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        status_text = "[green]enabled[/green]" if auto else "[yellow]disabled[/yellow]"
        rprint(f"[green][OK][/green] Automatic background updates {status_text} (opt-in preference saved).")
        return

    rprint("[bold cyan]Checking for Herald updates...[/bold cyan]")

    was_running = _router_alive()
    is_git_repo = False
    repo_root = None

    try:
        root_res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        )
        if root_res.returncode == 0:
            is_git_repo = True
            repo_root = Path(root_res.stdout.strip())
    except Exception:
        pass

    # Fetch latest changelog / remote changes preview
    changelog_file = (repo_root / "CHANGELOG.md") if repo_root else (Path.home() / ".herald" / "CHANGELOG.md")
    if is_git_repo and repo_root:
        # Check if there are remote updates without applying them immediately
        subprocess.run(["git", "fetch", "origin"], cwd=str(repo_root), capture_output=True)
        diff_summary = subprocess.run(
            ["git", "log", "HEAD..origin/main", "--oneline", "-n", "5"],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        if diff_summary.stdout.strip():
            rprint("\n[bold yellow]Incoming Changes in Latest Build:[/bold yellow]")
            for line in diff_summary.stdout.strip().splitlines():
                rprint(f"  • {line}")

    if changelog_file.exists():
        try:
            content = changelog_file.read_text(encoding="utf-8")
            # Extract latest release section
            sections = content.split("## [")
            if len(sections) > 1:
                latest_section = "## [" + sections[1]
                from rich.markdown import Markdown
                from rich.panel import Panel
                rprint(Panel(Markdown(latest_section[:2500]), title="[bold yellow]Release Notes & Warnings[/bold yellow]", border_style="yellow"))
        except Exception:
            pass

    from rich.prompt import Confirm
    if not Confirm.ask("Proceed with update?", default=True):
        rprint("[yellow]Update cancelled.[/yellow]")
        return

    if is_git_repo and repo_root:
        rprint(f"[dim]Updating git repository at {repo_root}...[/dim]")
        pull_res = subprocess.run(["git", "pull", "--ff-only"], cwd=str(repo_root), capture_output=True, text=True)
        if pull_res.returncode != 0:
            rprint(f"[yellow]![/yellow] Git pull output:\n{pull_res.stderr.strip() or pull_res.stdout.strip()}")
        else:
            rprint(f"[green][OK][/green] {pull_res.stdout.strip() or 'Git repository updated.'}")

        rprint("[dim]Reinstalling Python package in editable mode...[/dim]")
        subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(repo_root), "--upgrade"], capture_output=True)
    else:
        rprint("[dim]Updating package via pip...[/dim]")
        res = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "herald-ai"], capture_output=True, text=True)
        if res.returncode == 0:
            rprint("[green][OK][/green] Herald package updated.")
        else:
            rprint(f"[yellow]![/yellow] Pip install output:\n{res.stderr.strip() or res.stdout.strip()}")

    if was_running and restart:
        rprint("[dim]Restarting Herald router...[/dim]")
        stop()
        time.sleep(1)
        start(foreground=False)

    rprint("[bold green]Herald is up to date![/bold green]")


# ---------------------------------------------------------------------------
# Visual Graphs, Diagrams & Topology
# ---------------------------------------------------------------------------

@app.command(name="graph")
def cli_graph():
    """Render the full visual architecture and routing topology flowchart."""
    from herald.cli.graphs import render_mesh_graph
    _ensure_router()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    render_mesh_graph(_client().status())


@app.command(name="mesh")
def cli_mesh():
    """Alias for herald graph: render the mesh architecture flowchart."""
    cli_graph()


@app.command(name="tree")
def cli_tree():
    """Render an interactive tree hierarchy of all active pools, backends, and tools."""
    from herald.cli.graphs import render_mesh_tree
    _ensure_router()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = _client().status()
    backends = s.get("backends", [])
    tools = _client().list_tools()
    personas = s.get("personas", [])
    render_mesh_tree(backends, tools, personas)


# ---------------------------------------------------------------------------
# herald talk -- voice conversation
# ---------------------------------------------------------------------------

def _reconcile_stt_candidates(candidates: list[dict[str, Any]]) -> str:
    """Multiple speech engines heard the same utterance and may disagree.
    Rather than trust whichever ran first, ask the router to arbitrate --
    it has full sentence context to resolve the kind of errors a single
    ASR engine makes alone (homophones, dropped words)."""
    texts = [c["text"] for c in candidates if c.get("text")]
    if not texts:
        return ""
    unique = list(dict.fromkeys(texts))
    if len(unique) == 1:
        return unique[0]
    listing = "\n".join(f"{i+1}. {t}" for i, t in enumerate(unique))
    prompt = (
        "These are candidate transcriptions of the same short spoken utterance, "
        "produced by different speech recognizers that may have misheard words:\n\n"
        f"{listing}\n\n"
        "Reply with ONLY the single most likely correct transcription -- pick the best "
        "one or merge them if the correct sentence is obvious from combining parts. "
        "No commentary, no quotes, no numbering."
    )
    from herald.client import RouterClient
    reconciled = RouterClient(ROUTER_URL).chat(prompt, prefer_fast=True)
    reconciled = reconciled.strip()
    return reconciled if reconciled and not reconciled.startswith("[error]") else unique[0]


@app.command()
def talk(
    session: str = typer.Option("talk", "--session", "-s", help="Memory session name to keep continuity across turns"),
    rate: int = typer.Option(0, "--rate", help="SAPI speech rate, -10 (slow) to 10 (fast)"),
    text_only: bool = typer.Option(False, "--text-only", help="Skip TTS playback, print replies instead"),
):
    """Have a spoken conversation with Herald: press Enter, talk, press Enter
    again to stop recording. Runs every available local speech engine on what
    you said and has Herald itself arbitrate between them before replying,
    voiced in its plain-language conversational persona."""
    from herald.router import voice

    if not voice.microphone_available():
        rprint("[red][X][/red] Microphone recording needs the 'sounddevice' package: pip install sounddevice")
        raise typer.Exit(1)
    if not voice.whisper_available() and not voice.windows_sapi_available():
        rprint("[red][X][/red] No speech-to-text engine available (install faster-whisper, or run on Windows for SAPI)")
        raise typer.Exit(1)

    _ensure_router()
    can_speak = (not text_only) and voice.windows_sapi_available()
    rprint(f"[bold cyan]Herald voice — session '{session}'[/bold cyan]")
    rprint("[dim]Press Enter to start talking, Enter again to stop. Ctrl+C to quit.[/dim]\n")

    while True:
        try:
            input("[hold Enter, speak, Enter to stop] ")
        except (KeyboardInterrupt, EOFError):
            rprint("\n[dim]Ending voice session.[/dim]")
            break

        with Console().status("[cyan]Listening...[/cyan]", spinner="dots"):
            audio_path = voice.record_microphone(max_seconds=30.0)
        with Console().status("[cyan]Transcribing (running every available engine)...[/cyan]", spinner="dots"):
            candidates = voice.transcribe_all(audio_path)
        os.unlink(audio_path)

        for c in candidates:
            label = c["engine"]
            shown = c.get("text") or f"[error: {c.get('error')}]"
            rprint(f"  [dim]{label}: {shown}[/dim]")

        heard = _reconcile_stt_candidates(candidates)
        if not heard:
            rprint("[yellow]Didn't catch that -- no engine returned a transcript.[/yellow]\n")
            continue
        rprint(f"[bold]You:[/bold] {heard}")

        from herald.router.voice_commands import match_voice_command, execute_voice_command
        matched_cmd = match_voice_command(heard)
        if matched_cmd:
            with Console().status(f"[cyan]{matched_cmd['description']}...[/cyan]", spinner="dots"):
                reply = execute_voice_command(matched_cmd, client_or_none=_client())
        else:
            from herald import chat as herald_chat
            with Console().status("[cyan]Herald is thinking...[/cyan]", spinner="dots"):
                reply = herald_chat(heard, session=session, persona="herald", mode="quality")
        rprint(f"[bold cyan]Herald:[/bold cyan] {reply}\n")

        if can_speak:
            voice.speak_windows(reply, rate=rate)


# ---------------------------------------------------------------------------
# herald status & auth
# ---------------------------------------------------------------------------

@app.command()
def status():

    """Show full Herald router health, backends, auth, and quotas."""
    _ensure_router()
    client = _client()
    s = client.status()

    # Backends table
    table = Table(title="Herald Backends", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Priority")
    table.add_column("Enabled")
    table.add_column("Circuit")
    table.add_column("Pool")

    for b in s.get("backends", []):
        enabled = "[green][OK][/green]" if b.get("enabled") else "[dim]off[/dim]"
        circuit = "[red]OPEN[/red]" if b.get("circuit_open") else "[green]closed[/green]"
        fails = b.get("consecutive_failures", 0)
        if fails:
            circuit += f" ({fails}f)"
        table.add_row(
            b["name"], b["backend_type"], str(b.get("priority", "")),
            enabled, circuit, b.get("pool_name") or "",
        )
    console.print(table)

    # Auth table
    auth = s.get("auth", [])
    if auth:
        auth_table = Table(title="CLI Profile Auth", show_header=True, header_style="bold cyan")
        auth_table.add_column("CLI Adapter")
        auth_table.add_column("Status")
        rows = auth if isinstance(auth, list) else [dict(info, cli=cli) for cli, info in auth.items()]
        for info in rows:
            cli = info.get("cli", "unknown")
            ok = info.get("logged_in") or info.get("status") in {"ok", "logged_in"}
            status_str = "[green][OK] logged in[/green]" if ok else "[red][X] not logged in[/red]"
            auth_table.add_row(cli, status_str)
    rprint(f"\n[dim]Router Endpoint: {ROUTER_URL}[/dim]")


@app.command(name="usage")
def usage_report(

    refresh: bool = typer.Option(True, "--refresh/--cached", help="Refresh native CLI subscription limits"),
    all_models: bool = typer.Option(False, "--all-models", "-a", help="Show all individual backend model aliases"),
):
    """Show high-readability Herald usage, native subscription limits, and cloud capacity."""
    _ensure_router()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    payload = _client()._get("/usage/all", refresh=refresh)
    totals = payload.get("totals", {})
    calls = totals.get("calls", 0)
    tin = totals.get("input_tokens", 0)
    tout = totals.get("output_tokens", 0)
    cost = totals.get("known_cost_usd", 0.0)

    def _fmt_tok(num: int | None) -> str:
        if not num:
            return "-"
        if num >= 1_000_000:
            return f"{num/1_000_000:.1f}M"
        if num >= 1_000:
            return f"{num/1_000:.1f}k"
        return str(num)

    def _progress(rem_pct: float | None, width: int = 12) -> str:
        rem = max(0, min(100, rem_pct or 0))
        filled = int(round((rem / 100) * width))
        empty = width - filled
        color = "green" if rem > 50 else ("yellow" if rem > 20 else "red")
        return f"[{color}]{'#' * filled}{'-' * empty}[/{color}] {rem:.0f}%"

    # Header Panel
    header = Text()
    header.append("Herald Unified Router Mesh\n", style="bold cyan")
    header.append(f"• Total Inferences: {calls:,}  |  ", style="bold white")
    header.append(f"Tokens: {_fmt_tok(tin)} In -> {_fmt_tok(tout)} Out  |  ", style="bold white")
    reporting = totals.get("cost_reporting_backends", 0)
    if reporting:
        header.append(f"Reported provider cost: ${cost:.4f}", style="bold green")
    else:
        header.append("Provider cost: not reported", style="dim")
    console.print(Panel(header, border_style="cyan", box=box.ROUNDED))

    # 1. Routing Pools & Active Backends Table
    backends = payload.get("backends", [])
    b_table = Table(title="[bold cyan]Active Routing Pools & Backends[/bold cyan]", box=box.ROUNDED, header_style="bold cyan")
    b_table.add_column("Backend / Pool", style="bold white")
    b_table.add_column("Type", style="dim")
    b_table.add_column("Calls (Success)", justify="center")
    b_table.add_column("Tokens (In -> Out)", justify="center")
    b_table.add_column("Cost", justify="right", style="green")
    b_table.add_column("Status", justify="center")

    active_backends = [b for b in backends if b.get("enabled", True) or b.get("total_calls", 0) > 0]
    if not all_models:
        # Prioritize backends with traffic or primary pool heads
        filtered = []
        seen_pools = set()
        for b in active_backends:
            if b.get("total_calls", 0) > 0:
                filtered.append(b)
            else:
                p = b.get("pool")
                if p and p not in seen_pools:
                    seen_pools.add(p)
                    filtered.append(b)
                elif not p and len(filtered) < 12:
                    filtered.append(b)
        active_backends = filtered

    for b in active_backends:
        total_c = b.get("total_calls", 0)
        succ_c = b.get("successful_calls", 0)
        if total_c > 0:
            rate = int(round((succ_c / total_c) * 100))
            call_str = f"[green]{succ_c}/{total_c} ({rate}%)[/green]" if rate >= 90 else f"[yellow]{succ_c}/{total_c} ({rate}%)[/yellow]"
        else:
            call_str = "[dim]0 (Ready)[/dim]"

        tin_s = _fmt_tok(b.get("input_tokens"))
        tout_s = _fmt_tok(b.get("output_tokens"))
        tok_str = f"{tin_s} -> {tout_s}" if (tin_s != "-" or tout_s != "-") else "[dim]-[/dim]"
        cost_str = f"${b['cost_usd']:.4f}" if b.get("cost_usd") is not None else "$0.00"

        if b.get("circuit_open"):
            status_str = "[red]Tripped[/red]"
        elif b.get("enabled", True):
            status_str = "[green]Active[/green]" if total_c > 0 else "[blue]Standby[/blue]"
        else:
            status_str = "[dim]Off[/dim]"

        pool_name = b.get("pool")
        name_display = f"{b['name']}\n[dim cyan]pool: {pool_name}[/dim cyan]" if pool_name else b["name"]

        b_table.add_row(
            name_display,
            b.get("type", "-"),
            call_str,
            tok_str,
            cost_str,
            status_str,
        )
    console.print(b_table)

    # 2. Native CLI Subscription Limits Table
    cli_sessions = payload.get("cli_sessions", [])
    if cli_sessions:
        cli_table = Table(title="[bold cyan]Native Subscription Quotas (Claude, Codex, Antigravity)[/bold cyan]", box=box.ROUNDED, header_style="bold cyan")
        cli_table.add_column("Subscription Profile", style="bold white")
        cli_table.add_column("Model Group / Plan", style="dim")
        cli_table.add_column("Window", style="cyan")
        cli_table.add_column("Quota Capacity Remaining", justify="center")
        cli_table.add_column("Resets At", style="yellow")

        for cli in cli_sessions:
            if cli.get("status") == "ok":
                for limit in cli.get("limits", []):
                    rem_pct = limit.get("remaining_percent", 100)
                    bar = _progress(rem_pct, width=12)
                    reset = limit.get("resets_at")
                    try:
                        stamp = datetime.fromtimestamp(reset).astimezone() if isinstance(reset, (int, float)) else datetime.fromisoformat(str(reset)).astimezone()
                        reset_text = stamp.strftime("%a %b %d, %I:%M %p")
                    except (TypeError, ValueError, OSError):
                        reset_text = str(reset or "-")

                    cli_table.add_row(
                        cli["cli"],
                        cli.get("plan_type") or "-",
                        limit.get("name") or "session",
                        bar,
                        reset_text,
                    )
        console.print(cli_table)

    # 3. Free Cloud & Web API Balances Table
    quotas = payload.get("quotas", [])
    if quotas:
        q_table = Table(title="[bold cyan]Free Cloud & Web API Balances[/bold cyan]", box=box.ROUNDED, header_style="bold cyan")
        q_table.add_column("Provider / Pool", style="bold white")
        q_table.add_column("Tier & Window", style="dim")
        q_table.add_column("Used", justify="right")
        q_table.add_column("Remaining Headroom", justify="center")
        q_table.add_column("Resets Schedule", style="yellow")
        q_table.add_column("Status", justify="center")

        for q in quotas:
            rem_pct = q.get("remaining_percent")
            bar = "[blue]unmetered[/blue]" if rem_pct is None else _progress(rem_pct, width=10)
            remaining = q.get("remaining_count")
            rem_count = "Unmetered" if remaining is None else f"{remaining:,} {q.get('unit', '')}"
            used_count = f"{q.get('used_count', 0):,} {q.get('unit', '')}"

            q_table.add_row(
                f"{q['provider']}\n[dim cyan]{q['pool']}[/dim cyan]",
                f"{q.get('tier')}\n[dim]{q.get('window')}[/dim]",
                used_count,
                f"{bar}\n[dim]({rem_count})[/dim]",
                q.get("resets", "-"),
                "[green]Available[/green]" if rem_pct is None or rem_pct > 0 else "[red]Exhausted[/red]",
            )
        console.print(q_table)

    rprint("[dim]Usage shown only for backends and accounts registered to this Router.[/dim]")




@app.command(name="auth")
def auth_status(
    action: str = typer.Argument("status", help="status, capabilities, start, poll, code, cancel, or recheck"),
    cli: Optional[str] = typer.Argument(None, help="CLI profile for lifecycle operations"),
    code: Optional[str] = typer.Option(None, "--code", help="One-time code (write-only; never printed)"),
):
    """Inspect and operate CLI login lifecycles without reading stored secrets."""
    _ensure_router()
    client = _client()
    if action != "status":
        if action == "capabilities":
            result = client._get("/auth/capabilities")
        elif action == "recheck":
            result = client._post("/auth/refresh", {})
        elif action in {"start", "poll", "code", "cancel"}:
            if not cli:
                raise typer.BadParameter(f"{action} requires CLI")
            if action == "start":
                result = client._post("/auth/login/start", {"cli": cli})
            elif action == "poll":
                result = client._get(f"/auth/login/{cli}")
            elif action == "cancel":
                result = client._post(f"/auth/login/{cli}/cancel", {})
            else:
                if not code:
                    raise typer.BadParameter("code requires --code")
                result = client._post("/auth/login/code", {"cli": cli, "code": code})
        else:
            raise typer.BadParameter("action must be status, capabilities, start, poll, code, cancel, or recheck")
        console.print_json(data=result)
        if result.get("error"):
            raise typer.Exit(1)
        return
    auth = client._get("/auth/status").get("clis", [])
    auth_table = Table(title="CLI Auth Profiles", show_header=True, header_style="bold cyan")
    auth_table.add_column("CLI Adapter", style="bold")
    auth_table.add_column("Status")
    auth_table.add_column("Details")
    rows = auth if isinstance(auth, list) else [dict(info, cli=cli) for cli, info in auth.items()]
    for info in rows:
        cli = info.get("cli", "unknown")
        ok = info.get("logged_in") or info.get("status") in {"ok", "logged_in"}
        status_str = "[green][OK] logged in[/green]" if ok else "[red][X] not logged in[/red]"
        detail = info.get("detail") or info.get("user") or info.get("account") or json.dumps(info)
        auth_table.add_row(cli, status_str, str(detail)[:60])
    console.print(auth_table)


# ---------------------------------------------------------------------------
# herald ask & run
# ---------------------------------------------------------------------------

@app.command(name="ask")
@app.command(name="run")
def ask(
    prompt: Optional[str] = typer.Argument(None, help="Prompt to execute (reads stdin if omitted)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model/backend (codex, claude, gemini, g4f-gateway, lmstudio, openrouter, antigravity)"),
    system: Optional[str] = typer.Option(None, "--system", "-s", help="System prompt override"),
    code: bool = typer.Option(False, "--code", help="Prefer code models (claude-cli, codex-cli)"),
    reason: bool = typer.Option(False, "--reason", help="Prefer reasoning models (antigravity, claude-cli, g4f-gateway)"),
    fast: bool = typer.Option(False, "--fast", help="Prefer fast models (gemini-2.5-flash)"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Scoped project name"),
    part: Optional[str] = typer.Option(None, "--part", help="Scoped part name"),
    mode: str = typer.Option(
        "efficiency", "--mode", help="Routing behavior: efficiency, balanced, quality, or local",
    ),
):
    """Execute a prompt through Herald's unified model & tool router."""
    _ensure_router()
    if bool(project) != bool(part):
        rprint("[red][X][/red] --project and --part must be used together")
        raise typer.Exit(2)
    if not prompt:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            rprint("[red][X][/red] Please provide a prompt or pipe input.")
            raise typer.Exit(1)

    task_type = "code" if code else ("reason" if reason else ("fast" if fast else None))
    client = _client()

    if system:
        full_prompt = f"System: {system}\n\nUser: {prompt}"
    else:
        full_prompt = prompt

    with console.status("[bold cyan]Executing through Herald infrastructure...[/bold cyan]"):
        if project and part:
            result = client.chat_scoped(
                full_prompt, project=project, part=part, model=model,
                agentic=True, mode=mode,
            )
        else:
            result = client.chat(
                full_prompt, model=model, task_type=task_type, agentic=True, mode=mode,
            )

    rprint(Markdown(result))


@flow_app.command(name="validate")
def validate_flow_file(path: str = typer.Argument(..., help="Flow YAML file")):
    """Validate topology, agents, rules, memories, and routing mode."""
    from herald.flow import FlowSpec

    _ensure_router()
    try:
        spec = FlowSpec.load(path)
    except (OSError, ValueError) as exc:
        rprint(f"[red][X][/red] {exc}")
        raise typer.Exit(1) from None
    result = _client().validate_flow(spec.to_dict())
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(
        f"[green][OK][/green] [bold]{spec.name}[/bold]: "
        f"{result['agents']} agents, {result['stages']} stages, {spec.mode} mode"
    )


@flow_app.command(name="run")
def run_flow_file(
    path: str = typer.Argument(..., help="Flow YAML file"),
    prompt: Optional[str] = typer.Argument(None, help="Input to the flow (reads stdin if omitted)"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    part: Optional[str] = typer.Option(None, "--part"),
    trace: bool = typer.Option(False, "--trace", help="Print execution trace after output"),
    persist: bool = typer.Option(False, "--persist", help="Encrypt checkpoints and make the run resumable"),
):
    """Run a reusable multi-agent flow through the Herald router."""
    from herald.flow import FlowSpec

    _ensure_router()
    if bool(project) != bool(part):
        raise typer.BadParameter("--project and --part must be used together")
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if not prompt:
        raise typer.BadParameter("provide flow input or pipe it on stdin")
    try:
        spec = FlowSpec.load(path)
    except (OSError, ValueError) as exc:
        rprint(f"[red][X][/red] {exc}")
        raise typer.Exit(1) from None
    client = _client()
    result = (
        client.create_flow_run(spec.to_dict(), prompt, project=project, part=part)
        if persist else client.run_flow(spec.to_dict(), prompt, project=project, part=part)
    )
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    flow_result = result.get("result", result)
    rprint(Markdown(flow_result.get("content", "")))
    if persist and result.get("run"):
        rprint(f"[dim]run: {result['run']['id']} ({result['run']['status']})[/dim]")
    if trace:
        console.print_json(data=flow_result.get("trace", []))


@flow_app.command(name="runs")
def list_persistent_flow_runs(limit: int = typer.Option(20, "--limit")):
    """List encrypted, resumable flow runs."""
    _ensure_router()
    rows = _client().list_flow_runs(limit)
    table = Table(title="Herald Flow Runs", header_style="bold cyan")
    for column in ("ID", "Name", "Mode", "Status", "Stage", "Updated"):
        table.add_column(column)
    for run in rows:
        table.add_row(
            run["id"][:12], run["name"], run["mode"], run["status"],
            f"{run['current_stage']}/{run['total_stages']}", run["updated_at"][:19],
        )
    console.print(table)


@flow_app.command(name="resume")
def resume_persistent_flow(run_id: str = typer.Argument(...)):
    """Resume from the last encrypted stage checkpoint."""
    _ensure_router()
    result = _client().resume_flow_run(run_id)
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(Markdown(result.get("result", {}).get("content", "")))


@capture_app.command(name="accounts")
def capture_accounts():
    """List G4F accounts eligible for protected Kapture refresh."""
    _ensure_router()
    table = Table(title="G4F Capture Accounts", header_style="bold cyan")
    for column in ("Account", "Identity hint", "Enabled", "Session", "Updated"):
        table.add_column(column)
    for account in _client().list_capture_accounts():
        table.add_row(
            account["name"], account.get("email_hint") or "-", str(account["enabled"]),
            "present" if account["session_materialized"] else "missing",
            (account.get("session_updated_at") or "-")[:19],
        )
    console.print(table)


@capture_app.command(name="start")
def capture_start(
    account: str = typer.Argument(...), timeout: float = typer.Option(120, "--timeout"),
    wait: bool = typer.Option(True, "--wait/--no-wait"),
):
    """Watch a real ChatGPT message and securely refresh one G4F account."""
    _ensure_router()
    result = _client().start_g4f_capture(account, timeout=timeout)
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    session = result["session"]
    rprint(result["instruction"])
    rprint(f"[dim]capture: {session['id']}[/dim]")
    if not wait:
        return
    deadline = time.time() + timeout + 10
    with console.status("Watching Kapture for an authenticated ChatGPT request..."):
        while time.time() < deadline:
            snapshot = _client().get_g4f_capture(session["id"])
            session = snapshot.get("session", {})
            if session.get("status") != "watching":
                break
            time.sleep(1)
    if session.get("status") == "materialized":
        rprint("[green][OK][/green] Identity verified; encrypted artifact saved and restricted HAR materialized.")
        rprint("[yellow]Restart the G4F account stack to load the refreshed session.[/yellow]")
    else:
        rprint(f"[red][X][/red] Capture ended as {session.get('status')}: {session.get('error') or ''}")


@capture_app.command(name="list")
def capture_list(limit: int = typer.Option(20, "--limit")):
    _ensure_router()
    console.print_json(data=_client().list_g4f_captures(limit))


@mcp_group_app.command(name="list")
def mcp_group_list():
    """List controlled MCP groups, their tool allowlists, and access bindings."""
    _ensure_router()
    console.print_json(data=_client().list_mcp_groups())


@mcp_group_app.command(name="create")
def mcp_group_create(
    name: str = typer.Argument(...),
    description: str = typer.Option("", "--description", "-d"),
):
    """Create or update a reusable MCP tool group."""
    _ensure_router()
    result = _client().create_mcp_group(name, description)
    if result.get("error"):
        raise typer.BadParameter(str(result["error"]))
    console.print_json(data=result)


@mcp_group_app.command(name="show")
def mcp_group_show(name: str = typer.Argument(...)):
    _ensure_router()
    console.print_json(data=_client().get_mcp_group(name))


@mcp_group_app.command(name="add")
def mcp_group_add(
    group: str = typer.Argument(...), tool: str = typer.Argument(...),
    allow: list[str] = typer.Option(None, "--allow", help="Callable tool name to expose; repeat to form an allowlist"),
    alias: Optional[str] = typer.Option(None, "--alias"),
    position: Optional[int] = typer.Option(None, "--position"),
):
    """Add an MCP server to a group, optionally exposing only selected tools."""
    _ensure_router()
    result = _client().add_mcp_group_tool(
        group, tool, allowed_tools=allow or None, alias=alias, position=position,
    )
    if result.get("error"):
        raise typer.BadParameter(str(result["error"]))
    console.print_json(data=result)


@mcp_group_app.command(name="remove")
def mcp_group_remove(group: str = typer.Argument(...), tool: str = typer.Argument(...)):
    _ensure_router()
    console.print_json(data=_client().remove_mcp_group_tool(group, tool))


@mcp_group_app.command(name="delete")
def mcp_group_delete(
    group: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Delete a group and its bindings without deleting the underlying MCP servers."""
    if not yes and not typer.confirm(f"Delete MCP group {group}?"):
        raise typer.Abort()
    _ensure_router()
    console.print_json(data=_client().delete_mcp_group(group))


@mcp_group_app.command(name="bind")
def mcp_group_bind(
    group: str = typer.Argument(...),
    target_type: str = typer.Argument(..., help="global, project, part, or cli"),
    target: str = typer.Argument("*", help="*, project name, PROJECT/PART, or CLI profile"),
    position: Optional[int] = typer.Option(None, "--position"),
):
    """Assign a group to an access level or named CLI profile."""
    _ensure_router()
    result = _client().bind_mcp_group(group, target_type, target, position)
    if result.get("error"):
        raise typer.BadParameter(str(result["error"]))
    console.print_json(data=result)


@mcp_group_app.command(name="unbind")
def mcp_group_unbind(
    group: str = typer.Argument(...),
    target_type: str = typer.Argument(..., help="global, project, part, or cli"),
    target: str = typer.Argument("*"),
):
    """Remove one access-level binding while keeping the reusable group."""
    _ensure_router()
    console.print_json(data=_client().unbind_mcp_group(group, target_type, target))


@mcp_native_app.command(name="add")
def mcp_native_add(
    kind: str = typer.Argument(..., help="filesystem, shell, or github"),
    group: Optional[str] = typer.Option(None, "--group", help="Also add the server to this controlled group"),
    root: str = typer.Option(".", "--root", help="Allowed filesystem or shell working root"),
    name: Optional[str] = typer.Option(None, "--name"),
    scope: str = typer.Option("global", "--scope"),
    project: Optional[str] = typer.Option(None, "--project"),
    allow: list[str] = typer.Option(None, "--allow", help="Tool allowlist when adding to a group"),
):
    """Register a pinned native integration and optionally place it in a group."""
    from herald.native_integrations import native_integration_spec
    from herald.router.secret_vault import SecretVault

    _ensure_router()
    if scope not in {"global", "project"}:
        raise typer.BadParameter("--scope must be global or project")
    if scope == "project" and not project:
        raise typer.BadParameter("--project is required for project scope")
    try:
        spec = native_integration_spec(kind, root=root)
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from None
    registry_name = name or (spec["name"] if scope == "global" else f"{project}::{spec['name']}")
    if kind == "github" and not os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN"):
        gh = shutil.which("gh")
        if gh:
            token = subprocess.run(
                [gh, "auth", "token"], capture_output=True, text=True, timeout=15,
            )
            if token.returncode == 0 and token.stdout.strip():
                reference = SecretVault().put(
                    "integration/native/github/token", token.stdout.strip().encode(),
                    metadata={"kind": "integration-secret", "source": "gh-auth"},
                )
                spec["config"]["env_refs"] = {"GITHUB_PERSONAL_ACCESS_TOKEN": reference}
    result = _client().register_tool_instance(
        registry_name, spec["transport"], spec["config"],
        description=spec["description"], tags=spec["tags"],
        package_name=spec["package_name"], version=spec["version"],
        scope=scope, project=project, source=spec["source"],
        isolation_key=f"native::{registry_name}",
    )
    if result.get("error"):
        raise typer.BadParameter(str(result["error"]))
    if group:
        existing = _client().get_mcp_group(group)
        if existing.get("error"):
            _client().create_mcp_group(group, f"Controlled tools for {group}")
        result["group"] = _client().add_mcp_group_tool(
            group, registry_name, allowed_tools=allow or None,
        )
    console.print_json(data=result)


@mcp_app.command(name="access")
def mcp_access(
    group: list[str] = typer.Option(None, "--group"),
    project: Optional[str] = typer.Option(None, "--project"),
    part: Optional[str] = typer.Option(None, "--part"),
    profile: Optional[str] = typer.Option(None, "--profile"),
):
    """Preview the exact callable tools an MCP context will receive."""
    _ensure_router()
    console.print_json(data=_client().mcp_access(
        groups=group or None, project=project, part=part, profile=profile,
    ))


@mcp_app.command(name="serve", hidden=True)
def mcp_serve(
    group: list[str] = typer.Option(None, "--group"),
    project: Optional[str] = typer.Option(None, "--project"),
    part: Optional[str] = typer.Option(None, "--part"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    url: str = typer.Option(ROUTER_URL, "--url"),
):
    """Run the controlled stdio MCP gateway for an external CLI."""
    from herald.mcp_gateway import main as gateway_main

    args = ["--url", url]
    for selected in group or []:
        args.extend(["--group", selected])
    if project:
        args.extend(["--project", project])
    if part:
        args.extend(["--part", part])
    if profile:
        args.extend(["--profile", profile])
    gateway_main(args)


@mcp_app.command(name="connect")
def mcp_connect(
    cli: str = typer.Argument(..., help="codex or claude"),
    profile: str = typer.Option(..., "--profile", help="Herald CLI-profile binding to expose"),
    name: str = typer.Option("herald", "--name"),
    scope: str = typer.Option("user", "--scope", help="Claude config scope"),
    force: bool = typer.Option(False, "--force"),
):
    """Connect Codex or Claude Code to Herald's controlled MCP gateway."""
    executable = shutil.which(cli)
    herald_executable = shutil.which("herald")
    if cli not in {"codex", "claude"} or not executable:
        raise typer.BadParameter("cli must be an installed codex or claude command")
    if not herald_executable:
        raise typer.BadParameter("the herald executable is not on PATH")
    check = subprocess.run([executable, "mcp", "get", name], capture_output=True, text=True)
    if check.returncode == 0:
        if not force:
            raise typer.BadParameter(f"MCP connection '{name}' already exists; use --force to replace it")
        subprocess.run([executable, "mcp", "remove", name], check=True)
    command = [
        herald_executable, "mcp", "serve", "--profile", profile,
        "--url", ROUTER_URL,
    ]
    if cli == "codex":
        install = [executable, "mcp", "add", name, "--", *command]
    else:
        install = [executable, "mcp", "add", "--scope", scope, name, "--", *command]
    result = subprocess.run(install)
    if result.returncode:
        raise typer.Exit(result.returncode)
    rprint(f"[green][OK][/green] {cli} now receives Herald MCP profile [bold]{profile}[/bold]")


@integration_app.command(name="list")
def integration_list():
    """Discover MCP connections configured in supported external CLIs."""
    _ensure_router()
    console.print_json(data=_client().discover_integrations())


@integration_app.command(name="import")
def integration_import(
    owner: str = typer.Argument(...), name: str = typer.Argument(...),
    registry_name: Optional[str] = typer.Option(None, "--as"),
    scope: str = typer.Option("global", "--scope"),
    project: Optional[str] = typer.Option(None, "--project"),
):
    """Import an external MCP connection while vaulting its secret fields."""
    _ensure_router()
    result = _client().import_integration(
        owner, name, registry_name=registry_name, scope=scope, project=project,
    )
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Imported [bold]{result['name']}[/bold]")


@hook_app.command(name="list")
def hook_list():
    _ensure_router()
    console.print_json(data=_client().list_hooks())


@hook_app.command(name="add")
def hook_add(
    name: str = typer.Argument(...), pattern: str = typer.Option(..., "--event"),
    url: Optional[str] = typer.Option(None, "--url"),
    command_json: Optional[str] = typer.Option(None, "--command-json"),
    secret_ref: Optional[str] = typer.Option(None, "--secret-ref"),
):
    """Register an HTTP webhook or argv-based local command hook."""
    _ensure_router()
    if bool(url) == bool(command_json):
        raise typer.BadParameter("provide exactly one of --url or --command-json")
    transport = "http" if url else "command"
    config = {"url": url} if url else {"command": json.loads(command_json)}
    result = _client().register_hook(
        name, pattern, transport, config, secret_ref=secret_ref,
    )
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Hook [bold]{name}[/bold] watches {pattern}")


@hook_app.command(name="remove")
def hook_remove(name: str = typer.Argument(...)):
    """Remove an event hook by name."""
    _ensure_router()
    result = _client().remove_hook(name)
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Removed hook [bold]{name}[/bold]")


@app.command(name="events")
def event_list(
    limit: int = typer.Option(30, "--limit"), topic: Optional[str] = typer.Option(None, "--topic"),
):
    """List sanitized router lifecycle events."""
    _ensure_router()
    console.print_json(data=_client().list_events(limit, topic))


# ---------------------------------------------------------------------------
# herald tools & tool run
# ---------------------------------------------------------------------------

@app.command(name="tools")
def list_tools(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project scope"),
    part: Optional[str] = typer.Option(None, "--part", help="Part scope (requires --project)"),
):
    """List all registered tools across Herald (MCP instances, stdio, http, system tools)."""
    _ensure_router()
    client = _client()
    if bool(project) != bool(part):
        rprint("[red][X][/red] --project and --part must be used together")
        raise typer.Exit(2)
    params = {"project": project, "part": part} if project and part else {}
    res = client._get("/tools", **params)
    instances = res.get("tools", [])

    table = Table(title=f"Herald Registered Tools ({len(instances)} instances)", header_style="bold cyan")
    table.add_column("Tool Name", style="bold green")
    table.add_column("Instance", style="cyan")
    table.add_column("Transport", style="magenta")
    table.add_column("Description")
    table.add_column("Tags")

    for ti in instances:
        tags = ", ".join(ti.get("tags") or []) or "-"
        table.add_row(ti["name"], ti.get("instance", ""), ti["transport"], ti.get("description", "")[:60], tags)

    console.print(table)


@tool_app.command(name="run")
def run_tool(
    name: str = typer.Argument(..., help="Tool name to execute"),
    kv_args: list[str] = typer.Argument(None, help="Arguments in key=value format"),
    json_arg: Optional[str] = typer.Option(None, "--json", "-j", help="JSON string of arguments"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project scope"),
    part: Optional[str] = typer.Option(None, "--part", help="Part scope (requires --project)"),
):
    """Execute any registered tool directly from the command line."""
    _ensure_router()
    client = _client()
    if bool(project) != bool(part):
        rprint("[red][X][/red] --project and --part must be used together")
        raise typer.Exit(2)
    arguments: dict[str, Any] = {}
    if json_arg:
        arguments.update(json.loads(json_arg))
    if kv_args:
        for arg in kv_args:
            if "=" in arg:
                k, v = arg.split("=", 1)
                try:
                    arguments[k.strip()] = json.loads(v)
                except json.JSONDecodeError:
                    arguments[k.strip()] = v.strip()

    rprint(f"Executing tool [bold green]{name}[/bold green]...")
    body: dict[str, Any] = {"name": name, "arguments": arguments}
    if project and part:
        body.update({"project": project, "part": part})
    res = client._post("/tools/run", body)
    rprint(res)


@tool_app.command(name="install")
def install_tool(
    source: str = typer.Argument(..., help="npm:PACKAGE, pip:PACKAGE, git:URL, or local:PATH"),
    name: str = typer.Option(..., "--name", "-n", help="Unique installation name"),
    version: str = typer.Option("unversioned", "--version", "-v"),
    binary: Optional[str] = typer.Option(None, "--binary", help="Exported executable name/path"),
    root: str = typer.Option(".", "--root", help="Project root"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Owning project name"),
    part: Optional[str] = typer.Option(None, "--part", help="Part to bind after installation"),
    alias: Optional[str] = typer.Option(None, "--as", help="Part-local installation alias"),
    scope: str = typer.Option("project", "--scope", help="project or global"),
    arg: list[str] = typer.Option(None, "--arg", help="Argument passed to the MCP server"),
):
    """Install an open-source MCP package into an isolated Herald directory."""
    from herald.config import RouterConfig
    from herald.tool_packages import ToolPackageInstaller

    project_root = Path(root).resolve()
    if scope not in {"project", "global"}:
        raise typer.BadParameter("scope must be 'project' or 'global'", param_hint="--scope")
    if scope == "project" and not project:
        manifest = project_root / "router.yaml"
        if not manifest.exists():
            raise typer.BadParameter("--project is required when router.yaml is absent", param_hint="--project")
        project = RouterConfig.load(manifest).project
    tools_root = (
        project_root / ".herald" / "tools"
        if scope == "project" else Path.home() / ".herald" / "tools"
    )
    with console.status(f"Installing {source} in isolation..."):
        installed = ToolPackageInstaller(tools_root).install(
            source, name=name, version=version, binary=binary, args=arg or [],
        )

    _ensure_router()
    client = _client()
    registry_name = name if scope == "global" else f"{project}::{name}"
    if project:
        client.create_project(project)
    result = client.register_tool_instance(
        registry_name, "stdio", {"command": installed.command, "cwd": str(installed.root)},
        package_name=installed.package, version=installed.version, scope=scope,
        project=project if scope == "project" else None, source=installed.source,
        isolation_key=registry_name,
    )
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    if part and project:
        client.create_part(project, part)
        client.bind_tool(project, part, registry_name, alias=alias)
    rprint(f"[green][OK][/green] Installed [bold]{registry_name}[/bold] ({installed.version})")


# ---------------------------------------------------------------------------
# herald account
# ---------------------------------------------------------------------------

@account_app.command(name="list")
def list_accounts(
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
):
    """List every named identity available to Herald routing pools."""
    _ensure_router()
    accounts = _client().list_accounts(provider=provider)
    table = Table(title=f"Herald Accounts ({len(accounts)})", header_style="bold cyan")
    table.add_column("Name", style="bold green")
    table.add_column("Provider", style="cyan")
    table.add_column("Authentication", style="magenta")
    table.add_column("Priority")
    table.add_column("Status")
    table.add_column("Secret source")
    for account in accounts:
        table.add_row(
            account["name"], account["provider"], account["auth_kind"],
            str(account["priority"]), "enabled" if account["enabled"] else "disabled",
            account.get("secret_ref") or "session/profile",
        )
    console.print(table)


@account_app.command(name="add")
def add_account(
    name: str = typer.Argument(..., help="Unique account name, e.g. codex-backup"),
    provider: str = typer.Option(..., "--provider", "-p"),
    auth_kind: str = typer.Option(
        ..., "--kind", help="cli_profile, api_key, browser_session, oauth, or local",
    ),
    config_json: str = typer.Option("{}", "--config", help="Non-secret JSON configuration"),
    secret_ref: Optional[str] = typer.Option(
        None, "--secret-ref", help="env:NAME, keyring:NAME, or vault:NAME",
    ),
    priority: int = typer.Option(100, "--priority"),
    tag: list[str] = typer.Option(None, "--tag"),
):
    """Register a named account without placing secret values in Herald's database."""
    _ensure_router()
    try:
        config = json.loads(config_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    result = _client().register_account(
        name, provider, auth_kind, config=config, secret_ref=secret_ref,
        priority=priority, tags=tag or [],
    )
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Registered account [bold]{name}[/bold]")


@account_app.command(name="lane")
def add_account_lane(
    account: str = typer.Argument(...),
    name: str = typer.Argument(..., help="Lane name, e.g. gemini, claude, or gpt"),
    backend: str = typer.Option(..., "--backend", help="Existing router backend name"),
    model: str = typer.Option("", "--model"),
    capability: list[str] = typer.Option(None, "--capability", help="key=true capability"),
    priority: int = typer.Option(100, "--priority"),
):
    """Attach a callable model lane to an account."""
    _ensure_router()
    capabilities: dict[str, Any] = {}
    for item in capability or []:
        key, separator, value = item.partition("=")
        if not separator:
            raise typer.BadParameter("capabilities use key=value", param_hint="--capability")
        try:
            capabilities[key] = json.loads(value)
        except json.JSONDecodeError:
            capabilities[key] = value
    result = _client().register_account_lane(
        account, name, backend, model=model, capabilities=capabilities, priority=priority,
    )
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Registered [bold]{account}/{name}[/bold] -> {backend}")


@account_app.command(name="activate")
def activate_account(account: str = typer.Argument(...)):
    """Make an API-key account callable while keeping its key behind secret_ref."""
    _ensure_router()
    result = _client().activate_account(account)
    if "error" in result:
        rprint(f"[red][X][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(
        f"[green][OK][/green] Activated [bold]{account}[/bold] as backend "
        f"{result['backend']['name']}"
    )


# ---------------------------------------------------------------------------
# herald clink
# ---------------------------------------------------------------------------

@app.command(name="clink")
def clink_cli(
    cli_name: str = typer.Argument(..., help="CLI tool name (e.g. codex, claude, gemini, antigravity)"),
    prompt: str = typer.Argument(..., help="Prompt to execute via CLI"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Account profile override"),
):
    """Execute a prompt directly through a CLI adapter in Herald's router pool."""
    _ensure_router()
    client = _client()
    body = {"cli_name": cli_name, "prompt": prompt}
    if profile:
        body["env"] = {"CODEX_HOME": f"~/.{profile}"}
    res = client._post("/clink/run", body)
    content = res.get("content") or res.get("error") or str(res)
    rprint(Markdown(content) if isinstance(content, str) else content)


# ---------------------------------------------------------------------------
# herald backends & models
# ---------------------------------------------------------------------------

@app.command(name="models")
@app.command(name="backends")
def backends():
    """List all registered models and backends in Herald."""
    _ensure_router()
    bs = _client().list_backends()
    table = Table(title=f"Registered Backends ({len(bs)} active)", header_style="bold cyan")
    table.add_column("Backend Name", style="bold")
    table.add_column("Type", style="magenta")
    table.add_column("Priority")
    table.add_column("Pool")
    table.add_column("Status")

    for b in bs:
        if b.get("circuit_open"):
            st = "[red]circuit open[/red]"
        elif not b.get("enabled"):
            st = "[dim]disabled[/dim]"
        else:
            st = "[green]ready[/green]"
        table.add_row(b["name"], b["backend_type"], str(b.get("priority", "")), b.get("pool_name") or "-", st)

    console.print(table)


# ---------------------------------------------------------------------------
# herald agent (compatibility entry point for the same environment)
# ---------------------------------------------------------------------------

@app.command(name="agent")
def interactive_agent(
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Default model for agent session"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Scoped project name"),
    part: Optional[str] = typer.Option(None, "--part", help="Scoped part name"),
    system: Optional[str] = typer.Option(
        "You are Herald, an autonomous multi-model AI assistant with full tool and CLI infrastructure capabilities.",
        "--system", "-s"
    ),
):
    """Launch the Herald coding environment (compatibility alias)."""
    from herald.client import DEFAULT_MODEL
    _run_coding_shell(
        path=".", model=model or DEFAULT_MODEL, project=project,
        part=part or "main", prompt=None, system_prompt=system,
        reinstall=False, print_mode=False,
    )


@app.command(name="code")
def coding_shell(
    path: str = typer.Argument(".", help="Workspace directory"),
    model: str = typer.Option("balanced", "--model", "-m", help="Starting Herald model or pool"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    part: str = typer.Option("main", "--part"),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Initial task; omit for interactive mode"),
    print_mode: bool = typer.Option(False, "--print", help="Process the prompt and exit"),
    reinstall: bool = typer.Option(False, "--reinstall-shell", help="Reinstall the pinned terminal runtime"),
    memory_bank: bool = typer.Option(False, "--memory-bank/--no-memory-bank", help="Load canonical memory-bank files from this trusted workspace"),
    no_context_files: bool = typer.Option(False, "--no-context-files", help="Disable native and Memory Bank context files"),
):
    """Launch Herald's full coding environment."""
    _run_coding_shell(
        path=path, model=model, project=project, part=part, prompt=prompt,
        system_prompt=None, reinstall=reinstall, print_mode=print_mode,
        memory_bank=memory_bank, no_context_files=no_context_files,
    )


def _run_coding_shell(
    *, path: str, model: str, project: str | None, part: str,
    prompt: str | None, system_prompt: str | None,
    reinstall: bool, print_mode: bool, memory_bank: bool = False,
    no_context_files: bool = False,
) -> None:
    from herald.config import RouterConfig
    from herald.shell import install_shell, launch_shell

    workspace = Path(path).resolve()
    if not workspace.is_dir():
        raise typer.BadParameter(f"workspace does not exist: {workspace}", param_hint="path")
    manifest = workspace / "router.yaml"
    selected_project = project
    if manifest.exists():
        config = RouterConfig.load(manifest)
        selected_project = selected_project or config.project
        _ensure_router()
        result = config.register(_client())
        if result.get("errors"):
            rprint("[yellow]! Project registration warnings:[/yellow]")
            for error in result["errors"]:
                rprint(f"  [yellow]-[/yellow] {error}")
    else:
        _ensure_router()

    if reinstall:
        with console.status("Installing Herald terminal runtime..."):
            install_shell(force=True)
    try:
        shell_args = ["--print"] if print_mode else []
        if no_context_files:
            shell_args.append("--no-context-files")
        if system_prompt:
            shell_args.extend(["--system-prompt", system_prompt])
        exit_code = launch_shell(
            cwd=workspace, model=model, project=selected_project,
            part=part if selected_project else None, prompt=prompt,
            extra_args=shell_args or None, memory_bank=memory_bank,
        )
    except RuntimeError as exc:
        rprint(f"[red][X][/red] {exc}")
        raise typer.Exit(1) from exc
    raise typer.Exit(exit_code)


@app.command(name="shell-install")
def install_coding_shell(force: bool = typer.Option(False, "--force")):
    """Install Herald's pinned, isolated terminal runtime."""
    from herald.shell import install_shell
    try:
        with console.status("Installing Herald terminal runtime..."):
            runtime = install_shell(force=force)
    except RuntimeError as exc:
        rprint(f"[red][X][/red] {exc}")
        raise typer.Exit(1) from exc
    rprint(f"[green][OK][/green] Herald shell ready at [bold]{runtime.root}[/bold]")


# ---------------------------------------------------------------------------
# herald register & logs
# ---------------------------------------------------------------------------

@app.command()
def register(
    path: str = typer.Argument(".", help="Project directory or path to router.yaml"),
    config_file: str = typer.Option("router.yaml", "--config", "-c"),
):
    """Register a project with Herald from its router.yaml."""
    _ensure_router()
    config_path = Path(path)
    if config_path.is_dir():
        config_path = config_path / config_file

    if not config_path.exists():
        rprint(f"[red][X][/red] Config file not found: {config_path}")
        raise typer.Exit(1)

    from herald.config import RouterConfig
    client = _client()

    rprint(f"Reading [bold]{config_path}[/bold]...")
    cfg = RouterConfig.load(config_path)
    rprint(f"Registering project [bold]{cfg.project}[/bold]...")

    results = cfg.register(client)

    rprint(f"[green][OK][/green] Project: {results['project']}")
    rprint(f"[green][OK][/green] Parts: {', '.join(results['registered_parts']) or '(none)'}")
    rprint(f"[green][OK][/green] Tools: {', '.join(results['registered_tools']) or '(none)'}")
    if results.get("registered_policies"):
        rprint(f"[green][OK][/green] Policies: {', '.join(results['registered_policies'])}")
    if results["errors"]:
        for err in results["errors"]:
            rprint(f"[red][X][/red] {err}")


@app.command()
def logs(
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """Show recent calls routed through Herald."""
    _ensure_router()
    data = _client()._get("/logs/recent", limit=limit)
    calls = data.get("calls", [])

    table = Table(title=f"Recent Herald Calls ({len(calls)})", header_style="bold cyan")
    table.add_column("Time", style="dim")
    table.add_column("Backend", style="bold")
    table.add_column("OK")
    table.add_column("ms")
    table.add_column("Tokens")
    table.add_column("Preview")

    for c in calls:
        ok = "[green][OK][/green]" if c.get("success") else "[red][X][/red]"
        tokens = f"{c.get('input_tokens') or 0}->{c.get('output_tokens') or 0}"
        preview = (c.get("prompt_preview") or "")[:60].replace("\n", " ")
        table.add_row(
            (c.get("timestamp") or "")[:19],
            c.get("backend_name", ""),
            ok,
            str(c.get("duration_ms") or ""),
            tokens,
            preview,
        )

    console.print(table)


@memory_app.command(name="list")
def list_memories(limit: int = typer.Option(50, "--limit")):
    """List named agent memories without exposing their contents."""
    _ensure_router()
    rows = _client().list_agent_sessions(limit)
    table = Table(title="Herald Agent Memories", header_style="bold cyan")
    for label in ("ID", "Agent", "Memory", "Model", "Project / Part", "Turns", "Updated"):
        table.add_column(label)
    for row in rows:
        scope = f"{row.get('project') or '-'} / {row.get('part') or '-'}"
        table.add_row(row["id"][:12], row["name"], row["memory"], row["model"],
                      scope, str(row["turn_count"]), row["updated_at"][:19])
    console.print(table)


@memory_app.command(name="inspect")
def inspect_memory(session_id: str):
    """Show memory metadata, retention, and health without private content."""
    _ensure_router()
    console.print_json(data=_client().inspect_agent_session(session_id))


@memory_app.command(name="reset")
def reset_memory(
    session_id: str,
    force: bool = typer.Option(False, "--force", help="Recover an inaccessible memory by discarding it"),
):
    """Erase turns while retaining the named agent and instructions."""
    _ensure_router()
    console.print_json(data=_client().reset_agent_session(session_id, force=force))


@memory_app.command(name="delete")
def delete_memory(session_id: str, yes: bool = typer.Option(False, "--yes", "-y")):
    """Permanently delete one encrypted memory."""
    if not yes and not typer.confirm(f"Delete agent memory {session_id}?"):
        raise typer.Abort()
    _ensure_router()
    console.print_json(data=_client().delete_agent_session(session_id))


@memory_app.command(name="export")
def export_memory(session_id: str, path: str):
    """Export decrypted memory to an explicitly requested JSON file."""
    _ensure_router()
    target = Path(path).resolve()
    target.write_text(json.dumps(_client().export_agent_session(session_id), indent=2), encoding="utf-8")
    rprint(f"[green][OK][/green] Exported to [bold]{target}[/bold] (contains private memory)")


@memory_app.command(name="import")
def import_memory(session_id: str, path: str):
    """Replace one memory from a prior Herald JSON export."""
    _ensure_router()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    result = _client().import_agent_session(session_id, data)
    if result.get("error"):
        rprint(f"[red][ERROR][/red] {result['error']}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Imported memory into [bold]{session_id}[/bold] from {Path(path).resolve()}")


@connect_app.command(name="ollama")
def cli_connect_ollama(
    url: str = typer.Argument("http://localhost:11434", help="Ollama API base URL"),
    pool: str = typer.Option("local-coder", "--pool", "-p", help="Routing pool name"),
):
    """Auto-discover and register all downloaded models from an Ollama instance."""
    from herald.connectors import connect_ollama
    res = connect_ollama(url=url, pool_name=pool)
    if res.get("status") == "ok":
        rprint(f"[green][OK][/green] Discovered {res.get('count', 0)} model(s) from Ollama at [bold]{url}[/bold]:")
        for m in res.get("registered", []):
            rprint(f"  • {m} [dim](pool: {pool})[/dim]")
    else:
        rprint(f"[red][X][/red] {res.get('error')}")


@connect_app.command(name="lmstudio")
def cli_connect_lmstudio(
    url: str = typer.Argument("http://localhost:1234/v1", help="LM Studio API base URL"),
    pool: str = typer.Option("local-coder", "--pool", "-p", help="Routing pool name"),
):
    """Auto-discover and register currently loaded model in LM Studio."""
    from herald.connectors import connect_lmstudio
    res = connect_lmstudio(url=url, pool_name=pool)
    if res.get("status") == "ok":
        rprint(f"[green][OK][/green] Discovered {res.get('count', 0)} loaded model(s) in LM Studio at [bold]{url}[/bold]:")
        for m in res.get("registered", []):
            rprint(f"  • {m} [dim](pool: {pool})[/dim]")
    else:
        rprint(f"[red][X][/red] {res.get('error')}")


@connect_app.command(name="openrouter")
def cli_connect_openrouter(
    secret_ref: str = typer.Argument(..., help="Secret reference: env:NAME, keyring:NAME, or vault:NAME"),
    models: list[str] = typer.Option(None, "--model", "-m", help="Specific model IDs to register"),
    pool: str = typer.Option("openrouter-pool", "--pool", "-p", help="Routing pool name"),
):
    """Connect OpenRouter API key and register high-performance cloud models."""
    from herald.connectors import connect_openrouter
    res = connect_openrouter(secret_ref=secret_ref, models=models or None, pool_name=pool)
    rprint(f"[green][OK][/green] Registered {res.get('count', 0)} OpenRouter model(s) in pool [bold]{pool}[/bold]:")
    for m in res.get("registered", []):
        rprint(f"  • {m}")


@tool_app.command(name="import-openapi")
def cli_import_openapi(
    url: str = typer.Argument(..., help="OpenAPI / Swagger JSON spec URL (e.g. http://localhost:8000/openapi.json)"),
    base_url: Optional[str] = typer.Option(None, "--base-url", "-b", help="Base URL for executing API requests"),
):
    """Auto-generate AI tools for every endpoint in an OpenAPI / Swagger spec."""
    from herald.openapi_tools import tools_from_openapi
    tools = tools_from_openapi(url, base_url=base_url)
    if tools:
        rprint(f"[green][OK][/green] Auto-generated and registered [bold]{len(tools)}[/bold] tools from OpenAPI spec:")
        for t in tools:
            rprint(f"  • {t}")
    else:
        rprint(f"[red][X][/red] Failed to load OpenAPI spec from {url}")


@app.command(name="init")
def init_project(

    path: str = typer.Argument(".", help="Project directory to initialize"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Herald project name"),
    force: bool = typer.Option(False, "--force", help="Replace an existing router.yaml"),
):
    """Create a project manifest and isolated .herald tool directory."""
    from herald.scaffold import create_project_scaffold

    try:
        files = create_project_scaffold(path, name=name, force=force)
    except FileExistsError as exc:
        rprint(f"[red][X][/red] {exc}")
        raise typer.Exit(1) from None
    rprint(f"[green][OK][/green] Initialized Herald project in [bold]{Path(path).resolve()}[/bold]")
    for file in files:
        rprint(f"  {file}")


@schedule_app.command("add")
def schedule_add(
    name: str = typer.Argument(..., help="Unique name for this schedule"),
    cron: Optional[str] = typer.Option(None, "--cron", help="Cron expression, e.g. '0 8 * * *'"),
    on_event: Optional[str] = typer.Option(None, "--on-event", help="Event type to trigger on, e.g. 'quota.reset'"),
    event_filter: Optional[str] = typer.Option(None, "--filter", help="JSON payload filter for event triggers, e.g. '{\"cli\":\"codex-primary\"}'"),
    project: Optional[str] = typer.Option(None, "--project", help="Project name for an agentic action"),
    part: Optional[str] = typer.Option(None, "--part", help="Part name for an agentic action"),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Prompt to run for an agentic action"),
    disabled: bool = typer.Option(False, "--disabled", help="Create the schedule disabled"),
    model: Optional[str] = typer.Option(None, "--model", help="Pin this schedule to a specific backend name (e.g. 'codex-backup'), bypassing normal routing-policy selection"),
    agentic: bool = typer.Option(True, "--agentic/--no-agentic", help="Whether the agentic action executes real tool calls server-side. NOTE: --no-agentic uses a plain single-turn chat reply with NO tool execution (confirmed empirically) -- it is not a 'cheap version of agentic', just a different, more limited call. For a genuinely cheap frequent check, use --python-target instead."),
    python_target: Optional[str] = typer.Option(None, "--python-target", help="For a 'python' action: dotted path to an allowlisted Python callable (see PYTHON_ACTION_ALLOWLIST in scheduler.py), invoked in-process with no LLM call at all"),
    python_kwargs: Optional[str] = typer.Option(None, "--python-kwargs", help="JSON object of kwargs passed to --python-target's callable"),
):
    """Add a cron- or event-triggered schedule (agentic call, flow, or a
    plain Python callable for a cheap no-LLM check)."""
    import json as _json
    from herald.router.scheduler import ScheduleStore

    if bool(cron) == bool(on_event):
        rprint("[red][X][/red] Provide exactly one of --cron or --on-event")
        raise typer.Exit(1)

    action_type = "python" if python_target else "agentic"
    if action_type == "agentic" and not (project and part and prompt):
        rprint("[red][X][/red] --project, --part, and --prompt are all required for an agentic schedule")
        raise typer.Exit(1)

    store = ScheduleStore()
    try:
        store.add(
            name,
            trigger_type="cron" if cron else "event",
            cron_expression=cron,
            event_type=on_event,
            event_filter=_json.loads(event_filter) if event_filter else None,
            action_type=action_type,
            project=project, part=part, prompt=prompt,
            enabled=not disabled, model=model, agentic=agentic,
            python_target=python_target,
            python_kwargs=_json.loads(python_kwargs) if python_kwargs else None,
        )
    except ValueError as exc:
        rprint(f"[red][X][/red] {exc}")
        raise typer.Exit(1) from None
    rprint(f"[green][OK][/green] Schedule [bold]{name}[/bold] created.")


@schedule_app.command("list")
def schedule_list():
    """List all schedules."""
    from herald.router.scheduler import ScheduleStore

    store = ScheduleStore()
    schedules = store.list_all()
    if not schedules:
        rprint("[dim]No schedules configured.[/dim]")
        return
    for s in schedules:
        status = "[green]enabled[/green]" if s.enabled else "[dim]disabled[/dim]"
        trigger = s.cron_expression if s.trigger_type == "cron" else f"on {s.event_type}"
        model_note = f" (model={s.model})" if s.model else ""
        rprint(f"  [bold]{s.name}[/bold] ({status}) - {s.trigger_type}: {trigger} -> {s.action_type}{model_note} last_status={s.last_status or 'never run'} last_fired={s.last_fired_at or 'never'}")


def _render_trace_step(step: dict) -> str:
    if step.get("kind") == "tool":
        name = step.get("name", "?")
        ok = step.get("ok", step.get("success"))
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        args = step.get("arguments", {})
        return f"    {mark} tool [bold]{name}[/bold]({args})"
    model = step.get("model", "?")
    depth = step.get("depth", 0)
    branch = step.get("branch_id", "-")
    tag = "[dim](final)[/dim]" if step.get("final") else ""
    indent = "  " * (int(depth) + 1)
    return f"{indent}[cyan]{model}[/cyan] branch={branch} depth={depth} {tag}"


@schedule_app.command("log")
def schedule_log(
    name: str = typer.Argument(..., help="Schedule name"),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of most-recent runs to show"),
    trace: bool = typer.Option(False, "--trace", help="Show the full per-model/per-tool-call orchestration trace for each run, not just the final summary"),
):
    """Show the full run history for a schedule -- every fire, its status,
    and a summary of what the action actually did/returned. --trace shows
    the underlying model-delegation and tool-call steps (which backend
    handled it, what tools it called, in what order)."""
    from herald.router.scheduler import ScheduleStore

    store = ScheduleStore()
    if store.get(name) is None:
        rprint(f"[red][X][/red] No schedule named [bold]{name}[/bold]")
        raise typer.Exit(1)
    runs = store.list_runs(name, limit=limit)
    if not runs:
        rprint(f"[dim]No runs recorded yet for {name}.[/dim]")
        return
    for run in runs:
        icon = "[green]OK[/green]" if run["status"] == "ok" else "[red]FAILED[/red]"
        rprint(f"\n[bold]{run['fired_at']}[/bold] {icon}")
        summary = (run.get("summary") or "").strip()
        if summary:
            preview = summary if len(summary) <= 2000 else summary[:2000] + "\n...(truncated)"
            rprint(f"  {preview}")
        if run.get("trigger_context"):
            rprint(f"  [dim]triggered by: {run['trigger_context']}[/dim]")
        run_trace = run.get("trace")
        if trace and run_trace:
            rprint(f"  [dim]-- {len(run_trace)} orchestration step(s) --[/dim]")
            for step in run_trace:
                rprint(f"  {_render_trace_step(step)}")
        elif trace:
            rprint("  [dim](no trace recorded for this run -- non-agentic action, or ran before trace capture was added)[/dim]")


@schedule_app.command("run")
def schedule_run(name: str = typer.Argument(..., help="Schedule name to fire immediately")):
    """Manually fire a schedule's action right now, regardless of its cron/
    event trigger or enabled flag -- the primitive a cheap supervisor
    schedule uses to nudge an expensive worker schedule only when needed,
    instead of the worker firing blindly on every cron tick."""
    from herald.router.scheduler import ScheduleStore, Scheduler

    store = ScheduleStore()
    sched = store.get(name)
    if sched is None:
        rprint(f"[red][X][/red] No schedule named [bold]{name}[/bold]")
        raise typer.Exit(1)
    rprint(f"Firing [bold]{name}[/bold]...")
    Scheduler(store)._fire(sched)
    updated = store.get(name)
    icon = "[green]OK[/green]" if updated.last_status == "ok" else "[red]FAILED[/red]"
    rprint(f"{icon} -- see 'herald schedule log {name}' for the full summary")


@schedule_app.command("watch")
def schedule_watch(
    name: str = typer.Argument(..., help="Schedule name to watch"),
    timeout: int = typer.Option(1800, "--timeout", help="Give up waiting after this many seconds (matches the max agentic-call timeout)"),
):
    """Watch a schedule's currently-running (or next) fire live -- every
    model delegation and tool call as it happens, not just the final
    summary after it's done. Connects to the event-bus SSE stream and
    filters for this schedule's project/part; stops automatically when the
    run completes (a matching schedule.fired/schedule.failed event) or
    after --timeout seconds with nothing happening."""
    from herald.router.scheduler import ScheduleStore

    store = ScheduleStore()
    sched = store.get(name)
    if sched is None:
        rprint(f"[red][X][/red] No schedule named [bold]{name}[/bold]")
        raise typer.Exit(1)

    rprint(f"Watching [bold]{name}[/bold] (project={sched.project}, part={sched.part}) -- Ctrl+C to stop early.\n")
    try:
        with httpx.stream("GET", f"{ROUTER_URL}/event-bus/stream", timeout=timeout) as response:
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                etype = event.get("event_type")
                payload = event.get("payload", {})

                if etype == "agent.step" and payload.get("project") == sched.project and payload.get("part") == sched.part:
                    kind = payload.get("kind", "?")
                    detail = {k: v for k, v in payload.items() if k not in ("project", "part", "kind", "branch_id", "depth")}
                    rprint(f"  [cyan]{kind}[/cyan] {detail}")
                elif etype in ("schedule.fired", "schedule.failed") and payload.get("name") == name:
                    icon = "[green]OK[/green]" if etype == "schedule.fired" else "[red]FAILED[/red]"
                    rprint(f"\n{icon} -- run complete. See 'herald schedule log {name}' for the full summary.")
                    return
    except KeyboardInterrupt:
        rprint("\n[dim]Stopped watching (the run itself is still going).[/dim]")
    except httpx.TimeoutException:
        rprint(f"\n[yellow]No activity within {timeout}s, stopped watching.[/yellow]")


@schedule_app.command("remove")
def schedule_remove(name: str = typer.Argument(...)):
    """Remove a schedule."""
    from herald.router.scheduler import ScheduleStore

    ScheduleStore().remove(name)
    rprint(f"[green][OK][/green] Removed schedule [bold]{name}[/bold].")


@schedule_app.command("enable")
def schedule_enable(name: str = typer.Argument(...)):
    """Enable a schedule."""
    from herald.router.scheduler import ScheduleStore

    ScheduleStore().set_enabled(name, True)
    rprint(f"[green][OK][/green] Enabled [bold]{name}[/bold].")


@schedule_app.command("disable")
def schedule_disable(name: str = typer.Argument(...)):
    """Disable a schedule."""
    from herald.router.scheduler import ScheduleStore

    ScheduleStore().set_enabled(name, False)
    rprint(f"[green][OK][/green] Disabled [bold]{name}[/bold].")


@capability_app.command("list")
def capability_list():
    """List pending self-drafted capability proposals."""
    from herald.router.capability_drafting import get_store

    proposals = get_store().list_pending()
    if not proposals:
        rprint("[dim]No pending capability proposals.[/dim]")
        return
    for p in proposals:
        sandbox = "[green]ok[/green]" if p.sandbox_ok else "[red]failed[/red]"
        risk_color = {"CRITICAL": "red", "HIGH": "red", "MEDIUM": "yellow", "LOW": "green"}.get(p.risk_level, "white")
        rprint(f"  #{p.id} {p.gap_type} - risk=[{risk_color}]{p.risk_level}[/{risk_color}] sandbox={sandbox} created={p.created_at}")


@capability_app.command("show")
def capability_show(proposal_id: int = typer.Argument(...)):
    """Show the full drafted code, sandbox result, and risk scan for one proposal."""
    from herald.router.capability_drafting import get_store

    p = get_store().get(proposal_id)
    if p is None:
        rprint(f"[red][X][/red] No proposal #{proposal_id}")
        raise typer.Exit(1)
    rprint(f"[bold]Proposal #{p.id}[/bold] ({p.status}) - gap: {p.gap_type}")
    rprint(f"  details: {p.gap_details}")
    rprint(f"  risk: {p.risk_level} - findings: {p.risk_findings}")
    rprint(f"  sandbox ok: {p.sandbox_ok}")
    rprint(f"  sandbox output:\n{p.sandbox_output}")
    rprint("  drafted source:")
    rprint(p.draft_source)


@capability_app.command("approve")
def capability_approve(proposal_id: int = typer.Argument(...)):
    """Approve a proposal. Does NOT auto-deploy -- prints the code for you
    to manually wire into coding_tools.py."""
    from herald.router.capability_drafting import get_store

    p = get_store().decide(proposal_id, "approved")
    if p is None:
        rprint(f"[red][X][/red] No pending proposal #{proposal_id}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Approved proposal #{proposal_id}. This is NOT auto-deployed.")
    rprint("Manually review and add this to herald/coding_tools.py:\n")
    rprint(p.draft_source)


@capability_app.command("deny")
def capability_deny(proposal_id: int = typer.Argument(...)):
    """Deny a proposal."""
    from herald.router.capability_drafting import get_store

    p = get_store().decide(proposal_id, "denied")
    if p is None:
        rprint(f"[red][X][/red] No pending proposal #{proposal_id}")
        raise typer.Exit(1)
    rprint(f"[green][OK][/green] Denied proposal #{proposal_id}.")


@history_app.command("search")
def history_search(
    query: str = typer.Argument(..., help="Search query string"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Filter by project"),
    part: Optional[str] = typer.Option(None, "--part", help="Filter by part"),
    since: Optional[str] = typer.Option(None, "--since", help="ISO timestamp start filter"),
    until: Optional[str] = typer.Option(None, "--until", help="ISO timestamp end filter"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max search results"),
):
    """Search stored prompts and responses across agent sessions."""
    _ensure_router()
    results = _client().search_agent_sessions(
        query=query,
        project=project,
        part=part,
        since=since,
        until=until,
        limit=limit,
    )
    if not results:
        rprint(f"[dim]No matching turns found for '{query}'.[/dim]")
        return

    table = Table(title=f"Session History Matches for '{query}' ({len(results)})", header_style="bold cyan")
    table.add_column("Session", style="cyan")
    table.add_column("Turn", style="magenta")
    table.add_column("Scope")
    table.add_column("Input")
    table.add_column("Output Preview")

    for r in results:
        sess_name = r.get("session_name") or r.get("session_id", "")[:8]
        turn_idx = str(r.get("turn_index", 0) + 1)
        scope = f"{r.get('project') or '-'}/{r.get('part') or '-'}"
        in_preview = (r.get("input") or "").replace("\n", " ")[:60]
        out_preview = (r.get("output") or "").replace("\n", " ")[:60]
        table.add_row(sess_name, turn_idx, scope, in_preview, out_preview)

    console.print(table)


@app.command(name="timeline")
def cli_timeline(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of chronological events to display"),
    all_events: bool = typer.Option(False, "--all", "-a", help="Show all historical agent events"),
):
    """Deep inspection timeline: view Admin decisions and Subagent actions chronologically."""
    from datetime import datetime
    from herald.router.scheduler import ScheduleStore
    from herald.router.staging_zone import StagingStore

    def _parse_time(iso_str):
        if not iso_str or iso_str in ("none", "unknown", "never"):
            return datetime.min
        try:
            return datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        except Exception:
            return datetime.min

    events = []
    store = ScheduleStore()

    for s in store.list_all():
        runs = store.list_runs(s.name, limit=50 if all_events else 10)
        for r in runs:
            fired_at = r.get("fired_at")
            events.append({
                "timestamp": fired_at,
                "dt": _parse_time(fired_at),
                "source": s.name,
                "status": r.get("status", "ok"),
                "summary": (r.get("summary") or "").strip(),
            })

    try:
        staging = StagingStore()
        for p in staging.list_patches():
            events.append({
                "timestamp": p.created_at,
                "dt": _parse_time(p.created_at),
                "source": f"staging:{p.agent_name}",
                "status": p.status,
                "summary": f"Patch {p.id[:8]} (task: {p.task_id or 'none'}) touched: {', '.join(p.files_touched)}",
            })
    except Exception:
        pass

    events.sort(key=lambda x: x["dt"], reverse=True)
    display_events = events if all_events else events[:limit]

    if not display_events:
        rprint("[dim]No agent activity recorded yet.[/dim]")
        return

    table = Table(
        title=f"Herald Multi-Agent Activity Timeline ({len(display_events)}/{len(events)} events)",
        header_style="bold cyan",
    )
    table.add_column("Timestamp (UTC)", style="dim", width=22)
    table.add_column("Agent / Loop", style="bold green", width=26)
    table.add_column("Status", width=10)
    table.add_column("Decision / Action Summary")

    for e in display_events:
        ts_str = str(e["timestamp"] or "Unknown Time")[:19].replace("T", " ")
        st = e["status"]
        st_colored = f"[green]{st}[/green]" if st == "ok" or st == "merged" else f"[yellow]{st}[/yellow]"
        summary_line = (e["summary"] or "(no details)").replace("\n", " ")
        if len(summary_line) > 130:
            summary_line = summary_line[:127] + "..."
        table.add_row(ts_str, e["source"], st_colored, summary_line)

    console.print(table)


@app.command(name="swarm")
def cli_swarm(
    checklist: str = typer.Option("TASKS.md", "--checklist", "-c", help="Markdown checklist file path, relative to the detected project root"),
    max_workers: int = typer.Option(6, "--max-workers", "-w", help="Number of parallel agent workers to dispatch"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max tasks to execute in parallel"),
    model: str = typer.Option("codex-backup", "--model", "-m", help="Backend model for swarm workers"),
):
    """Fast-Track Swarm: One-shot massive parallel execution across checklist tasks + automated N-way merge.

    Operates on the Git repository containing your current directory -- run this
    from inside the project you want the swarm to work on.
    """
    from herald.router.fast_track import SwarmOrchestrator, _find_repo_root

    repo_root = _find_repo_root()
    checklist_path = (repo_root / checklist) if not Path(checklist).is_absolute() else Path(checklist)

    rprint(f"[dim]Project: {repo_root}[/dim]")
    if not checklist_path.exists():
        rprint(f"[red][X][/red] Checklist file '{checklist_path}' not found.")
        raise typer.Exit(1)

    rprint(f"[bold cyan]Launching Fast-Track Swarm (max {max_workers} concurrent workers)...[/bold cyan]")
    rprint(f"[dim]Checklist: {checklist_path}[/dim]\n")

    orchestrator = SwarmOrchestrator(repo_path=repo_root, max_parallel=max_workers)

    def _progress(kind: str, msg: str):
        if kind == "started":
            rprint(f"[bold yellow][START][/bold yellow] {msg}")
        elif kind == "worker_done":
            rprint(f"[green][DONE][/green] {msg}")
        elif kind == "merging":
            rprint(f"[bold magenta][MERGE][/bold magenta] {msg}")

    result = orchestrator.run_checklist_swarm(
        checklist_path=checklist_path,
        max_tasks=limit,
        backend_model=model,
        on_progress=_progress,
    )

    rprint(f"\n[bold green][OK] Swarm Execution Complete![/bold green]")
    rprint(f"- Tasks Dispatched: {result.get('tasks_dispatched', 0)}")
    rprint(f"- Synthesis Outcome: {result.get('merge_result', {}).get('status', 'none')}")


if __name__ == "__main__":
    app()
