"""Small project-first API for building software on Herald."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from herald.client import RouterClient
from herald.config import RouterConfig


@dataclass(frozen=True)
class Harness:
    """A router client permanently scoped to one project and project part."""

    project: str
    part: str
    root: Path
    client: RouterClient

    @classmethod
    def open(
        cls,
        root: str | Path = ".",
        *,
        part: str | None = None,
        manifest: str = "router.yaml",
        url: str | None = None,
        register: bool = True,
        create: bool = False,
        name: str | None = None,
        auto_start: bool = True,
    ) -> "Harness":
        project_root = Path(root).resolve()
        manifest_path = project_root / manifest
        if create and not manifest_path.exists():
            from herald.scaffold import create_project_scaffold
            create_project_scaffold(project_root, name=name)
        config = RouterConfig.load(manifest_path)
        client = RouterClient(url or os.environ.get("HERALD_URL", "http://127.0.0.1:8790"))
        if auto_start and not client.is_alive():
            if client.base_url not in {"http://localhost:8790", "http://127.0.0.1:8790"}:
                from herald.errors import RouterUnavailableError
                raise RouterUnavailableError(
                    f"Herald router is not reachable at {client.base_url}; remote routers cannot be auto-started"
                )
            from herald.router import start_server
            start_server(background=True)
            for _ in range(30):
                if client.is_alive():
                    break
                time.sleep(0.2)
            else:
                from herald.errors import RouterUnavailableError
                raise RouterUnavailableError("Herald could not start the local router on port 8790")
        if register:
            result = config.register(client)
            if result.get("errors"):
                raise RuntimeError("; ".join(result["errors"]))
        selected_part = part or (config.parts[0].name if config.parts else "main")
        if not config.parts:
            client.create_part(config.project, selected_part, "Default project harness")
        return cls(project=config.project, part=selected_part, root=project_root, client=client)

    def for_part(self, part: str, *, create: bool = False) -> "Harness":
        if create:
            self.client.create_part(self.project, part)
        return replace(self, part=part)

    def ask(
        self, prompt: str, *, model: str | None = None, agentic: bool = True,
        mode: str = "efficiency",
    ) -> str:
        return self.client.chat_scoped(
            prompt, project=self.project, part=self.part, model=model,
            agentic=agentic, mode=mode,
        )

    chat = ask

    def code(self, prompt: str, *, model: str | None = None) -> str:
        selected = model or self.client._resolve_model("code", False)
        return self.ask(prompt, model=selected)

    def reason(self, prompt: str, *, model: str | None = None) -> str:
        selected = model or self.client._resolve_model("reason", False)
        return self.ask(prompt, model=selected)

    def fast(self, prompt: str, *, model: str | None = None) -> str:
        selected = model or self.client._resolve_model("fast", True)
        return self.ask(prompt, model=selected, agentic=False)

    def tools(self) -> list[dict[str, Any]]:
        return self.client.list_tools(project=self.project, part=self.part)

    def tool(self, name: str, **arguments: Any) -> dict[str, Any]:
        return self.client.run_tool(
            name, arguments, project=self.project, part=self.part,
        )

    def agent(
        self,
        name: str,
        *,
        model: str = "auto",
        memory: str | None = None,
        instructions: str = "",
        mode: str = "efficiency",
        agentic: bool = False,
        recent_turns: int = 8,
        ledger_entries: int = 40,
        retention_days: int | None = None,
    ):
        """Open one named model identity with encrypted memory across calls."""
        from herald.agent import StatefulAgent

        result = self.client.open_agent_session(
            name=name,
            memory=memory or name,
            model=model,
            mode=mode,
            instructions=instructions,
            project=self.project,
            part=self.part,
            agentic=agentic,
            recent_turns=recent_turns,
            ledger_entries=ledger_entries,
            retention_days=retention_days,
        )
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        session = result.get("session")
        if not isinstance(session, dict):
            raise RuntimeError("Herald returned an invalid agent session")
        return StatefulAgent(self.client, session)

    def map(
        self, items: list[Any], *, task: str, parallel: int = 4,
        model: str | None = None, mode: str = "efficiency",
    ) -> list[str]:
        """Process independent items concurrently without shared memory."""
        workers = min(max(1, parallel), 32)
        def invoke(item: Any) -> str:
            return self.ask(
                f"{task}\n\nItem:\n{item}", model=model, mode=mode, agentic=False,
            )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(invoke, items))

    def loop(
        self,
        prompt: Any,
        *,
        agent_name: str = "Loop",
        memory: str | None = None,
        model: str = "auto",
        instructions: str = "",
        stop_condition: Any = None,
        max_iterations: int = 50,
        on_step: Any = None,
        sleep_seconds: float = 0.0,
        retry_on_error: int = 2,
        retry_backoff_seconds: float = 5.0,
    ) -> Any:
        """Run a StatefulAgent through repeated steps until stop_condition
        fires, max_iterations is exhausted, or a step errors.

        `prompt` may be a fixed string sent every iteration, or a callable
        taking the step history (list[LoopStep]) and returning the next
        prompt -- useful for polling loops that need to reference the
        previous output.
        """
        from herald.loop import Loop

        worker = self.agent(
            agent_name, model=model, memory=memory, instructions=instructions,
        )
        return Loop(
            worker.ask, prompt=prompt, stop_condition=stop_condition,
            max_iterations=max_iterations, on_step=on_step, sleep_seconds=sleep_seconds,
            retry_on_error=retry_on_error, retry_backoff_seconds=retry_backoff_seconds,
        ).run()

    def run_flow(self, flow: Any, input_text: str, *, persist: bool = False) -> dict[str, Any]:
        """Run a Flow, FlowSpec, dictionary, or YAML file in this project scope."""
        from herald.flow import Flow, FlowSpec

        if isinstance(flow, (str, Path)):
            spec = FlowSpec.load(flow).to_dict()
        elif isinstance(flow, Flow):
            spec = flow.to_dict()
        elif isinstance(flow, FlowSpec):
            spec = flow.to_dict()
        elif isinstance(flow, dict):
            spec = FlowSpec.from_dict(flow).to_dict()
        else:
            raise TypeError("flow must be a Flow, FlowSpec, dictionary, or YAML path")
        method = self.client.create_flow_run if persist else self.client.run_flow
        return method(spec, input_text, project=self.project, part=self.part)
