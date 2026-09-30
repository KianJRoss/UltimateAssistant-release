"""RouterClient — HTTP wrapper around the herald router.

This is what every layer of the API ultimately calls. It handles:
  - Connection to the router (auto-detect, explicit URL, or env var)
  - Simple chat, scoped chat, agentic chat
  - Backend CRUD
  - Project/part/tool management
  - Status and telemetry reads
  - Task-type routing hints (code, fast, reason)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
import asyncio
from typing import Any

import httpx


# Task-type hints → which backend capabilities to prefer.
# The router uses these when no explicit model is given.
_TASK_BACKEND_HINTS = {
    "code":   {"prefer_capabilities": ["code"], "prefer_names": ["claude-cli", "codex-cli"]},
    "reason": {"prefer_capabilities": ["reasoning"], "prefer_names": ["antigravity", "claude-cli"]},
    "fast":   {"prefer_capabilities": ["fast"], "prefer_names": ["gemini-2.5-flash"]},
}

# The backend the router will use when no model and no task_type are given.
# Projects can override this per-part via router.yaml.
DEFAULT_MODEL = os.environ.get("HERALD_DEFAULT_MODEL", "balanced")


@dataclass(frozen=True)
class InvocationResult:
    """Machine-readable outcome for package features built on inference."""

    ok: bool
    content: str = ""
    error_kind: str | None = None
    error: str | None = None
    """Human-readable detail (e.g. "no healthy backend satisfies the
    'efficiency' routing policy"), when the Router provided one. May be
    None even when ok is False -- error_kind is the field guaranteed to be
    set on failure."""


@dataclass(frozen=True)
class CLIAccount:
    """A credential-free handle to a named CLI account on a router."""

    name: str
    _client: "RouterClient"

    def __call__(
        self, prompt: str, *, timeout: float | None = None,
        agentic: bool = False, mode: str = "efficiency",
    ) -> str:
        return self._client.invoke_cli_account(
            self.name, prompt, timeout=timeout, agentic=agentic, mode=mode,
        )

    async def acall(
        self, prompt: str, *, timeout: float | None = None,
        agentic: bool = False, mode: str = "efficiency",
    ) -> str:
        """Run the synchronous HTTP client without blocking the event loop."""
        return await asyncio.to_thread(
            self, prompt, timeout=timeout, agentic=agentic, mode=mode,
        )


class RouterClient:
    """HTTP client for the herald router.

    Instantiated by herald.__init__ and cached process-wide. Projects
    that want a separate client (e.g. different URL) can instantiate
    directly:

        from herald import Router
        r = Router.connect("http://other-host:8790")
        r.chat("hello")
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8790", timeout: float = 1800.0) -> None:
        # Raised from 120.0: every caller (herald ask/code/agent, and the
        # RouterClient() calls in herald/__init__.py and cli/main.py) uses
        # this default and never overrides it. A real agentic coding task
        # routed through a CLI backend can legitimately run long -- the
        # router's own internal CLI-call timeout was raised to 1800s for
        # exactly this reason, but this client-side HTTP timeout, one hop
        # further out, was still cutting the whole request off at 120s
        # regardless. That made this the most common way to actually hit
        # the "task gets killed before it can finish" failure.
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        key = os.environ.get("HERALD_API_KEY")
        return {"Authorization": f"Bearer {key}"} if key else {}

    @classmethod
    def connect(cls, url: str) -> "RouterClient":
        return cls(base_url=url)

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            r = httpx.get(f"{self.base_url}{path}", params=params, headers=self._auth_headers(), timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except httpx.ConnectError:
            return {"error": f"Herald router not reachable at {self.base_url}. Run: herald start"}
        except httpx.TimeoutException:
            return {"error": "Herald router request timed out"}
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            return {"error": detail or f"Herald router returned HTTP {exc.response.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def _post(self, path: str, body: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        try:
            r = httpx.post(
                f"{self.base_url}{path}", json=body, headers=self._auth_headers(),
                timeout=timeout or self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except httpx.ConnectError:
            return {"error": f"Herald router not reachable at {self.base_url}. Run: herald start"}
        except httpx.TimeoutException:
            return {
                "error": "Herald router request timed out",
                "error_kind": "timeout",
            }
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            return {"error": detail or f"Herald router returned HTTP {exc.response.status_code}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def _delete(self, path: str) -> dict[str, Any]:
        try:
            r = httpx.delete(f"{self.base_url}{path}", headers=self._auth_headers(), timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def _put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = httpx.put(f"{self.base_url}{path}", json=body, headers=self._auth_headers(), timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Core inference
    # ------------------------------------------------------------------

    def chat(
        self,
        prompt: str,
        *,
        model: str | None = None,
        task_type: str | None = None,
        prefer_fast: bool = False,
        agentic: bool = False,
        mode: str = "efficiency",
    ) -> str:
        """Route a prompt. Returns text response or '[error] ...' string."""
        _, content = self.chat_with_status(
            prompt, model=model, task_type=task_type,
            prefer_fast=prefer_fast, agentic=agentic, mode=mode,
        )
        return content

    def chat_with_status(
        self,
        prompt: str,
        *,
        model: str | None = None,
        task_type: str | None = None,
        prefer_fast: bool = False,
        agentic: bool = False,
        mode: str = "efficiency",
        consult_depth: int = 0,
        timeout: float | None = None,
        cli_account: str | None = None,
    ) -> tuple[bool, str]:
        """Same call as chat(), but returns a real (ok, content) signal.

        chat()'s "[error] ..." string prefix is a convenience for a human
        reading output directly, not a reliable success/failure signal for
        code -- a legitimate model response that happens to start with
        those exact characters would be indistinguishable from a real
        failure. Chain.chat() and chat_chain() need the real signal, so
        this is what they call instead of string-sniffing chat()'s output.

        `consult_depth`: forwarded to the router so a chain of
        consult_models delegations (herald/consult_tools.py) can enforce a
        max recursion depth -- each delegated call passes its own depth+1
        forward since there's no shared in-memory state across the separate
        processes/requests involved.
        """
        effective_model = model or self._resolve_model(task_type, prefer_fast)
        body: dict[str, Any] = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "mode": mode,
        }
        if agentic:
            body["agentic"] = True
        if consult_depth:
            body["consult_depth"] = consult_depth
        if cli_account:
            body["cli_account"] = cli_account
        result = self._post("/v1/chat/completions", body, timeout=timeout)
        if "error" in result:
            return False, f"[error] {result['error']}"
        try:
            return True, result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return False, "[error] Herald router returned a malformed chat response"

    def invoke(
        self, prompt: str, *, model: str | None = None,
        timeout: float | None = None, mode: str = "efficiency",
    ) -> InvocationResult:
        """Invoke inference without encoding failure state in response text."""
        effective_model = model or self._resolve_model(None, False)
        body = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "mode": mode,
        }
        return self._invocation_result(
            self._post("/v1/chat/completions", body, timeout=timeout)
        )

    def invoke_scoped(
        self, prompt: str, *, project: str, part: str,
        model: str | None = None, timeout: float | None = None,
        mode: str = "efficiency",
    ) -> InvocationResult:
        """Status-aware inference with server-enforced Part scope."""
        body = {
            "model": model or self._resolve_model_for_part(project, part),
            "messages": [{"role": "user", "content": prompt}],
            "project": project,
            "part": part,
            "mode": mode,
        }
        return self._invocation_result(
            self._post("/v1/chat/completions", body, timeout=timeout)
        )

    @staticmethod
    def _invocation_result(result: dict[str, Any]) -> InvocationResult:
        if "error" in result:
            detail = result.get("error")
            return InvocationResult(
                False,
                error_kind=result.get("error_kind") or "router",
                error=str(detail) if detail else None,
            )
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return InvocationResult(False, error_kind="router")
        if not isinstance(content, str):
            return InvocationResult(False, error_kind="response")
        return InvocationResult(True, content=content)

    def chat_scoped(
        self,
        prompt: str,
        *,
        project: str,
        part: str,
        model: str | None = None,
        agentic: bool = False,
        mode: str = "efficiency",
    ) -> str:
        """Route a prompt with server-enforced project+part tool scope."""
        effective_model = model or self._resolve_model_for_part(project, part)
        body: dict[str, Any] = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "project": project,
            "part": part,
            "mode": mode,
        }
        if agentic:
            body["agentic"] = True
        result = self._post("/v1/chat/completions", body)
        return self._extract_content(result)

    def chat_scoped_full(
        self,
        prompt: str,
        *,
        project: str,
        part: str,
        model: str | None = None,
        agentic: bool = False,
        mode: str = "efficiency",
    ) -> dict[str, Any]:
        """Same as chat_scoped(), but returns the full response dict instead
        of just the extracted text -- specifically so callers can capture
        `orchestration_trace` (per-branch/per-model/per-tool-call steps from
        the agentic recursive/parallel delegation graph, populated whenever
        agentic=True) rather than having it silently discarded, which is
        what chat_scoped() itself still does for backward compatibility."""
        effective_model = model or self._resolve_model_for_part(project, part)
        body: dict[str, Any] = {
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "project": project,
            "part": part,
            "mode": mode,
        }
        if agentic:
            body["agentic"] = True
        return self._post("/v1/chat/completions", body)

    def chat_chain(self, chain_models: list[str], prompt: str) -> str:
        """Try each model in order, stopping at first success."""
        errors = []
        for model in chain_models:
            ok, content = self.chat_with_status(prompt, model=model)
            if ok:
                return content
            errors.append(f"{model}: {content}")
        return f"[error] all models in chain failed: {chain_models}\n" + "\n".join(errors)

    # ------------------------------------------------------------------
    # Model resolution helpers
    # ------------------------------------------------------------------

    def _resolve_model(self, task_type: str | None, prefer_fast: bool) -> str:
        backends = self._get("/backends").get("backends", [])
        available = [b["name"] for b in backends if b.get("enabled") and not b.get("circuit_open")]
        if prefer_fast:
            task_type = "fast"
        if task_type and task_type in _TASK_BACKEND_HINTS:
            capability = _TASK_BACKEND_HINTS[task_type]["prefer_capabilities"][0]
            if any(
                b.get("enabled") and not b.get("circuit_open")
                and b.get("capabilities", {}).get(capability) is True
                for b in backends
            ):
                return task_type
            hints = _TASK_BACKEND_HINTS[task_type]
            for name in hints.get("prefer_names", []):
                if name in available:
                    return name
        env_default = os.environ.get("HERALD_DEFAULT_MODEL")
        if env_default and env_default in available:
            return env_default
        if DEFAULT_MODEL in available:
            return DEFAULT_MODEL
        if available:
            return available[0]
        return DEFAULT_MODEL

    def _resolve_model_for_part(self, project: str, part: str) -> str:
        """Check if the part's router.yaml declared preferred backends."""
        # For now fall back to default — routing preferences per part
        # will be stored in a future `routing_json` column on the parts table.
        return DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_content(result: dict[str, Any]) -> str:
        if "error" in result:
            return f"[error] {result['error']}"
        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return f"[error] unexpected router response: {json.dumps(result)}"

    # ------------------------------------------------------------------
    # Status / observability
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        backends = self._get("/backends").get("backends", [])
        auth = self._get("/auth/status").get("clis", {})
        usage = self._get("/usage/summary")
        return {
            "router_url": self.base_url,
            "backends": backends,
            "auth": auth,
            "usage": usage,
        }

    def is_alive(self) -> bool:
        try:
            httpx.get(
                f"{self.base_url}/v1/models", timeout=3, headers=self._auth_headers(),
            ).raise_for_status()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Backend CRUD
    # ------------------------------------------------------------------

    def list_backends(self) -> list[dict[str, Any]]:
        return self._get("/backends").get("backends", [])

    def admin_reviews(self) -> list[dict[str, Any]]:
        """Return completed autonomous bundles awaiting operator review."""
        return self._get("/admin/reviews").get("reviews", [])

    def approve_admin_review(self, dispatch_id: str) -> dict[str, Any]:
        """Approve one reviewed Admin bundle into the authoritative workspace."""
        return self._post(f"/admin/reviews/{dispatch_id}/approve", {})

    def deny_admin_review(self, dispatch_id: str, reason: str) -> dict[str, Any]:
        """Deny one Admin bundle while retaining its immutable audit ref."""
        return self._post(f"/admin/reviews/{dispatch_id}/deny", {"reason": reason})

    def pending_devices(self) -> list[dict[str, Any]]:
        """Herald nodes discovered on the LAN via mDNS that aren't trusted yet."""
        return self._get("/devices/pending").get("devices", [])

    def trusted_devices(self) -> list[dict[str, Any]]:
        return self._get("/devices").get("devices", [])

    def approve_device(self, code: str) -> dict[str, Any]:
        """Trust a pending device by its short pairing code and hand it a bearer token."""
        return self._post("/devices/approve", {"code": code})

    def revoke_device(self, node_id: str) -> dict[str, Any]:
        return self._post(f"/devices/{node_id}/revoke", {})

    def register_backend(
        self,
        name: str,
        backend_type: str,
        config: dict[str, Any],
        *,
        priority: int = 100,
        pool_name: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name, "backend_type": backend_type, "config": config,
            "priority": priority,
        }
        if pool_name:
            body["pool_name"] = pool_name
        return self._post("/backends", body)

    def delete_backend(self, name: str) -> dict[str, Any]:
        return self._delete(f"/backends/{name}")

    def swarm(self, tasks: list[str], *, model: str | None = None,
              max_workers: int = 4, instructions: str = "", **chat_options: Any) -> list[dict[str, Any]]:
        """Run general-purpose independent tasks concurrently through this router."""
        from herald.swarm import run_swarm
        return run_swarm(self, tasks, model=model, max_workers=max_workers,
                         instructions=instructions, **chat_options)

    # ------------------------------------------------------------------
    # Named accounts and model lanes
    # ------------------------------------------------------------------

    def list_accounts(self, *, provider: str | None = None) -> list[dict[str, Any]]:
        params = {"provider": provider} if provider else {}
        result = self._get("/accounts", **params)
        if "error" in result:
            raise ValueError(self._stable_cli_transport_error(result["error"]))
        rows = result.get("accounts")
        if not isinstance(rows, list):
            raise ValueError("Herald router returned a malformed account response")
        return rows

    def register_account(
        self, name: str, provider: str, auth_kind: str, *,
        config: dict[str, Any] | None = None, secret_ref: str | None = None,
        enabled: bool = True, priority: int = 100, tags: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._post("/accounts", {
            "name": name, "provider": provider, "auth_kind": auth_kind,
            "config": config or {}, "secret_ref": secret_ref, "enabled": enabled,
            "priority": priority, "tags": tags or [],
        })

    def register_account_lane(
        self, account: str, name: str, backend_name: str, *, model: str = "",
        capabilities: dict[str, Any] | None = None, priority: int = 100,
        enabled: bool = True,
    ) -> dict[str, Any]:
        return self._post(f"/accounts/{account}/lanes", {
            "name": name, "backend_name": backend_name, "model": model,
            "capabilities": capabilities or {}, "priority": priority,
            "enabled": enabled,
        })

    def list_account_lanes(self, account: str) -> list[dict[str, Any]]:
        result = self._get(f"/accounts/{account}/lanes")
        if "error" in result:
            raise ValueError(self._stable_cli_transport_error(result["error"]))
        rows = result.get("lanes")
        if not isinstance(rows, list):
            raise ValueError("Herald router returned a malformed lane response")
        return rows

    def activate_account(self, account: str) -> dict[str, Any]:
        """Explicitly activate one named API-key account as its own backend."""
        return self._post(f"/accounts/{account}/activate", {})

    def _resolve_cli_account_lane(self, name: str) -> dict[str, Any]:
        """Validate an account and deterministically select an enabled lane."""
        accounts = self.list_accounts()
        account = next((item for item in accounts if item.get("name") == name), None)
        if account is None:
            raise ValueError(f"CLI account {name!r} was not found")
        if not account.get("enabled", True):
            raise ValueError(f"CLI account {name!r} is disabled")
        if account.get("auth_kind") != "cli_profile":
            raise ValueError(f"account {name!r} is not a CLI profile")
        lanes = [lane for lane in self.list_account_lanes(name) if lane.get("enabled", True)]
        if not lanes:
            raise ValueError(f"CLI account {name!r} has no enabled model lane")
        return min(
            lanes,
            key=lambda lane: (
                int(lane.get("priority", 100)), str(lane.get("name", "")),
                str(lane.get("id", "")),
            ),
        )

    @staticmethod
    def _stable_cli_transport_error(error: Any) -> str:
        from herald.router.sanitization import sanitize_error
        text = sanitize_error(error)
        lowered = text.lower()
        if "timed out" in lowered or "timeout" in lowered:
            return "Herald router request timed out"
        if "not reachable" in lowered or "connect" in lowered:
            return "Herald router is unavailable"
        return f"Herald router request failed: {text}"

    def cli_account(self, name: str) -> CLIAccount:
        """Return a validated callable handle for a router-owned CLI profile."""
        self._resolve_cli_account_lane(name)
        return CLIAccount(name=name, _client=self)

    def invoke_cli_account(
        self, name: str, prompt: str, *, timeout: float | None = None,
        agentic: bool = False, mode: str = "efficiency",
    ) -> str:
        """Resolve an enabled lane and use the router's normal chat route."""
        lane = self._resolve_cli_account_lane(name)
        backend_name = lane.get("backend_name")
        if not isinstance(backend_name, str) or not backend_name:
            raise ValueError(f"CLI account {name!r} has no enabled model lane")
        ok, content = self.chat_with_status(
            prompt, model=backend_name, agentic=agentic, mode=mode, timeout=timeout,
            cli_account=name,
        )
        if not ok:
            from herald.router.sanitization import sanitize_error
            from herald.errors import CLIAccountInvocationError
            raise CLIAccountInvocationError(sanitize_error(content))
        return content

    # ------------------------------------------------------------------
    # Project / Part / ToolInstance CRUD
    # ------------------------------------------------------------------

    def create_project(self, name: str, description: str = "") -> dict[str, Any]:
        return self._post("/projects", {"name": name, "description": description})

    def list_projects(self) -> list[dict[str, Any]]:
        return self._get("/projects").get("projects", [])

    def delete_project(self, name: str) -> dict[str, Any]:
        return self._delete(f"/projects/{name}")

    def create_part(self, project: str, part: str, description: str = "") -> dict[str, Any]:
        return self._post(f"/projects/{project}/parts", {"name": part, "description": description})

    def list_parts(self, project: str) -> list[dict[str, Any]]:
        return self._get(f"/projects/{project}/parts").get("parts", [])

    def set_part_tools(self, project: str, part: str, tools: list[str]) -> dict[str, Any]:
        return self._put(f"/projects/{project}/parts/{part}/tools", {"tools": tools})

    def bind_tool(
        self, project: str, part: str, tool: str,
        position: int | None = None, alias: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"tool": tool}
        if position is not None:
            body["position"] = position
        if alias is not None:
            body["alias"] = alias
        return self._post(f"/projects/{project}/parts/{part}/tools", body)

    def register_tool_instance(
        self,
        name: str,
        transport: str,
        config: dict[str, Any],
        *,
        description: str = "",
        tags: list[str] | None = None,
        package_name: str = "",
        version: str = "unversioned",
        scope: str = "global",
        project: str | None = None,
        source: dict[str, Any] | None = None,
        isolation_key: str = "",
    ) -> dict[str, Any]:
        return self._post("/tool-instances", {
            "name": name, "transport": transport, "config": config,
            "description": description, "tags": tags or [],
            "package_name": package_name, "version": version, "scope": scope,
            "project": project, "source": source or {}, "isolation_key": isolation_key,
        })

    def register_routing_policy(
        self,
        name: str,
        *,
        automatic_order: list[str],
        delegate_types: list[str],
        description: str = "",
        free_only: bool = False,
        compact_tool_catalog: bool = False,
        tool_bridge: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Register a user-defined routing policy from router.yaml, usable
        by name through get_policy() exactly like a built-in policy."""
        return self._post("/routing-policies", {
            "name": name, "description": description,
            "automatic_order": automatic_order, "delegate_types": delegate_types,
            "free_only": free_only, "compact_tool_catalog": compact_tool_catalog,
            "tool_bridge": tool_bridge, "project": project,
        })

    def register_idle_unload_config(self, model_name: str, idle_unload_minutes: float | None) -> dict[str, Any]:
        """Set (or clear, with None/0) the auto-unload idle threshold for a
        local model, from router.yaml's `local_models` block."""
        return self._post("/local-model-idle-config", {
            "model_name": model_name, "idle_unload_minutes": idle_unload_minutes,
        })

    def register_connector(
        self, name: str, *, type: str, base_url: str, auth: str = "none",
        capability_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register a declarative connector from router.yaml's `connectors`
        block -- routes to the matching auto-discovery function in
        herald/connectors.py instead of requiring core-file edits."""
        return self._post("/connectors", {
            "name": name, "type": type, "base_url": base_url,
            "auth": auth, "capability_tags": capability_tags or [],
        })

    def resolve_scope(self, project: str, part: str) -> list[dict[str, Any]]:
        return self._get(f"/projects/{project}/parts/{part}/scope").get("tools", [])

    def list_tools(
        self, *, project: str | None = None, part: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if project is not None or part is not None:
            params = {"project": project, "part": part}
        return self._get("/tools", **params).get("tools", [])

    def run_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *,
        project: str | None = None, part: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if project is not None or part is not None:
            body.update({"project": project, "part": part})
        return self._post("/tools/run", body)

    # ------------------------------------------------------------------
    # Controlled MCP groups and shared gateway access
    # ------------------------------------------------------------------

    def list_mcp_groups(self) -> list[dict[str, Any]]:
        return self._get("/mcp-groups").get("groups", [])

    def create_mcp_group(self, name: str, description: str = "") -> dict[str, Any]:
        return self._post("/mcp-groups", {"name": name, "description": description})

    def get_mcp_group(self, name: str) -> dict[str, Any]:
        return self._get(f"/mcp-groups/{name}")

    def delete_mcp_group(self, name: str) -> dict[str, Any]:
        return self._delete(f"/mcp-groups/{name}")

    def add_mcp_group_tool(
        self, group: str, tool: str, *, allowed_tools: list[str] | None = None,
        alias: str | None = None, position: int | None = None,
    ) -> dict[str, Any]:
        return self._post(f"/mcp-groups/{group}/tools", {
            "tool": tool, "allowed_tools": allowed_tools,
            "alias": alias, "position": position,
        })

    def remove_mcp_group_tool(self, group: str, tool: str) -> dict[str, Any]:
        return self._delete(f"/mcp-groups/{group}/tools/{tool}")

    def bind_mcp_group(
        self, group: str, target_type: str, target_key: str = "*",
        position: int | None = None,
    ) -> dict[str, Any]:
        return self._post(f"/mcp-groups/{group}/bindings", {
            "target_type": target_type, "target_key": target_key,
            "position": position,
        })

    def unbind_mcp_group(
        self, group: str, target_type: str, target_key: str = "*",
    ) -> dict[str, Any]:
        return self._delete(
            f"/mcp-groups/{group}/bindings/{target_type}/{target_key}"
        )

    def mcp_access(
        self, *, groups: list[str] | None = None, project: str | None = None,
        part: str | None = None, profile: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/mcp-access", groups=",".join(groups or []),
            project=project, part=part, profile=profile,
        )

    def run_mcp_access_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *,
        groups: list[str] | None = None, project: str | None = None,
        part: str | None = None, profile: str | None = None,
    ) -> dict[str, Any]:
        return self._post("/mcp-access/run", {
            "name": name, "arguments": arguments or {}, "groups": groups,
            "project": project, "part": part, "profile": profile,
        })

    # ------------------------------------------------------------------
    # Named stateful agents
    # ------------------------------------------------------------------

    def open_agent_session(
        self, *, name: str, memory: str | None = None, model: str = "auto",
        mode: str = "efficiency", instructions: str = "",
        project: str | None = None, part: str | None = None,
        agentic: bool = False,
        recent_turns: int = 8, ledger_entries: int = 40,
        retention_days: int | None = None,
    ) -> dict[str, Any]:
        return self._post("/agent-sessions", {
            "name": name,
            "memory": memory,
            "model": model,
            "mode": mode,
            "instructions": instructions,
            "project": project,
            "part": part,
            "agentic": agentic,
            "recent_turns": recent_turns,
            "ledger_entries": ledger_entries,
            "retention_days": retention_days,
        })

    def send_agent_message(
        self, session_id: str, prompt: str, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            f"/agent-sessions/{session_id}/messages",
            {"prompt": prompt, "request_id": request_id},
            timeout=max(self.timeout, 600),
        )

    def reset_agent_session(
        self, session_id: str, *, force: bool = False, instructions: str = "",
    ) -> dict[str, Any]:
        return self._post(
            f"/agent-sessions/{session_id}/reset",
            {"force": force, "instructions": instructions},
        )

    def list_agent_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._get("/agent-sessions", limit=limit).get("sessions", [])

    def search_agent_sessions(
        self, query: str, *, project: str | None = None, part: str | None = None,
        since: str | None = None, until: str | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "limit": limit}
        if project:
            params["project"] = project
        if part:
            params["part"] = part
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        return self._get("/agent-sessions/search", **params).get("results", [])

    def inspect_agent_session(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/agent-sessions/{session_id}")

    def delete_agent_session(self, session_id: str) -> dict[str, Any]:
        return self._delete(f"/agent-sessions/{session_id}")

    def export_agent_session(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/agent-sessions/{session_id}/export")

    def import_agent_session(self, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/agent-sessions/{session_id}/import", {"data": data})

    # ------------------------------------------------------------------
    # Declarative flows and routing behavior
    # ------------------------------------------------------------------

    def routing_policies(self) -> list[dict[str, Any]]:
        return self._get("/routing/policies").get("policies", [])

    def validate_flow(
        self, spec: dict[str, Any], *, project: str | None = None,
        part: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"spec": spec}
        if project is not None or part is not None:
            body.update({"project": project, "part": part})
        return self._post("/flows/validate", body)

    def run_flow(
        self, spec: dict[str, Any], input_text: str, *,
        project: str | None = None, part: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"spec": spec, "input": input_text}
        if project is not None or part is not None:
            body.update({"project": project, "part": part})
        return self._post("/flows/run", body, timeout=max(self.timeout, 600))

    def create_flow_run(
        self, spec: dict[str, Any], input_text: str, *,
        project: str | None = None, part: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"spec": spec, "input": input_text}
        if project is not None or part is not None:
            body.update({"project": project, "part": part})
        return self._post("/flow-runs", body, timeout=max(self.timeout, 600))

    def list_flow_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._get("/flow-runs", limit=limit).get("runs", [])

    def resume_flow_run(self, run_id: str) -> dict[str, Any]:
        return self._post(f"/flow-runs/{run_id}/resume", {}, timeout=max(self.timeout, 600))

    def list_capture_accounts(self) -> list[dict[str, Any]]:
        return self._get("/capture/g4f/accounts").get("accounts", [])

    def start_g4f_capture(self, account: str, *, timeout: float = 120) -> dict[str, Any]:
        return self._post("/capture/g4f/sessions", {"account": account, "timeout": timeout})

    def list_g4f_captures(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._get("/capture/g4f/sessions", limit=limit).get("sessions", [])

    def get_g4f_capture(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/capture/g4f/sessions/{session_id}")

    def discover_integrations(self) -> dict[str, Any]:
        return self._get("/integrations/discover")

    def import_integration(
        self, owner: str, name: str, *, source: str | None = None,
        registry_name: str | None = None, scope: str = "global",
        project: str | None = None,
    ) -> dict[str, Any]:
        return self._post("/integrations/import", {
            "owner": owner, "name": name, "source": source,
            "registry_name": registry_name, "scope": scope, "project": project,
        })

    def list_events(self, limit: int = 100, topic: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if topic:
            params["topic"] = topic
        return self._get("/events", **params).get("events", [])

    def register_hook(
        self, name: str, pattern: str, transport: str, config: dict[str, Any], *,
        secret_ref: str | None = None, enabled: bool = True,
    ) -> dict[str, Any]:
        return self._post("/hooks", {
            "name": name, "pattern": pattern, "transport": transport,
            "config": config, "secret_ref": secret_ref, "enabled": enabled,
        })

    def list_hooks(self) -> list[dict[str, Any]]:
        return self._get("/hooks").get("hooks", [])

    def remove_hook(self, name: str) -> dict[str, Any]:
        return self._delete(f"/hooks/{name}")
