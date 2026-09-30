"""router.yaml config loader and project auto-registration.

Loads a project's router.yaml and registers everything with the running
herald router — projects, parts, tool instances, and backend allowlists.

Usage:
    herald register .             # CLI: register current directory
    herald register path/to/proj  # CLI: register another project

    # Or programmatically:
    from herald.config import RouterConfig
    cfg = RouterConfig.load("router.yaml")
    cfg.register(client)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from herald.client import RouterClient


@dataclass
class ToolInstanceConfig:
    name: str
    transport: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    shared: bool = False          # if True, don't re-register — just bind
    # transport-specific
    url: str = ""                 # http / sse
    command: list[str] = field(default_factory=list)  # stdio
    env: dict[str, str] = field(default_factory=dict)  # stdio
    cwd: str = ""
    package_name: str = ""
    version: str = "unversioned"
    scope: str = "project"
    source: dict[str, Any] = field(default_factory=dict)
    alias: str = ""
    extra_config: dict[str, Any] = field(default_factory=dict)

    def to_config_dict(self) -> dict[str, Any]:
        if self.transport in ("http", "sse"):
            return {"url": self.url, **self.extra_config}
        if self.transport == "stdio":
            d: dict[str, Any] = {"command": self.command}
            if self.env:
                d["env"] = self.env
            if self.cwd:
                d["cwd"] = self.cwd
            d.update(self.extra_config)
            return d
        return {}


@dataclass
class PartConfig:
    name: str
    description: str = ""
    tools: list[ToolInstanceConfig] = field(default_factory=list)
    routing: dict[str, Any] = field(default_factory=dict)


@dataclass
class CustomPolicyConfig:
    name: str
    automatic_order: list[str]
    delegate_types: list[str]
    description: str = ""
    free_only: bool = False
    compact_tool_catalog: bool = False
    tool_bridge: str | None = None


@dataclass
class LocalModelConfig:
    model_name: str
    idle_unload_minutes: float | None = None  # 0/null = never auto-unload


@dataclass
class ConnectorConfig:
    """Declarative connector manifest entry -- 'drop in a manifest, get a
    new backend', applying the same pattern router.yaml already uses for
    tools to external model endpoints. type must match a known auto-discovery
    function in herald/connectors.py (ollama, lmstudio, openrouter,
    openai-compatible); the connector's base_url is used directly instead of
    that function's assumed default."""
    name: str
    type: str
    base_url: str
    auth: str = "none"  # env var name to read a key from, or "none"
    capability_tags: list[str] = field(default_factory=list)


@dataclass
class RouterConfig:
    project: str
    description: str = ""
    parts: list[PartConfig] = field(default_factory=list)
    project_tools: list[ToolInstanceConfig] = field(default_factory=list)
    global_tools: list[ToolInstanceConfig] = field(default_factory=list)
    custom_policies: list[CustomPolicyConfig] = field(default_factory=list)
    local_models: list[LocalModelConfig] = field(default_factory=list)
    connectors: list[ConnectorConfig] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path = "router.yaml") -> "RouterConfig":
        """Load and parse a router.yaml file."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"router.yaml not found at {p.resolve()}")

        with open(p) as f:
            data = yaml.safe_load(f)

        return cls._parse(data)

    @classmethod
    def _parse(cls, data: dict[str, Any]) -> "RouterConfig":
        parts = []
        for part_name, part_data in (data.get("parts") or {}).items():
            tools = []
            for tool_data in (part_data.get("tools") or []):
                tools.append(cls._parse_tool(tool_data, default_scope="project"))
            parts.append(PartConfig(
                name=part_name,
                description=part_data.get("description", ""),
                tools=tools,
                routing=part_data.get("routing") or {},
            ))

        project_tools = []
        for tool_data in (data.get("tools") or []):
            project_tools.append(cls._parse_tool(tool_data, default_scope="project"))

        global_tools = []
        for tool_data in (data.get("global_tools") or []):
            global_tools.append(cls._parse_tool(tool_data, default_scope="global"))

        custom_policies = []
        for name, policy_data in (data.get("custom_policies") or {}).items():
            custom_policies.append(CustomPolicyConfig(
                name=name,
                automatic_order=list(policy_data.get("automatic_order") or []),
                delegate_types=list(policy_data.get("delegate_types") or []),
                description=policy_data.get("description", ""),
                free_only=bool(policy_data.get("free_only", False)),
                compact_tool_catalog=bool(policy_data.get("compact_tool_catalog", False)),
                tool_bridge=policy_data.get("tool_bridge"),
            ))

        local_models = []
        for model_name, model_data in (data.get("local_models") or {}).items():
            local_models.append(LocalModelConfig(
                model_name=model_name,
                idle_unload_minutes=(model_data or {}).get("idle_unload_minutes"),
            ))

        connectors = []
        for name, conn_data in (data.get("connectors") or {}).items():
            conn_data = conn_data or {}
            connectors.append(ConnectorConfig(
                name=name,
                type=conn_data.get("type", "openai-compatible"),
                base_url=conn_data.get("base_url", ""),
                auth=conn_data.get("auth", "none"),
                capability_tags=list(conn_data.get("capability_tags") or []),
            ))

        return cls(
            project=data["project"],
            description=data.get("description", ""),
            parts=parts,
            project_tools=project_tools,
            global_tools=global_tools,
            custom_policies=custom_policies,
            local_models=local_models,
            connectors=connectors,
        )

    @staticmethod
    def _parse_tool(d: dict[str, Any] | str, default_scope: str = "project") -> ToolInstanceConfig:
        if isinstance(d, str):
            d = {"use": d}
        reference = "use" in d
        transport = d.get("transport", "")

        # Infer transport from keys if not explicit
        if not transport:
            if "url" in d:
                transport = "http"
            elif "command" in d:
                transport = "stdio"
            else:
                transport = "reference" if reference else "http"

        known = {
            "name", "use", "transport", "description", "tags", "shared", "url",
            "command", "env", "cwd", "package", "version", "scope", "source", "as",
        }

        return ToolInstanceConfig(
            name=d.get("name") or d["use"],
            transport=transport,
            description=d.get("description", ""),
            tags=d.get("tags") or [],
            shared=d.get("shared", reference),
            url=d.get("url", ""),
            command=d.get("command") or [],
            env=d.get("env") or {},
            cwd=d.get("cwd", ""),
            package_name=d.get("package") or d.get("name") or d.get("use", ""),
            version=str(d.get("version", "unversioned")),
            scope=d.get("scope", default_scope),
            source=d.get("source") or {},
            alias=d.get("as", ""),
            extra_config={key: value for key, value in d.items() if key not in known},
        )

    def _registry_name(self, tool: ToolInstanceConfig) -> str:
        if tool.scope == "global":
            return tool.name
        return f"{self.project}::{tool.name}"

    def _register_tool(self, client: "RouterClient", tool: ToolInstanceConfig) -> dict[str, Any]:
        return client.register_tool_instance(
            self._registry_name(tool), tool.transport, tool.to_config_dict(),
            description=tool.description, tags=tool.tags,
            package_name=tool.package_name, version=tool.version,
            scope=tool.scope, project=self.project if tool.scope == "project" else None,
            source=tool.source, isolation_key=self._registry_name(tool),
        )

    def register(self, client: "RouterClient") -> dict[str, Any]:
        """Push this config to the router. Idempotent — safe to call on every startup."""
        results: dict[str, Any] = {
            "project": self.project,
            "registered_parts": [],
            "registered_tools": [],
            "registered_policies": [],
            "errors": [],
        }

        # 1. Ensure project exists
        client.create_project(self.project, self.description)

        # 1b. Register any custom routing policies declared for this project.
        for policy in self.custom_policies:
            try:
                client.register_routing_policy(
                    policy.name, automatic_order=policy.automatic_order,
                    delegate_types=policy.delegate_types, description=policy.description,
                    free_only=policy.free_only, compact_tool_catalog=policy.compact_tool_catalog,
                    tool_bridge=policy.tool_bridge, project=self.project,
                )
                results["registered_policies"].append(policy.name)
            except Exception as exc:  # noqa: BLE001 -- one bad policy shouldn't abort registration
                results["errors"].append(f"policy '{policy.name}': {exc}")

        # 1c. Register per-model idle-unload thresholds.
        results.setdefault("registered_local_models", [])
        for lm in self.local_models:
            try:
                client.register_idle_unload_config(lm.model_name, lm.idle_unload_minutes)
                results["registered_local_models"].append(lm.model_name)
            except Exception as exc:  # noqa: BLE001 -- one bad entry shouldn't abort registration
                results["errors"].append(f"local_model '{lm.model_name}': {exc}")

        # 1d. Register declared connectors -- external model endpoints
        # dropped in via manifest instead of editing herald/connectors.py.
        results.setdefault("registered_connectors", [])
        for conn in self.connectors:
            try:
                client.register_connector(
                    conn.name, type=conn.type, base_url=conn.base_url,
                    auth=conn.auth, capability_tags=conn.capability_tags,
                )
                results["registered_connectors"].append(conn.name)
            except Exception as exc:  # noqa: BLE001 -- one bad connector shouldn't abort registration
                results["errors"].append(f"connector '{conn.name}': {exc}")

        # 2. Register machine-global and project-owned installations.
        for ti in [*self.global_tools, *self.project_tools]:
            if not ti.shared:
                r = self._register_tool(client, ti)
                if "error" in r:
                    results["errors"].append(f"tool '{ti.name}': {r['error']}")
                else:
                    results["registered_tools"].append(self._registry_name(ti))

        # 3. Register each part and its tool instances
        for part in self.parts:
            client.create_part(self.project, part.name, part.description)
            tool_names = []

            for ti in part.tools:
                # If shared=True, the tool instance is already in the router
                # (registered globally or by another project). Just bind it.
                if not ti.shared:
                    r = self._register_tool(client, ti)
                    if "error" in r:
                        results["errors"].append(f"part '{part.name}' tool '{ti.name}': {r['error']}")
                        continue
                    results["registered_tools"].append(self._registry_name(ti))

                tool_names.append(self._registry_name(ti))

            # Set the ordered tool list for this part
            if tool_names:
                client.set_part_tools(self.project, part.name, tool_names)
                for position, ti in enumerate(part.tools):
                    if ti.alias:
                        client.bind_tool(
                            self.project, part.name, self._registry_name(ti),
                            position=position, alias=ti.alias,
                        )

            results["registered_parts"].append(part.name)

        return results
