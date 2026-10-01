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


def _parse_codex_line(parsed: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """codex exec --json: {"type": "item.started"/"item.completed"/
    "turn.completed"/"error"/..., "item": {"type": "command_execution"/
    "agent_message", "command", "status", "exit_code", "text"}}"""
    event_type = parsed.get("type", "?")
    item = parsed.get("item", {})
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
    }


def _parse_claude_line(parsed: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """claude -p --output-format stream-json: {"type": "assistant"/"user"/
    "result"/"system", "message": {"content": [{"type": "tool_use"/
    "tool_result"/"text", "name", "input", "content", "text"}]}}"""
    mtype = parsed.get("type", "?")
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
        return {"kind": "tool_use", "name": block.get("name"), "input": block.get("input")}
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
    if etype == "result":
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
    if step_type == "tool":
        state["native_tools_started"] = state.get("native_tools_started", 0) + 1
    if step_type == "agent_response" and step.get("text_delta"):
        state.setdefault("response_deltas", []).append(step["text_delta"])
    tool_error = step.get("state") == "ERROR" or bool((step.get("tool_info") or {}).get("error"))
    if step_type == "tool" and tool_error:
        info = step.get("tool_info") or {}
        error = info.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        state.setdefault("stderr_lines", []).append(
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
    cmd: list[str], env: dict[str, str], client: Any, timeout: int, cli_kind: str,
    *, stdin_text: str | None = None,
) -> dict[str, Any]:
    """Run a CLI in its own real-time JSONL streaming mode (codex exec
    --json / claude --output-format stream-json / agy --output-format
    stream-json) with a live-reading Popen loop instead of a single
    blocking subprocess.run(), emitting one event_bus `agent.step` per
    line AS IT ARRIVES -- real live visibility into what each CLI is
    actually doing internally (shell commands, file edits, tool calls),
    not just Herald's own orchestration wrapper around the call. Tagged
    with event_bus.get_scope() (project/part), set by the request handler
    in server.py before dispatching here.

    Falls back to the old accumulate-everything-then-return behavior for
    the actual return value (content/usage) -- streaming only changes
    *when* the data becomes visible (live, via events), not the final
    contract callers of call_cli() already depend on.
    """
    from herald.router import event_bus

    parse_line = _STREAMING_PARSERS[cli_kind]
    scope = event_bus.get_scope()
    state: dict[str, Any] = {}
    deadline = time.monotonic() + timeout

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            env=env, bufsize=1,
            cwd=str(client.working_dir) if client.working_dir else None,
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"{cmd[0]} not found on PATH"}

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            proc.kill()
            return {"ok": False, "error": f"could not send prompt to {cli_kind} stdin: {sanitize_error(exc)}"}

    # proc.stdout.readline() is a blocking call with no timeout of its own --
    # checking `deadline` only between readline() calls does NOT actually
    # enforce the timeout if the CLI goes silent (an internal hang, a stuck
    # network call inside its own process) and never produces another line:
    # the thread just parks inside the blocking read forever and the check
    # code never runs again. Confirmed live -- this is what several
    # multi-hour "stuck" native CLI sessions turned out to be, not
    # legitimately slow work. Read on a background daemon thread instead and
    # consume lines through a queue with a real timeout, so a silent CLI
    # gets detected and killed on schedule instead of blocking forever.
    line_queue: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            for raw_line in proc.stdout:
                line_queue.put(raw_line)
        except Exception:
            pass
        finally:
            line_queue.put(None)  # sentinel: stdout closed / process done

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    # stderr was previously only read in one blocking chunk after the
    # process exited or was killed on timeout -- meaning a stuck CLI (an
    # internal hang, a blocked handshake to its own MCP subprocess, a stalled
    # network call) was completely invisible for the entire timeout window.
    # Confirmed live 2026-09-01: three swarm workers sat idle for 30+ minutes
    # with zero visibility into why. Stream stderr the same way as stdout --
    # line-by-line, live, via event_bus -- so a stuck worker's own diagnostic
    # output shows up as it happens instead of only after the kill.
    stderr_lines_buf: list[str] = []

    def _stderr_reader() -> None:
        try:
            for raw_line in proc.stderr:
                stderr_lines_buf.append(raw_line)
                stripped = raw_line.strip()
                if stripped:
                    # event_bus is in-process only -- an SSE client attached to
                    # the router server (a separate process from a standalone
                    # `herald swarm` CLI run) never sees these. Log directly
                    # too so a CLI-launched swarm/admin run is watchable in its
                    # own terminal/log file, not just via /event-bus/stream.
                    _logger.info("[%s stderr] %s", cli_kind, stripped)
                    event_bus.emit_nowait(
                        "agent.stderr", importance=0.1,
                        payload={**scope, "line": stripped}, source=f"{cli_kind}_cli",
                    )
        except Exception:
            pass

    stderr_reader_thread = threading.Thread(target=_stderr_reader, daemon=True)
    stderr_reader_thread.start()

    try:
        while True:
            if env.get("HERALD_ADMIN_CALL") == "1":
                try:
                    from herald.router.admin_control import stop_requested
                    if stop_requested():
                        proc.kill()
                        proc.wait(timeout=5)
                        return {
                            "ok": False, "error_kind": "cancelled",
                            "error": "Admin execution stopped by operator",
                            "partial_content": _partial_content(state),
                        }
                except Exception:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                return {
                    "ok": False, "error": f"{cmd[0]} timed out after {timeout}s "
                    "(no output for the full timeout window -- likely an internal hang)",
                    "partial_content": _partial_content(state),
                }
            try:
                line = line_queue.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                step = parse_line(parsed, state)
                if step:
                    # Same reasoning as the stderr logging above: event_bus is
                    # in-process only, so a standalone `herald swarm`/admin CLI
                    # run needs its own direct log line to be trackable in its
                    # own terminal/log file.
                    step_desc = step.get("summary") or step.get("kind") or step.get("type") or str(step)[:200]
                    _logger.info("[%s step] %s", cli_kind, step_desc)
                    event_bus.emit_nowait(
                        "agent.step", importance=0.2, payload={**scope, **step}, source=f"{cli_kind}_cli",
                    )
            except Exception:
                pass
        # EOF on the stdout pipe does not update Popen.returncode. Reap the
        # child explicitly so a fast CLI crash cannot masquerade as an empty
        # successful response while its real error sits unread on stderr.
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            return {
                "ok": False,
                "error": f"{cmd[0]} did not exit after closing stdout",
                "partial_content": _partial_content(state),
            }
    finally:
        stderr_reader_thread.join(timeout=5)
        stderr_tail = "".join(stderr_lines_buf)
        proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    stderr_lines = state.get("stderr_lines") or []
    if proc.returncode != 0 or stderr_lines:
        diagnostics = list(filter(None, stderr_lines))
        native_stderr = sanitize_error(stderr_tail.strip())
        if native_stderr and native_stderr not in diagnostics:
            diagnostics.append(native_stderr[-4000:])
        error_text = "; ".join(diagnostics) or f"exit code {proc.returncode}"
        try:
            from herald.router.cli_auth import check_one_status
            status = check_one_status(cli_kind if cli_kind != "antigravity" else "antigravity")
        except Exception:  # noqa: BLE001
            status = None
        if status and status.get("status") == "logged_out":
            return {
                "ok": False, "error_kind": "auth",
                "error": f"{cli_kind} appears logged out; original error: {error_text}",
                "partial_content": _partial_content(state),
            }
        return {"ok": False, "error": error_text, "partial_content": _partial_content(state),
                "native_tools_started": state.get("native_tools_started", 0)}

    agent_messages = state.get("agent_messages") or []
    usage = state.get("usage") or {}
    if cli_kind == "antigravity":
        content = state.get("result_response") or "".join(state.get("response_deltas") or [])
    else:
        content = agent_messages[-1] if agent_messages else ""
    if not content.strip():
        return {
            "ok": False,
            "error": f"{cli_kind} exited successfully but returned no assistant response",
            "partial_content": content or _partial_content(state),
        }
    return {
        "ok": True, "content": content,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cost_usd": state.get("cost_usd"),
        },
        "thinking": None, "thinking_tokens": usage.get("reasoning_output_tokens") or usage.get("thinking_tokens"),
    }


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
        depth = event_bus.get_scope().get("consult_depth")
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
    if cli_kind == "codex":
        # On Windows, codex is normally a .cmd shim. Passing a multiline
        # prompt as one argv value through that shim truncates it at the first
        # newline. Codex officially supports `-` to read the prompt from
        # stdin, which preserves the complete Admin/worker mission.
        codex_args = list(base_args)
        if "--skip-git-repo-check" not in codex_args:
            codex_args.append("--skip-git-repo-check")
        cmd = [resolved_exec, *client.executable[1:], *codex_args, "--json", "-"]
    elif cli_kind == "claude":
        cmd = [resolved_exec, *client.executable[1:], *base_args,
               "-p", "--output-format", "stream-json", "--verbose", prompt]
    elif cli_kind == "antigravity":
        # agy's --print/-p/--prompt all greedily take the very next arg as
        # their value (confirmed empirically -- not a boolean flag like
        # codex/claude's -p/--print) -- the prompt must be immediately
        # adjacent to --print, so --output-format has to come before it.
        cmd = [resolved_exec, *client.executable[1:], *base_args,
               "--output-format", "stream-json", "--print", prompt]
    else:
        cmd = [resolved_exec, *client.executable[1:], *client.internal_args, *client.config_args, "-p", prompt]

    if cli_kind:
        result = _call_cli_streaming(
            cmd, env, client, timeout, cli_kind,
            stdin_text=prompt if cli_kind == "codex" else None,
        )
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
                event_bus.emit_nowait("agent.step", payload={**event_bus.get_scope(), "kind": "model.fallback", "tool_name": model, "status": "switching"}, source="antigravity_cli")
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
