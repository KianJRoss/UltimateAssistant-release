"""Interactive Setup Wizard for Herald -- foolproof on-device onboarding.

Helps users configure:
1. Deployment Mode (Standalone Local vs Client connecting to Remote Router)
2. Model Providers (Cloud API Keys, Local Ollama / LM Studio, CLI Adapters)
3. Safety Defaults (Quota Reserve Floor, Tool Execution Sandbox Mode)

Must be run from a real interactive terminal, not with redirected/piped
stdin -- confirmed on Windows: the cloud-API-key prompts use Rich's
password=True (Python's getpass under it), and unlike Linux/macOS, Windows'
getpass has no graceful non-console fallback. It blocks forever with no
warning if stdin isn't a real console, instead of the clear
"password may be echoed" warning Linux prints in the same situation.
"""
from __future__ import annotations

import os
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from herald.router.storage_paths import data_dir

console = Console()


def _store_provider_key(name: str, value: str, env_vars: dict[str, str]) -> None:
    """Store a cloud API key in the OS keyring (herald/account_registry.py's
    existing keyring:NAME convention), not as plaintext in the generated
    .env file. Confirmed live: this .env is actually loaded into os.environ
    on Router/CLI startup, so a raw key here is a real plaintext secret on
    disk, not just a theoretical one. Only falls back to plaintext when the
    keyring genuinely isn't usable (e.g. no secret-service backend in a
    minimal container) -- silently losing a key the user just typed would
    be worse than the previous plaintext behavior.
    """
    try:
        import keyring
        keyring.set_password("herald", name, value)
        env_vars.pop(name, None)
        console.print(f"[green][OK][/green] Stored {name} in the system keyring")
    except Exception:  # noqa: BLE001
        console.print(
            f"[yellow]![/yellow] System keyring unavailable; saving {name} to "
            "the config file instead (less secure -- consider a keyring-backed "
            "environment for production use)"
        )
        env_vars[name] = value


def run_setup_wizard() -> None:
    """Run interactive terminal setup."""
    console.print(
        Panel.fit(
            "[bold cyan]Herald Interactive Setup[/bold cyan]\n"
            "[dim]One router for models, tools, agents, and applications[/dim]",
            border_style="cyan",
        )
    )

    # Step 1: Deployment Mode
    console.print("\n[bold yellow]Step 1: Choose Deployment Role[/bold yellow]")
    console.print("  [1] [bold green]Standalone / Local Device[/bold green] (Runs router & models locally on this machine)")
    console.print("  [2] [bold blue]Client Workstation[/bold blue] (Connects this laptop/PC to a remote Herald server)")
    console.print("  [3] [bold magenta]Headless Server Hub[/bold magenta] (Hosts router daemon 24/7 for other devices)")

    choice = Prompt.ask("Select role", choices=["1", "2", "3"], default="1")

    # Honor HERALD_DATA_DIR like every other piece of Herald's persisted
    # state (storage_paths.data_dir()) -- this used to hardcode
    # Path.home()/".herald" directly, so a configured HERALD_DATA_DIR was
    # silently ignored and setup always wrote into the real home directory
    # regardless, confirmed live: setup still wrote to ~/.herald/.env even
    # with HERALD_DATA_DIR pointed elsewhere.
    config_dir = data_dir()
    env_file = config_dir / ".env"

    env_vars: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()

    if choice == "2":
        server_url = Prompt.ask("Enter Remote Herald Router URL", default="http://127.0.0.1:8790")
        env_vars["HERALD_URL"] = server_url.rstrip("/")
        console.print(f"[green][OK][/green] Configured client to connect to [bold]{server_url}[/bold]")
    else:
        env_vars["HERALD_URL"] = "http://127.0.0.1:8790"

    # Step 2: Local LLM Engine Discovery
    if choice in ("1", "3"):
        console.print("\n[bold yellow]Step 2: Local & Cloud Model Connections[/bold yellow]")

        # Check Ollama
        try:
            import httpx
            r = httpx.get("http://localhost:11434/api/tags", timeout=1.5)
            if r.status_code == 200:
                console.print("[green][OK][/green] Detected running [bold]Ollama[/bold] on localhost:11434")
        except Exception:
            pass

        # Check LM Studio
        try:
            import httpx
            r = httpx.get("http://localhost:1234/v1/models", timeout=1.5)
            if r.status_code == 200:
                console.print("[green][OK][/green] Detected running [bold]LM Studio[/bold] on localhost:1234")
        except Exception:
            pass

        # Ask for optional cloud API keys
        if Confirm.ask("Would you like to configure Cloud API keys now?", default=False):
            for label, name in (
                ("Google Gemini", "GEMINI_API_KEY"),
                ("OpenAI", "OPENAI_API_KEY"),
                ("Anthropic", "ANTHROPIC_API_KEY"),
                ("OpenRouter", "OPENROUTER_API_KEY"),
            ):
                value = Prompt.ask(f"{label} API Key (press Enter to skip)", default="", password=True)
                if value:
                    _store_provider_key(name, value, env_vars)

    # Step 3: Safety & Budget Preferences
    console.print("\n[bold yellow]Step 3: Safety & Budget Preferences[/bold yellow]")
    reserve_pct = Prompt.ask("Quota Reserve Floor % (Protects balance for personal/trading use)", default="30")
    env_vars["HERALD_USAGE_RESERVE_PERCENT"] = str(reserve_pct)

    auto_update = Confirm.ask("Enable automatic background updates? (Default: No, opt-in only)", default=False)
    env_vars["HERALD_AUTO_UPDATE"] = "1" if auto_update else "0"

    # Write .env
    lines = [f"{k}={v}" for k, v in env_vars.items()]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green][OK][/green] Saved configuration to [bold]{env_file}[/bold]")

    console.print(
        Panel.fit(
            "[bold green]Setup Complete![/bold green]\n\n"
            "• Run [bold cyan]herald status[/bold cyan] to check backend connections\n"
            "• Run [bold cyan]herald code[/bold cyan] to launch the interactive coding environment\n"
            "• Run [bold cyan]herald dashboard[/bold cyan] to open the Account Console web UI",
            border_style="green",
        )
    )
