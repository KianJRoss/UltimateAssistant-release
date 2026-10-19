"""Per-backend-type execution. Each function takes a backend's config dict
and a prompt, returns:
    {"ok": bool, "content"|"error": str,
     "usage": {"input_tokens": int|None, "output_tokens": int|None, "cost_usd": float|None},
     "thinking": str|None, "thinking_tokens": int|None}
Kept separate from registry.py (state) and main.py (HTTP surface) so each
adapter is independently testable, matching how every other piece of this
build has been verified today.

Usage/thinking extraction is honest about what each backend actually
exposes -- Antigravity's JSON gives a thinking_tokens COUNT but never the
reasoning text itself, Claude gives real cost, LM Studio/g4f give OpenAI-
shaped token counts but no cost. Fields that aren't available stay None
rather than being guessed at.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from herald.router.sanitization import sanitize_error

_logger = logging.getLogger("herald.adapters")

HERALD_ROOT = Path(__file__).resolve().parents[1]
CLINK_ROOT = HERALD_ROOT / "clink"
UTILS_ROOT = HERALD_ROOT / "utils"

for _p in (HERALD_ROOT, CLINK_ROOT, UTILS_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

cfg_dir = HERALD_ROOT / "conf" / "conf" / "cli_clients"
if cfg_dir.exists():
    os.environ.setdefault("CLI_CLIENTS_CONFIG_PATH", str(cfg_dir))


def _no_usage() -> dict[str, Any]:
    return {"input_tokens": None, "output_tokens": None, "cost_usd": None}


# OpenAI-compatible base URLs for providers that don't need an explicit
# base_url in config -- only consulted when config["provider"] names one of
# these; anything else must supply its own base_url.
_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "xai": "https://api.x.ai/v1",
}


def _call_api_key_direct(config: dict[str, Any], prompt: str, timeout: float = 30.0) -> dict[str, Any]:
    """config: {"api_key": "...", "model_name": "...", "base_url": "..." |
    "provider": "openai"|"gemini"|"openrouter"|"deepseek"|"xai"}
    Used instead of PAL's ModelProviderRegistry when a backend row carries
    its own key -- PAL resolves keys from a single process-wide .env, which
    can't hold N keys per provider, and mutating os.environ per request
    would race under FastAPI's concurrent handling. This calls the
    provider's OpenAI-compatible endpoint directly instead, same shape as
    call_local_model below."""
    if config.get("provider") in ("anthropic", "claude"):
        try:
            base = config.get("base_url") or "https://api.anthropic.com/v1"
            resp = httpx.post(f"{base.rstrip('/')}/messages",
                              headers={"x-api-key": config["api_key"], "anthropic-version": "2023-06-01"},
                              json={"model": config["model_name"], "max_tokens": int(config.get("max_tokens", 4096)),
                                    "messages": [{"role": "user", "content": prompt}]}, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage") or {}
            return {"ok": True, "content": "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text"),
                    "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "cost_usd": None},
                    "thinking": None, "thinking_tokens": None}
        except Exception as exc:
            return {"ok": False, "error": sanitize_error(exc)}
    if config.get("provider") in ("gemini", "google"):
        model_name = config.get("model_name", "gemini-flash-latest")
        api_key = config["api_key"]
        try:
            resp = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(p.get("text", "") for p in parts)
            usage = data.get("usageMetadata", {})
            return {
                "ok": True, "content": content,
                "usage": {
                    "input_tokens": usage.get("promptTokenCount"),
                    "output_tokens": usage.get("candidatesTokenCount"),
                    "cost_usd": 0.0,
                },
                "thinking": None, "thinking_tokens": None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": sanitize_error(exc)}

    base_url = config.get("base_url") or _PROVIDER_BASE_URLS.get(config.get("provider", ""))
    if not base_url:
        return {"ok": False, "error": "config has an api_key but no base_url and no recognized 'provider' to default it from"}
    model_name = config.get("model_name")
    if not model_name:
        return {"ok": False, "error": "config missing 'model_name' for direct API-key call"}
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config['api_key']}"},
            json={"model": model_name, "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": sanitize_error(exc)}
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError):
        return {"ok": False, "error": f"unexpected response shape: {data!r}"}

    usage = data.get("usage") or {}
    return {
        "ok": True, "content": message.get("content", ""),
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "cost_usd": None,  # per-row keys don't carry a known $/token rate
        },
        "thinking": message.get("reasoning_content") or None,
        "thinking_tokens": None,
    }


def call_api_key(config: dict[str, Any], prompt: str) -> dict[str, Any]:
    """config: {"model_name": "<PAL-registered model or alias>"}, OR
    {"api_key": "...", "model_name": ..., "base_url"|"provider": ...} for a
    backend row carrying its own key (see _call_api_key_direct)."""
    if config.get("api_key") or config.get("secret_ref"):
        resolved = dict(config)
        if not resolved.get("api_key"):
            try:
                from herald.router.account_registry import resolve_secret_ref
                resolved["api_key"] = resolve_secret_ref(str(resolved["secret_ref"]))
            except ValueError as exc:
                return {"ok": False, "error": sanitize_error(exc)}
        return _call_api_key_direct(resolved, prompt)

    _ensure_pal_configured()
    from herald.providers.providers.registry import ModelProviderRegistry
    model_name = config["model_name"]
    provider = ModelProviderRegistry.get_provider_for_model(model_name)
    if provider is None:
        return {"ok": False, "error": f"no provider registered for model '{model_name}'"}
    try:
        response = provider.generate_content(prompt, model_name)
    except Exception as exc:  # noqa: BLE001 - one bad backend must not crash the router
        return {"ok": False, "error": sanitize_error(exc)}
    usage = response.usage or {}
    return {
        "ok": True, "content": response.content,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost_usd": None,  # PAL's ModelResponse doesn't carry a dollar cost
        },
        "thinking": None, "thinking_tokens": None,
    }


def _brief_arguments(value: Any, limit: int = 300) -> Any:
    """Tool arguments for a progress line: small values as-is, long strings and big inputs cut."""
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:8]:
            if isinstance(item, str) and len(item) > 120:
                item = item[:117] + "..."
            elif isinstance(item, (dict, list)):
                item = "..."
            out[str(key)[:40]] = item
        return out
    return str(value)[:limit] if value is not None else None


def _parse_codex_line(parsed: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """codex exec --json: {"type": "item.started"/"item.completed"/
    "turn.completed"/"error"/..., "item": {"type": "command_execution"/
    "agent_message", "command", "status", "exit_code", "text"}}"""
    event_type = parsed.get("type", "?")
    item = parsed.get("item", {})
    if event_type == "thread.started" and parsed.get("thread_id"):
        state["native_session_id"] = parsed["thread_id"]
    if event_type == "item.completed" and item.get("type") == "agent_message":
        state.setdefault("agent_messages", []).append(item.get("text", ""))
    elif event_type == "turn.completed":
        state["usage"] = parsed.get("usage") or {}
    elif event_type in ("error", "turn.failed"):
        state.setdefault("stderr_lines", []).append(
            parsed.get("message") or parsed.get("error", {}).get("message", "")
        )
    return {
        "kind": event_type,
        **({"item_type": item.get("type"), "command": item.get("command"),
            "status": item.get("status"), "exit_code": item.get("exit_code")} if item else {}),
        **({"text": item.get("text")} if item.get("type") == "agent_message" else {}),
        # What the step is doing, so interfaces can show "list_windows" rather than "mcp tool call".
        **({"server": item.get("server"), "tool_name": item.get("tool"), "arguments": _brief_arguments(item.get("arguments"))}
           if item.get("type") == "mcp_tool_call" else {}),
        **({"query": item.get("query")} if item.get("type") == "web_search" else {}),
        **({"paths": [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict)][:10]}
           if item.get("type") == "file_change" else {}),
    }


def _parse_claude_line(parsed: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """claude -p --output-format stream-json: {"type": "assistant"/"user"/
    "result"/"system", "message": {"content": [{"type": "tool_use"/
    "tool_result"/"text", "name", "input", "content", "text"}]}}"""
    mtype = parsed.get("type", "?")
    if parsed.get("session_id"):
        state["native_session_id"] = parsed["session_id"]
    if mtype == "result":
        state["usage"] = parsed.get("usage") or {}
        state["cost_usd"] = parsed.get("total_cost_usd")
        if parsed.get("result"):
            state.setdefault("agent_messages", []).append(parsed["result"])
        if parsed.get("is_error"):
            state.setdefault("stderr_lines", []).append(parsed.get("result") or "claude reported an error result")
        return {"kind": "result", "is_error": parsed.get("is_error"), "num_turns": parsed.get("num_turns")}
    if mtype not in ("assistant", "user"):
        return None
    blocks = (parsed.get("message") or {}).get("content") or []
    if not blocks or not isinstance(blocks, list):
        return None
    block = blocks[0]
    btype = block.get("type")
    if btype == "tool_use":
        return {"kind": "tool_use", "name": block.get("name"), "tool_name": block.get("name"),
                "arguments": _brief_arguments(block.get("input")), "input": block.get("input")}
    if btype == "tool_result":
        content = block.get("content")
        return {"kind": "tool_result", "content": content if isinstance(content, str) else str(content)[:500]}
    if btype == "text":
        state.setdefault("agent_messages", []).append(block.get("text", ""))
        return {"kind": "text", "text": block.get("text")}
    return None


def _parse_antigravity_line(parsed: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """agy --output-format stream-json: {"event": "step_update"/"result"/
    "init", "step_update": {"step_type": "tool"/"agent_response", "state":
    "ACTIVE"/"DONE", "tool_name", "tool_info", "text_delta", "usage"}}"""
    etype = parsed.get("event", "?")
    if etype == "init":
        import re
        info=parsed.get("init") if isinstance(parsed.get("init"),dict) else {}
        metadata={"kind":"session.init"}
        identity=parsed.get("conversation_id") or info.get("conversation_id") or info.get("session_id")
        if isinstance(identity,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',identity):
            state["conversation_id"]=metadata["conversation_id"]=identity
        permission=parsed.get("permission_mode") or info.get("permission_mode")
        if isinstance(permission,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}',permission):
            state["permission_mode"]=metadata["permission_mode"]=permission
        tools=parsed.get("tools",info.get("tools"))
        if isinstance(tools,list) and len(tools)<=512 and all(isinstance(tool,str) and 0<len(tool)<=200 for tool in tools):
            state["available_tools"]=metadata["available_tools"]=len(tools)
        return metadata
    if etype == "result":
        state["result_seen"]=True
        state["result_status"]=(parsed.get("result") or {}).get("status")
        result = parsed.get("result", {})
        state["usage"] = result.get("usage") or {}
        if result.get("response"):
            state["result_response"] = result["response"]
        if result.get("status") != "SUCCESS":
            state.setdefault("stderr_lines", []).append(f"antigravity status: {result.get('status')}")
            if result.get("error"):
                state["stderr_lines"].append(sanitize_error(result["error"]))
        return {"kind": "result", "status": result.get("status")}
    if etype != "step_update":
        return None
    step = parsed.get("step_update", {})
    step_type = step.get("step_type")
    if step_type == "tool" and step.get("state") == "ACTIVE":
        state["native_tools_started"] = state.get("native_tools_started", 0) + 1
    if step_type == "agent_response" and step.get("text_delta"):
        state.setdefault("response_deltas", []).append(step["text_delta"])
    tool_error = step.get("state") == "ERROR" or bool((step.get("tool_info") or {}).get("error"))
    if step_type == "tool" and tool_error:
        info = step.get("tool_info") or {}
        error = info.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        # A missing-file reconnaissance probe is recoverable in the native loop.
        # Permission failures and all other tool errors remain fatal; terminal
        # SUCCESS and independent scope/test gates are still required.
        missing_probe = (step.get("tool_name") == "view_file"
                         and isinstance(message, str)
                         and "no such file or directory" in message.lower()
                         and not any(term in message.lower() for term in
                                     ("permission denied", "access denied", "not authorized")))
        state.setdefault("tool_warnings" if missing_probe else "stderr_lines", []).append(
            f"Antigravity tool {step.get('tool_name') or 'unknown'} failed: {message or 'reported ERROR'}"
        )
    return {
        "kind": f"step.{step_type}", "state": step.get("state"),
        **({"tool_name": step.get("tool_name"), "tool_info": step.get("tool_info")} if step_type == "tool" else {}),
        **({"text": step.get("text_delta")} if step_type == "agent_response" else {}),
    }


_STREAMING_PARSERS = {
    "codex": _parse_codex_line,
    "claude": _parse_claude_line,
    "antigravity": _parse_antigravity_line,
}


def _detect_streaming_cli_kind(exec_name: str) -> str | None:
    name = exec_name.lower()
    if "codex" in name:
        return "codex"
    if "claude" in name:
        return "claude"
    if "agy" in name or "antigravity" in name:
        return "antigravity"
    return None


def _partial_content(state: dict[str, Any]) -> str:
    """Whatever real text the CLI had produced before a failure/timeout cut
    it off -- used to hand real, already-done progress forward into a
    fallback attempt on a different CLI/account instead of starting that
    fallback cold with only the original prompt. Herald is already watching
    every line this CLI streams (that's what makes it a real "window", not
    just a black-box subprocess) -- this is that same captured state,
    reused for continuity instead of being thrown away on failure."""
    if state.get("result_response"):
        return state["result_response"].strip()
    if state.get("response_deltas"):
        return "".join(state["response_deltas"]).strip()
    messages = state.get("agent_messages") or []
    return "\n".join(m for m in messages if m).strip()


def _call_cli_streaming(
    cmd: list[str], env: dict[str,str], client: Any, timeout: int, cli_kind: str,
    *, stdin_text: str | None = None, health_check_interval: float | None = None,
) -> dict[str,Any]:
    """Stream the native CLI, draining both pipes within a wall-clock deadline."""
    from herald.router import event_bus
    from herald.router.native_process import OwnedCLI
    parse_line=_STREAMING_PARSERS[cli_kind]
    scope=event_bus.get_scope()
    state={}
    if health_check_interval is not None:
        import math
        if type(health_check_interval) not in (int,float) or not math.isfinite(health_check_interval) or health_check_interval<=0:
            raise ValueError("Health check interval must be positive and finite")
    started=time.monotonic();deadline=started+timeout;last=None
    next_health=started+(health_check_interval or 0)
    try:owned=OwnedCLI(cmd,env,str(client.working_dir) if client.working_dir else None,stdin_text)
    except FileNotFoundError:return {'ok':False,'error_kind':'missing_executable','error':f'{cmd[0]} not found on PATH'}
    def failure(kind,message):
        value={'ok':False,'error_kind':kind,'error':message,'partial_content':(state.get('result_response') or ''.join(state.get('response_deltas') or [])) if cli_kind=='antigravity' else _partial_content(state),
               'native_tools_started':state.get('native_tools_started',0),'elapsed_seconds':time.monotonic()-started}
        if last is not None:value['since_last_event_seconds']=time.monotonic()-last
        if state.get('conversation_id'):value['conversation_id']=state['conversation_id']
        return value
    ended=set()
    try:
        while len(ended)<2:
            try:
                from herald.router.execution_control import execution_cancel
                cancellation=execution_cancel.get()
            except ImportError:cancellation=None
            if cancellation is not None and cancellation.is_set():
                return failure('cancelled','Delegated native execution cancelled')
            if env.get('HERALD_ADMIN_CALL')=='1':
                from herald.router.admin_control import stop_requested
                if stop_requested():return failure('cancelled','Admin execution stopped by operator')
            now=time.monotonic()
            if health_check_interval is not None and now>=next_health:
                code=owned.proc.poll()
                if code is not None and code!=0:return failure("health_failed",f"Owned native process exited with code {code}")
                event_bus.emit_nowait("agent.step",importance=.2,payload={**scope,"kind":"health.check","status":"unknown" if code is None else "exited","elapsed":now-started},source=f"{cli_kind}_cli")
                next_health=now+health_check_interval
            remaining=deadline-now if health_check_interval is None else .2
            if remaining<=0:return failure('deadline_exceeded',f'{cli_kind} exceeded its {timeout}s wall-clock deadline')
            try:kind,line=owned.queue.get(timeout=min(remaining,.2))
            except queue.Empty:continue
            if kind=='oversize':
                _logger.info('%s: skipped one stdout line over %s bytes', cli_kind, owned.MAX_LINE)
                continue
            if kind=='stdin_error':return failure('stdin_failed','Could not send native prompt')
            if line is None:ended.add(kind);continue
            try:parsed=json.loads(line)
            except json.JSONDecodeError:continue
            if not isinstance(parsed,dict):continue
            step=parse_line(parsed,state)
            if step:
                last=time.monotonic()
                try:
                    from herald.router.execution_control import execution_progress
                    progress=execution_progress.get()
                except ImportError:progress=None
                if progress:progress({key:step.get(key) for key in ('kind','tool_name','status','state')})
                event_bus.emit_nowait('agent.step',importance=.2,payload={**scope,**step},source=f'{cli_kind}_cli')
        try:owned.proc.wait(timeout=max(.1,deadline-time.monotonic()) if health_check_interval is None else None)
        except subprocess.TimeoutExpired:return failure('deadline_exceeded',f'{cli_kind} exceeded its {timeout}s wall-clock deadline')
        stderr_lines=state.get('stderr_lines') or []
        if owned.proc.returncode or stderr_lines:
            diagnostics=list(filter(None,stderr_lines))
            native_stderr=sanitize_error(owned.stderr.strip())
            if native_stderr and native_stderr not in diagnostics:diagnostics.append(native_stderr[-4000:])
            details='; '.join(diagnostics) or f'exit code {owned.proc.returncode}'
            return failure('cli_failed',sanitize_error(details[-4000:]))
        if cli_kind=='antigravity' and (not state.get('result_seen') or state.get('result_status')!='SUCCESS'):
            return failure('missing_result','Antigravity exited without a successful final result')
        messages=state.get('agent_messages') or []
        content=(state.get('result_response') or ''.join(state.get('response_deltas') or [])) if cli_kind=='antigravity' else (messages[-1] if messages else '')
        if not content.strip():return failure('missing_response',f'{cli_kind} exited successfully but returned no assistant response')
        usage=state.get('usage') or {}
        return {'ok':True,'content':content,'native_tools_started':state.get('native_tools_started',0),
                **({'conversation_id':state['conversation_id']} if state.get('conversation_id') else {}),
                **({'native_session_id':state['native_session_id']} if state.get('native_session_id') else {}),
                'usage':{'input_tokens':usage.get('input_tokens'),'output_tokens':usage.get('output_tokens'),'cost_usd':state.get('cost_usd')},
                'thinking':None,'thinking_tokens':usage.get('reasoning_output_tokens') or usage.get('thinking_tokens')}
    finally:owned.close()


def call_cli(
    config: dict[str, Any],
    prompt: str,
    timeout: int | None = None,
    *,
    working_dir: str | Path | None = None,
) -> dict[str, Any]:
    """config: {"cli_name": "<clink-registered name, e.g. codex>", "timeout": <seconds, optional>}

    Default timeout is 1800s (30 min), not the old 180s (3 min). A CLI
    backend doing real coding work (reading files, editing, compiling) is a
    single-prompt, non-interactive invocation with no way to resume once
    killed -- a call that gets cut off mid-task doesn't produce a partial
    result, it produces whatever garbage was in stdout/stderr at that
    instant, and 180s is shorter than a single real build/test step, let
    alone the sequence of steps a real task needs.

    Precedence, highest first: explicit `timeout=` (a direct API caller) >
    `config["timeout"]` (a per-backend-row override) > the CLI's own
    resolved `timeout_seconds` from its clink config file (previously
    computed but never actually read here -- a user setting a longer
    timeout for one specific slow CLI had no way to make it take effect).
    """
    try:
        from clink.registry import ClinkRegistry
        client = ClinkRegistry().get_client(config["cli_name"])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": sanitize_error(exc)}
    if working_dir is None:
        working_dir = config.get("working_dir")
    if working_dir is None and not client.working_dir:
        working_dir = os.environ.get("HERALD_WORKSPACE")
    if working_dir is not None:
        resolved_working_dir = Path(working_dir).expanduser().resolve()
        if not resolved_working_dir.is_dir():
            return {"ok": False, "error": f"working directory does not exist: {resolved_working_dir}"}
        # ResolvedCLIClient is a Pydantic model.  Clone it per call instead of
        # mutating the registry-owned profile: swarm workers run concurrently
        # and each must keep its own Git worktree as its process cwd.
        client = client.model_copy(update={"working_dir": resolved_working_dir})
    if timeout is None:
        timeout = int(config.get("timeout") or client.timeout_seconds or 1800)
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in client.env.items()})
    if "env" in config and isinstance(config["env"], dict):
        env.update({str(key): str(value) for key, value in config["env"].items()})
    # Forward the current consult_models() recursion depth into this CLI
    # subprocess's own env -- its own consult_models tool instance (if
    # reachable via its MCP gateway) reads HERALD_CONSULT_DEPTH to decide
    # whether it's allowed to delegate further. Sourced from event_bus's
    # per-request scope (set in server.py before dispatch) rather than a
    # new parameter threaded through every call in this chain, matching
    # how project/part scope already reaches _call_cli_streaming below.
    try:
        from herald.router import event_bus
        native_scope = event_bus.get_scope()
        if native_scope.get("run_id"):
            for field, variable in (("run_id", "HERALD_ASSISTANT_PARENT_ID"),
                                    ("project", "HERALD_ASSISTANT_PROJECT"),
                                    ("part", "HERALD_ASSISTANT_PART"),
                                    ("activity_id", "HERALD_ASSISTANT_ACTIVITY_ID")):
                env[variable] = str(native_scope.get(field) or "")
        depth = native_scope.get("consult_depth")
        if depth:
            env["HERALD_CONSULT_DEPTH"] = str(depth)
    except Exception:
        pass
    extra_paths = [
        str(Path.home() / "AppData" / "Roaming" / "npm"),
        str(Path.home() / "AppData" / "Local" / "agy" / "bin"),
        str(Path.home() / ".local" / "bin"),
        "/usr/local/bin",
    ]
    current_path = env.get("PATH", "")
    for p in extra_paths:
        if p not in current_path.split(os.pathsep):
            current_path = f"{p}{os.pathsep}{current_path}"
    env["PATH"] = current_path

    import shutil
    exec_name = client.executable[0] if client.executable else "codex"
    resolved_exec = shutil.which(exec_name, path=env["PATH"]) or exec_name
    # Real per-step JSONL streaming, confirmed live for all three: codex's
    # `--json`, claude's `--output-format stream-json --verbose`, agy's
    # (Antigravity, including the profile_gemini/profile_claude/profile_gpt
    # model presets, all sharing the same binary) `--output-format
    # stream-json`. This is what makes each CLI's own internal tool calls
    # (shell commands, file edits) visible to /schedule watch at all --
    # previously this was a fully opaque subprocess.run() call with zero
    # visibility into anything until the whole process exited.
    #
    # claude.json and antigravity.json's clink configs already hardcode
    # `--output-format json` in internal_args (for the old single-shot
    # parse path) -- appending our own `--output-format stream-json` after
    # that produces a duplicate/conflicting flag. Confirmed empirically:
    # claude happened to take the later flag (last-wins), antigravity did
    # NOT (silently kept single-shot json, meaning zero live events and
    # visibly garbled content extraction from feeding a whole JSON blob
    # through the per-line JSONL reader). Strip any existing
    # `--output-format <value>` pair before adding our own instead of
    # relying on undocumented per-CLI precedence behavior.
    def _strip_output_format(args: list[str]) -> list[str]:
        out: list[str] = []
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg == "--output-format":
                skip_next = True
                continue
            out.append(arg)
        return out

    base_args = _strip_output_format([*client.internal_args, *client.config_args])
    from herald.router import event_bus
    if event_bus.get_scope().get("assistant_work_slot") is not None:
        resume_flags = {"--continue", "--resume", "--conversation", "--conversation-id", "--session-id", "resume"}
        if any(str(arg).split("=", 1)[0] in resume_flags for arg in base_args):
            return {"ok": False, "error": "Delegated workers require independent native sessions; this profile has shared conversation/resume flags. Configure a separate worker profile without those flags."}

    cli_kind = _detect_streaming_cli_kind(exec_name)
    if config.get("model"):
        # A per-call model selection must replace the profile flag, not duplicate it.
        cleaned = []
        skip = False
        for arg in base_args:
            if skip:
                skip = False
                continue
            if arg == "--model":
                skip = True
            elif not arg.startswith("--model="):
                cleaned.append(arg)
        base_args = [*cleaned, "--model", str(config["model"])]
    # These calls run unattended through the full native provider harness.
    # Use the provider's supported per-invocation permission flag; leave its
    # persisted configuration and native tools intact.
    permission_flag = {
        "codex": "--dangerously-bypass-approvals-and-sandbox",
        "claude": "--dangerously-skip-permissions",
        "antigravity": "--dangerously-skip-permissions",
    }.get(cli_kind)
    if permission_flag and permission_flag not in base_args:
        base_args.append(permission_flag)
    # Every router-launched CLI gets Herald's tools (global groups, the
    # caller's project/part, its profile's groups, paired devices) through
    # one scoped `herald` MCP server -- see cli_gateway.py.
    from herald.router import cli_gateway
    if cli_kind and cli_gateway.enabled(config):
        gateway_scope = event_bus.get_scope()
        gateway_kwargs = {
            "project": gateway_scope.get("project"), "part": gateway_scope.get("part"),
            "profile": config.get("herald_profile"),
        }
        env.update(cli_gateway.scope_env(**gateway_kwargs))
        base_args = [*base_args, *cli_gateway.cli_args(cli_kind, **gateway_kwargs)]
    # A whole-CLI instance (cli_instances.py): its own workspace and instructions, and its native session
    # resumed so the CLI keeps its own history; only the new message (+ this turn's context) is sent.
    instance = event_bus.get_scope().get("cli_instance") if cli_kind else None
    resume_id = None
    if instance:
        from herald.router import cli_instances
        client = client.model_copy(update={"working_dir": cli_instances.workspace(
            instance["instance"], instance["parts"]["instructions"])})
        if cli_kind in cli_instances.RESUMABLE and instance.get("session"):
            resume_id = cli_instances.native_session(instance["instance"], instance["session"], cli_kind)
        prompt = cli_instances.turn_prompt(instance["parts"], fresh=resume_id is None)
    stream_stdin = None
    if cli_kind == "codex":
        # On Windows, codex is normally a .cmd shim. Passing a multiline
        # prompt as one argv value through that shim truncates it at the first
        # newline. Codex officially supports `-` to read the prompt from
        # stdin, which preserves the complete Admin/worker mission.
        codex_args = list(base_args)
        if "--skip-git-repo-check" not in codex_args:
            codex_args.append("--skip-git-repo-check")
        cmd = [resolved_exec, *client.executable[1:], *codex_args, "--json",
               *(["resume", resume_id] if resume_id else []), "-"]
    elif cli_kind == "claude":
        cmd = [resolved_exec, *client.executable[1:], *base_args, *(["--resume", resume_id] if resume_id else []),
               "-p", "--output-format", "stream-json", "--verbose", prompt]
    elif cli_kind == "antigravity":
        from .prompt_transport import antigravity_stream_input
        cmd, stream_stdin = antigravity_stream_input(
            [resolved_exec, *client.executable[1:]], base_args, prompt,
            output_schema=config.get('output_schema'),
        )
    else:
        cmd = [resolved_exec, *client.executable[1:], *client.internal_args, *client.config_args, "-p", prompt]

    if cli_kind:
        result = _call_cli_streaming(
            cmd, env, client, timeout, cli_kind,
            stdin_text=prompt if cli_kind == "codex" else stream_stdin,
            health_check_interval=config.get("health_check_interval"),
        )
        if instance and instance.get("session"):
            from herald.router import cli_instances
            if result.get("ok") and result.get("native_session_id"):
                cli_instances.remember_session(instance["instance"], instance["session"], cli_kind,
                                               result["native_session_id"])
            elif resume_id and not result.get("ok") and not result.get("native_tools_started"):
                # The native session is gone (pruned, other machine...): start it fresh with the history.
                cli_instances.forget_session(instance["instance"], instance["session"], cli_kind)
                return call_cli(config, prompt, timeout, working_dir=working_dir)
        if (cli_kind == "antigravity" and not result.get("ok") and not result.get("native_tools_started")
                and result.get("error_kind") != "auth" and config.get("model_fallback", True)
                and any(word in str(result.get("error", "")).lower() for word in ("quota", "rate limit", "resource_exhausted", "429"))):
            try:
                from .antigravity_models import available_fallbacks
                candidates = available_fallbacks([resolved_exec, *client.executable[1:]], env,
                                                 str(client.working_dir) if client.working_dir else None,
                                                 config.get("fallback_models", []))
            except Exception:
                candidates = []
            for model in candidates[:2]:
                event_bus.emit_nowait("agent.step", importance=.2, payload={**event_bus.get_scope(), "kind": "model.fallback", "tool_name": model, "status": "switching"}, source="antigravity_cli")
                attempt = call_cli({**config, "model": model, "model_fallback": False}, prompt, timeout, working_dir=working_dir)
                if attempt.get("ok"):
                    return {**attempt, "native_model": model, "fallback_used": True}
                result = attempt
                if attempt.get("native_tools_started") or attempt.get("error_kind") == "auth":
                    break
        return result

    try:
        # Explicit DEVNULL, not inherited stdin: a server process's stdin can be
        # closed/broken (detached background process), and some CLIs behave
        # differently -- including hard-failing auth/connector checks -- when
        # they inherit a bad stdin instead of a clean, well-defined one.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=env,
            cwd=str(client.working_dir) if client.working_dir else None,
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"{cmd[0]} not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{cmd[0]} timed out after {timeout}s"}
    if proc.returncode != 0:
        error_text = proc.stderr or proc.stdout
        # A failed call and an unauthenticated CLI look identical from stderr
        # alone (both are "some nonzero-exit text"). Rather than guess from
        # the text, ask the CLI's own dedicated status subcommand -- fast,
        # and already the authoritative signal the dashboard itself trusts.
        # Only on failure, not pre-flight, so the happy path pays no cost.
        try:
            from herald.router.cli_auth import check_one_status
            status = check_one_status(config.get("cli_name", ""))
        except Exception:  # noqa: BLE001 - a diagnostic must not mask the real failure
            status = None
        if status and status.get("status") == "logged_out":
            return {
                "ok": False, "error_kind": "auth",
                "error": f"{cmd[0]} appears logged out (status check: {status.get('detail', '')}); "
                         f"original error: {error_text}",
            }
        return {"ok": False, "error": error_text}
    if client.parser in ("antigravity_json", "claude_json"):
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"ok": True, "content": proc.stdout.strip(), "usage": _no_usage(), "thinking": None, "thinking_tokens": None}
        content = parsed.get("response") or parsed.get("result") or proc.stdout
        usage = parsed.get("usage") or {}
        cost_usd = parsed.get("total_cost_usd")  # claude_json only
        return {
            "ok": True, "content": content,
            "usage": {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cost_usd": cost_usd,
            },
            # Antigravity's JSON exposes a thinking TOKEN COUNT, never the
            # reasoning text itself -- so `thinking` stays None here rather
            # than fabricating content that was never actually returned.
            "thinking": None,
            "thinking_tokens": usage.get("thinking_tokens"),
        }
    return {"ok": True, "content": proc.stdout.strip(), "usage": _no_usage(), "thinking": None, "thinking_tokens": None}


def call_local_model(config: dict[str, Any], prompt: str, timeout: float = 120.0) -> dict[str, Any]:
    """config: {"base_url": "http://<node-tailscale-ip>:<port>/v1", "model": "<name>"}
    Points at an LM Studio or Ollama OpenAI-compatible server running on another
    configured node, reached directly over Tailscale -- no deploy/control
    plane needed for inference itself, only for load/unload (separate concern)."""
    base_url = config["base_url"].rstrip("/")
    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            json={"model": config["model"], "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": sanitize_error(exc)}
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return {"ok": False, "error": f"unexpected response shape: {data!r}"}
    usage = data.get("usage") or {}
    return {
        "ok": True, "content": content,
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "cost_usd": None,  # local compute -- no per-token dollar cost
        },
        # LM Studio surfaces reasoning under reasoning_content when a
        # thinking-capable model is loaded -- real text, when present.
        "thinking": (data["choices"][0]["message"].get("reasoning_content") or None),
        "thinking_tokens": None,
    }


def call_browser_session(config: dict[str, Any], prompt: str, timeout: float = 120.0) -> dict[str, Any]:
    """config: {"gateway_url": "http://host:port/v1", "model": "g4f-<account>/<model>"}
    Same OpenAI-compatible shape as call_local_model -- the g4f gateway already
    speaks this protocol, so this is nearly identical, kept as its own function
    since the two categories have different operational meaning (one is a real
    account with session-expiry risk, not a controllable local process)."""
    port = config.get("port", 4900)
    gateway_url = (config.get("gateway_url") or f"http://127.0.0.1:{port}/v1").rstrip("/")
    model_name = config.get("model") or config.get("account_name") or "g4f-auto"
    try:
        resp = httpx.post(
            f"{gateway_url}/chat/completions",
            json={"model": model_name, "messages": [{"role": "user", "content": prompt}]},
            timeout=httpx.Timeout(timeout, connect=3.0),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": sanitize_error(exc)}


    if "error" in data:
        # g4f's own failure shape (e.g. NoValidHarFileError) comes back as
        # 200 OK with an "error" body, not an HTTP error status.
        return {"ok": False, "error": data["error"].get("message", str(data["error"]))}
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return {"ok": False, "error": f"unexpected response shape: {data!r}"}
    usage = data.get("usage") or {}
    return {
        "ok": True, "content": content,
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "cost_usd": None,  # a real ChatGPT account, but not billed per-call/per-token
        },
        "thinking": None, "thinking_tokens": None,
    }


def call_image_gen(config: dict[str, Any], prompt: str, timeout: float = 60.0) -> dict[str, Any]:
    """Image generation backend adapter (Vertex AI / Gemini Imagen / OpenAI DALL-E / local SD).
    config: {"provider": "gemini"|"openai"|"local", "api_key": "...", "model_name": "imagen-3.0-generate-002"|...}
    Returns image generation URL or base64 markdown / JSON.
    """
    provider = config.get("provider", "gemini")
    api_key = config.get("api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    model_name = config.get("model_name", "imagen-3.0-generate-002")

    if provider in ("gemini", "google"):
        if not api_key:
            return {"ok": False, "error": "image_gen with gemini requires GEMINI_API_KEY"}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:predict?key={api_key}"
        body = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "sampleCount": config.get("sample_count", 1),
                "aspectRatio": config.get("aspect_ratio", "1:1"),
                "outputOptions": {"mimeType": "image/jpeg"},
            },
        }
        try:
            resp = httpx.post(url, json=body, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            predictions = data.get("predictions", [])
            if not predictions:
                return {"ok": False, "error": f"No image predictions returned: {data}"}
            b64_img = predictions[0].get("bytesBase64Encoded", "")
            return {
                "ok": True,
                "content": f"[Image generated via {model_name}; base64 payload size: {len(b64_img)} bytes]\ndata:image/jpeg;base64,{b64_img[:80]}...",
                "image_base64": b64_img,
                "format": "image/jpeg",
                "usage": _no_usage(),
                "thinking": None, "thinking_tokens": None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"gemini image_gen failed: {sanitize_error(exc)}"}

    if provider == "openai":
        if not api_key:
            return {"ok": False, "error": "image_gen with openai requires OPENAI_API_KEY"}
        url = "https://api.openai.com/v1/images/generations"
        try:
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": config.get("model_name", "dall-e-3"), "prompt": prompt, "n": 1, "size": "1024x1024"},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            img_url = data.get("data", [{}])[0].get("url", "")
            return {
                "ok": True,
                "content": f"![Generated Image]({img_url})",
                "image_url": img_url,
                "usage": _no_usage(),
                "thinking": None, "thinking_tokens": None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"openai image_gen failed: {sanitize_error(exc)}"}

    return {"ok": False, "error": f"unsupported image_gen provider '{provider}'"}


def call_gemini_image_edit(
    config: dict[str, Any], prompt: str, *,
    reference_images: list[bytes] | None = None, timeout: float = 90.0,
) -> dict[str, Any]:
    """Gemini 2.5/3.1 Flash Image ("Nano Banana") generation/editing.

    Unlike call_image_gen's Imagen path (pure text-to-image, no image input),
    this uses the multimodal generateContent endpoint so `reference_images`
    can be fed back in alongside the prompt -- this is what makes a LOCKED
    CHARACTER possible across many separate calls: pass the previous still
    (or an explicit character reference photo) as a reference_images entry
    and ask for "the same character, now: <next prompt>" rather than hoping
    a fresh unconditioned generation happens to look like the same person.

    config: {"api_key": "...", "model_name": "gemini-2.5-flash-image"}
    Returns the first generated image as raw bytes in "image_bytes", plus
    any text Gemini returned alongside it (safety notes, etc.) in "content".
    """
    api_key = config.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "gemini image edit requires GEMINI_API_KEY"}
    model_name = config.get("model_name", "gemini-2.5-flash-image")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    parts: list[dict[str, Any]] = []
    for image_bytes in (reference_images or []):
        parts.append({
            "inline_data": {
                "mime_type": "image/png",
                "data": base64.b64encode(image_bytes).decode("ascii"),
            },
        })
    parts.append({"text": prompt})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    try:
        resp = httpx.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"gemini image edit failed: {sanitize_error(exc)}"}

    candidates = data.get("candidates") or []
    if not candidates:
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        return {"ok": False, "error": f"no candidates returned (blockReason={block_reason})"}

    image_bytes: bytes | None = None
    text_out = ""
    for part in (candidates[0].get("content") or {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            image_bytes = base64.b64decode(inline["data"])
        elif part.get("text"):
            text_out += part["text"]

    if image_bytes is None:
        return {"ok": False, "error": f"response contained no image data: {text_out or data!r}"}

    return {
        "ok": True,
        "content": text_out,
        "image_bytes": image_bytes,
        "format": "image/png",
        "usage": _no_usage(),
        "thinking": None, "thinking_tokens": None,
    }


def call_video_gen(
    config: dict[str, Any], prompt: str, *,
    reference_image: bytes | None = None,
    poll_interval: float = 10.0, max_wait: float = 360.0,
) -> dict[str, Any]:
    """Veo video generation via the Gemini API's async predictLongRunning
    endpoint (confirmed reachable with a plain Gemini API key, not just
    Vertex AI/GCP-project auth -- see herald docs on this).

    config: {"api_key": "...", "model_name": "veo-3.1-fast-generate-preview",
             "aspect_ratio": "16:9"|"9:16", "resolution": "720p"|"1080p"|"4k",
             "duration_seconds": "4"|"6"|"8"}

    Dialogue/narration isn't a separate parameter -- Veo reads quoted speech
    directly out of `prompt` and synchronizes audio/lip movement to it, so
    callers wanting the character to speak an exact script should embed it
    in `prompt` as a quoted line (this is exactly the workflow PDF's own
    Stage 2 "Script section: '...'" format).

    `reference_image` (optional) locks the starting frame for image-to-video,
    e.g. the matching Stage-1 still, keeping this clip visually anchored to
    the same generated character/scene rather than starting from nothing.

    Real generation costs money and takes 11s-6min per Google's own latency
    figures -- `max_wait` bounds how long this call will block polling
    before giving up (the operation itself keeps running server-side either
    way; a caller can re-poll the returned `operation_name` later).
    """
    api_key = config.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "video_gen requires GEMINI_API_KEY"}
    model_name = config.get("model_name", "veo-3.1-fast-generate-preview")
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    instance: dict[str, Any] = {"prompt": prompt}
    if reference_image is not None:
        instance["image"] = {
            "inlineData": {
                "mimeType": "image/png",
                "data": base64.b64encode(reference_image).decode("ascii"),
            },
        }
    parameters = {
        "aspectRatio": config.get("aspect_ratio", "16:9"),
        "resolution": config.get("resolution", "720p"),
        "durationSeconds": str(config.get("duration_seconds", "8")),
    }
    submit_url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:predictLongRunning"
    )
    try:
        resp = httpx.post(
            submit_url, headers=headers,
            json={"instances": [instance], "parameters": parameters}, timeout=30,
        )
        resp.raise_for_status()
        operation_name = resp.json().get("name")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"veo submission failed: {sanitize_error(exc)}"}
    if not operation_name:
        return {"ok": False, "error": "veo submission returned no operation name"}

    poll_url = f"https://generativelanguage.googleapis.com/v1beta/{operation_name}"
    waited = 0.0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            poll_resp = httpx.get(poll_url, headers=headers, timeout=30)
            poll_resp.raise_for_status()
            status = poll_resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"veo poll failed: {sanitize_error(exc)}", "operation_name": operation_name}
        if status.get("done"):
            if "error" in status:
                return {"ok": False, "error": f"veo generation failed: {status['error']}", "operation_name": operation_name}
            try:
                sample = status["response"]["generateVideoResponse"]["generatedSamples"][0]["video"]
            except (KeyError, IndexError):
                return {"ok": False, "error": f"unexpected veo response shape: {status!r}", "operation_name": operation_name}
            video_uri, mime_type = sample.get("uri"), sample.get("mimeType", "video/mp4")
            if not video_uri:
                return {"ok": False, "error": "veo response had no video uri", "operation_name": operation_name}
            try:
                video_resp = httpx.get(video_uri, headers={"x-goog-api-key": api_key}, timeout=120)
                video_resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"veo video download failed: {sanitize_error(exc)}", "operation_name": operation_name}
            return {
                "ok": True,
                "content": f"[Veo video generated via {model_name}, {len(video_resp.content)} bytes]",
                "video_bytes": video_resp.content,
                "format": mime_type,
                "operation_name": operation_name,
                "usage": _no_usage(),
                "thinking": None, "thinking_tokens": None,
            }
    return {
        "ok": False,
        "error": f"veo generation still running after {max_wait}s; poll operation_name later",
        "operation_name": operation_name,
    }


def call_omni_generate(
    config: dict[str, Any], prompt: str, *,
    reference_image: bytes | None = None, timeout: float = 120.0,
) -> dict[str, Any]:
    """Gemini Omni Flash video+audio generation via Google's newer
    "Interactions API" (v1beta/interactions) -- confirmed live and working
    with a plain Gemini API key: one synchronous call returns a finished
    video WITH synced audio (no async predictLongRunning/poll loop like
    call_video_gen's Veo path).

    Unlike Veo, audio here isn't quoted dialogue parsed out of the prompt --
    just describe what the character should say in plain language and the
    model narrates it directly.

    config: {"api_key": "...", "model_name": "gemini-omni-flash-preview"}
    `reference_image` (optional) confirmed live to lock onto an existing
    character/scene for image-to-video, same purpose as call_video_gen's
    parameter of the same name.

    Real generation, real cost, separate from any Google One/Gemini app
    subscription -- confirmed via Google's own docs that subscription
    credits (Google Flow, Gemini app) do NOT extend to API/AI Studio
    billing. Every caller-facing surface using this must say so.
    """
    api_key = config.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "omni generation requires GEMINI_API_KEY"}
    model_name = config.get("model_name", "gemini-omni-flash-preview")
    url = f"https://generativelanguage.googleapis.com/v1beta/interactions?key={api_key}"

    input_steps: list[dict[str, Any]] = []
    if reference_image is not None:
        input_steps.append({
            "type": "image",
            "mime_type": "image/png",
            "data": base64.b64encode(reference_image).decode("ascii"),
        })
    input_steps.append({"type": "text", "text": prompt})

    try:
        resp = httpx.post(url, json={"model": f"models/{model_name}", "input": input_steps}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"omni generation failed: {sanitize_error(exc)}"}

    video_bytes: bytes | None = None
    text_out = ""
    for step in data.get("steps", []):
        for item in step.get("content", []) or []:
            if item.get("type") == "video" and item.get("data"):
                video_bytes = base64.b64decode(item["data"])
            elif item.get("type") == "text" and item.get("text"):
                text_out += item["text"]

    if video_bytes is None:
        return {"ok": False, "error": f"omni response contained no video data: {text_out or data!r}"}

    usage = data.get("usage") or {}
    return {
        "ok": True,
        "content": text_out,
        "video_bytes": video_bytes,
        "format": "video/mp4",
        "usage": {
            "input_tokens": usage.get("total_input_tokens"),
            "output_tokens": usage.get("total_output_tokens"),
            "cost_usd": None,
        },
        "thinking": None, "thinking_tokens": None,
    }


ADAPTERS = {
    "api_key": call_api_key,
    "cli": call_cli,
    "local_model": call_local_model,
    "browser_session": call_browser_session,
    "image_gen": call_image_gen,
    "video_gen": call_video_gen,
    "omni_gen": call_omni_generate,
}
