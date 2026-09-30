"""ASCII & Rich Graph Visualizers for Herald CLI.

Renders architecture flowcharts, mesh topology diagrams, and interactive routing trees.
"""
from __future__ import annotations

from typing import Any
from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree
from rich.text import Text

console = Console()


def _render_configured_mesh_graph(status_data: dict[str, Any]) -> None:
    """Render the connected Router inventory without built-in model labels."""
    backends = list(status_data.get("backends") or [])
    console.print(Panel(
        Align.center("[bold white]Herald CLI  •  Pi agents  •  Python / TypeScript[/bold white]"),
        title="[bold cyan]Client & Agent Surfaces[/bold cyan]",
        border_style="cyan",
    ))
    console.print(Align.center("[bold cyan]▼[/bold cyan]"))
    console.print(Panel(
        Align.center("Routing  •  policy  •  health  •  usage  •  tools"),
        title="[bold blue]Herald Router[/bold blue]",
        border_style="blue",
    ))
    console.print(Align.center("[bold blue]▼[/bold blue]"))

    if not backends:
        console.print(Panel(
            "No models or CLI accounts are registered yet.\n"
            "Run [bold cyan]herald setup[/bold cyan] or open "
            "[bold cyan]herald dashboard[/bold cyan] to connect one.",
            title="[yellow]No connected routes[/yellow]",
            border_style="yellow",
        ))
        return

    pools: dict[str, list[dict[str, Any]]] = {}
    for backend in backends:
        pool = str(backend.get("pool_name") or backend.get("pool") or "Unpooled")
        pools.setdefault(pool, []).append(backend)

    panels = []
    for pool, members in sorted(pools.items(), key=lambda item: item[0].casefold()):
        lines = []
        for backend in sorted(members, key=lambda row: str(row.get("name", "")).casefold()):
            name = escape(str(backend.get("name") or "Unnamed route"))
            kind = escape(str(backend.get("backend_type") or backend.get("type") or "model"))
            enabled = backend.get("enabled", True)
            state = "[green]active[/green]" if enabled else "[dim]disabled[/dim]"
            if backend.get("circuit_open"):
                state = "[red]circuit open[/red]"
            lines.append(f"[bold]{name}[/bold]\n[dim]{kind}[/dim]  {state}")
        panels.append(Panel(
            "\n\n".join(lines),
            title=f"[bold]{escape(pool)}[/bold]",
            border_style="green" if any(row.get("enabled", True) for row in members) else "dim",
            padding=(1, 2),
        ))
    console.print(Columns(panels, equal=True, expand=True))


def render_mesh_graph(status_data: dict[str, Any] | None = None) -> None:
    """Render the full Unicode architecture flowchart of the Herald Mesh."""
    if status_data is not None:
        _render_configured_mesh_graph(status_data)
        return
    diag = """[bold cyan]┌─────────────────────────────────────────────────────────────────────────────┐[/bold cyan]
[bold cyan]│[/bold cyan]                       [bold white]HERALD CLIENT & SDK SURFACES[/bold white]                         [bold cyan]│[/bold cyan]
[bold cyan]│[/bold cyan]  [green]• import herald[/green]     [yellow]• herald CLI (ask/run)[/yellow]     [magenta]• OpenAI/Anthropic API Gateway[/magenta]   [bold cyan]│[/bold cyan]
[bold cyan]└──────────────────────────────────────┬──────────────────────────────────────┘[/bold cyan]
                                       │
                                       ▼
[bold blue]┌─────────────────────────────────────────────────────────────────────────────┐[/bold blue]
[bold blue]│[/bold blue]                     [bold white]PREDICTIVE MESH ROUTER & HARNESS[/bold white]                        [bold blue]│[/bold blue]
[bold blue]│[/bold blue]  [cyan]• Quota Ranker[/cyan]   [green]• Circuit Breaker[/green]   [yellow]• MCP Tool Engine[/yellow]   [magenta]• Persona Steering[/magenta]  [bold blue]│[/bold blue]
[bold blue]└──────────────────┬───────────────────┬───────────────────┬──────────────────┘[/bold blue]
                   │                   │                   │
         ┌─────────┴─────────┐         │         ┌─────────┴─────────┐
         ▼                   ▼         │         ▼                   ▼
┌──────────────────┐┌──────────────────┐┌──────────────────┐┌──────────────────┐
│   [bold green]LOCAL GPU[/bold green]      ││   [bold cyan]FREE CLOUD[/bold cyan]     ││  [bold yellow]G4F WEB SESSIONS[/bold yellow]││   [bold magenta]SUBSCRIPTION[/bold magenta]   │
│   [dim]local-coder[/dim]    ││   [dim]free-cloud[/dim]     ││  [dim]g4f-gateway[/dim]    ││   [dim]codex/claude/agy[/dim]│
├──────────────────┤├──────────────────┤├──────────────────┤├──────────────────┤
│• RTX 4070 Ti S   ││• Google Gemini 2 ││• ChatGPT Plus Web││• Codex Primary   │
│• Qwen 2.5 Coder  ││• Gemma 4 31B     ││• Gemini Pro Web  ││• Codex Backup    │
│• $0.00 / 24/7    ││• 1,500/day RPD   ││• 160/3h Window   ││• Claude Pro      │
│• Infinite Quota  ││• Zero Latency    ││• Self-Healing    ││• Antigravity Pro │
└──────────────────┘└──────────────────┘└──────────────────┘└──────────────────┘"""
    console.print(Panel(diag, title="[bold white]Herald AI Infrastructure & Routing Topology[/bold white]", border_style="cyan", padding=(1, 2)))


def render_mesh_tree(backends: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, personas: list[str] | None = None) -> None:
    """Render an interactive Rich Tree of all connected pools, models, and tools."""
    tree = Tree("[bold cyan]🌐 Herald Mesh Network[/bold cyan]")

    # 1. Routing Pools
    pools_tree = tree.add("[bold green]🔀 Active Routing Pools & Backends[/bold green]")
    pools: dict[str, list[dict[str, Any]]] = {}
    for b in backends:
        p = b.get("pool_name") or "unpooled"
        if p not in pools:
            pools[p] = []
        pools[p].append(b)

    for pool_name, members in pools.items():
        pool_node = pools_tree.add(f"[bold yellow]{pool_name}[/bold yellow] [dim]({len(members)} backends)[/dim]")
        for m in members:
            status = "[green][Active][/green]" if m.get("enabled") else "[dim][Disabled][/dim]"
            b_type = m.get("backend_type", "api")
            prio = m.get("priority", 100)
            pool_node.add(f"[white]{m.get('name')}[/white] [dim]({b_type} | prio: {prio})[/dim] {status}")

    # 2. Tools
    if tools:
        tools_tree = tree.add(f"[bold magenta]🛠️ Registered Tools & MCP Servers ({len(tools)})[/bold magenta]")
        for t in tools:
            name = t.get("name", "unknown")
            desc = t.get("description", "")
            tools_tree.add(f"[white]{name}[/white]: [dim]{desc[:60]}...[/dim]" if len(desc) > 60 else f"[white]{name}[/white]: [dim]{desc}[/dim]")

    # 3. Personas
    if personas:
        personas_tree = tree.add(f"[bold blue]🎭 Steering Personas ({len(personas)})[/bold blue]")
        for p in personas:
            personas_tree.add(f"[white]{p}[/white]")

    console.print(tree)
