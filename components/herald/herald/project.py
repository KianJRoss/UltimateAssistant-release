"""Project and Part — Layer 2 of the herald API.

    proj = herald.project("college-assistant")
    agent = proj.part("study-agent")
    result = agent.chat("what's due this week?")
    result = agent.code("refactor this scraper")
    result = agent.agentic("plan and execute my study schedule")
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TYPE_CHECKING, TypeVar

T = TypeVar("T")

if TYPE_CHECKING:
    from herald.client import RouterClient


class Part:
    """A scoped agent within a project. All calls are tool-isolated to
    the tools bound to this project+part combination."""

    def __init__(self, project_name: str, part_name: str, client: "RouterClient") -> None:
        self._project = project_name
        self._part = part_name
        self._client = client

    def chat(self, prompt: str, *, model: str | None = None) -> str:
        """General-purpose scoped chat."""
        return self._client.chat_scoped(
            prompt, project=self._project, part=self._part, model=model
        )

    def code(self, prompt: str, *, file: str | None = None, model: str | None = None) -> str:
        """Code-focused scoped chat. Automatically prefers code models."""
        full_prompt = prompt
        if file:
            try:
                content = open(file).read()
                full_prompt = f"{prompt}\n\n```\n{content}\n```"
            except OSError:
                full_prompt = f"{prompt}\n[file not readable: {file}]"
        return self._client.chat_scoped(
            full_prompt, project=self._project, part=self._part,
            model=model or self._client._resolve_model("code", False),
        )

    def agentic(self, prompt: str, *, model: str | None = None) -> str:
        """Agentic scoped chat — model may delegate to other backends."""
        return self._client.chat_scoped(
            prompt, project=self._project, part=self._part,
            model=model, agentic=True,
        )

    def agentic_with_trace(self, prompt: str, *, model: str | None = None) -> dict[str, Any]:
        """Same as agentic(), but returns the full response dict including
        `orchestration_trace` (per-branch/model/tool-call steps) and
        `orchestration_budget`, instead of just the final text."""
        return self._client.chat_scoped_full(
            prompt, project=self._project, part=self._part,
            model=model, agentic=True,
        )

    def quick(self, prompt: str) -> str:
        """Fast, cheap scoped chat for simple lookups."""
        return self._client.chat_scoped(
            prompt, project=self._project, part=self._part,
            model=self._client._resolve_model("fast", True),
        )

    def _helper_invoke(self, prompt: str, **options: Any):
        return self._client.invoke_scoped(
            prompt, project=self._project, part=self._part, **options,
        )

    def summarize(self, text: str, *, max_words: int | None = None,
                  model: str | None = None, timeout: float | None = None,
                  mode: str = "efficiency") -> str:
        """Scoped form of :func:`herald.summarize`."""
        from herald.helpers import summarize
        return summarize(text, max_words=max_words, model=model, timeout=timeout,
                         mode=mode, _invoke=self._helper_invoke)

    async def asummarize(self, text: str, *, max_words: int | None = None,
                         model: str | None = None, timeout: float | None = None,
                         mode: str = "efficiency") -> str:
        from herald.helpers import asummarize
        return await asummarize(text, max_words=max_words, model=model,
                                timeout=timeout, mode=mode, _invoke=self._helper_invoke)

    def classify(self, text: str, labels: Sequence[str], *,
                 model: str | None = None, timeout: float | None = None,
                 mode: str = "efficiency") -> str:
        """Scoped, constrained classification."""
        from herald.helpers import classify
        return classify(text, labels, model=model, timeout=timeout, mode=mode,
                        _invoke=self._helper_invoke)

    async def aclassify(self, text: str, labels: Sequence[str], *,
                        model: str | None = None, timeout: float | None = None,
                        mode: str = "efficiency") -> str:
        from herald.helpers import aclassify
        return await aclassify(text, labels, model=model, timeout=timeout,
                               mode=mode, _invoke=self._helper_invoke)

    def extract(self, text: str, schema: type[T], *, model: str | None = None,
                timeout: float | None = None, mode: str = "efficiency") -> T:
        """Scoped, schema-validated extraction."""
        from herald.helpers import extract
        return extract(text, schema, model=model, timeout=timeout, mode=mode,
                       _invoke=self._helper_invoke)

    async def aextract(self, text: str, schema: type[T], *,
                       model: str | None = None, timeout: float | None = None,
                       mode: str = "efficiency") -> T:
        from herald.helpers import aextract
        return await aextract(text, schema, model=model, timeout=timeout,
                              mode=mode, _invoke=self._helper_invoke)

    def tools(self) -> list[dict[str, Any]]:
        """List the tool instances bound to this part."""
        return self._client.resolve_scope(self._project, self._part)

    def bind_tool(self, tool: str, *, position: int | None = None) -> "Part":
        """Bind a registered tool instance to this part. Returns self for chaining."""
        self._client.bind_tool(self._project, self._part, tool, position)
        return self

    def __repr__(self) -> str:
        return f"Part({self._project!r}/{self._part!r})"


class Project:
    """A named project in the herald router. Groups parts together.

    Projects auto-register in the router if they don't already exist.

        proj = herald.project("college-assistant")
        proj.part("study-agent").chat("hello")
        proj.from_config("router.yaml")   # register all parts from file
    """

    def __init__(self, name: str, *, client: "RouterClient", description: str = "") -> None:
        self._name = name
        self._client = client
        self._description = description
        self._ensure_registered()

    def _ensure_registered(self) -> None:
        projects = self._client.list_projects()
        existing = {p["name"] for p in projects}
        if self._name not in existing:
            self._client.create_project(self._name, self._description)

    def part(self, name: str, *, description: str = "", auto_create: bool = True) -> Part:
        """Get a Part object. Auto-creates the part in the router if needed."""
        if auto_create:
            parts = self._client.list_parts(self._name)
            existing = {p["name"] for p in parts}
            if name not in existing:
                self._client.create_part(self._name, name, description)
        return Part(self._name, name, self._client)

    def parts(self) -> list[dict[str, Any]]:
        """List all parts in this project with their tool bindings."""
        return self._client.list_parts(self._name)

    def from_config(self, config_path: str = "router.yaml") -> "Project":
        """Register this project's parts and tools from a router.yaml file.

            proj = herald.project("college-assistant")
            proj.from_config("router.yaml")
        """
        from herald.config import RouterConfig
        cfg = RouterConfig.load(config_path)
        cfg.register(self._client)
        return self

    def chat(self, prompt: str, *, part: str, model: str | None = None) -> str:
        """Convenience — chat directly on a project without getting the Part first."""
        return self.part(part).chat(prompt, model=model)

    def delete(self) -> None:
        """Remove this project and all its parts from the router."""
        self._client.delete_project(self._name)

    @property
    def name(self) -> str:
        return self._name

    def __repr__(self) -> str:
        return f"Project({self._name!r})"
