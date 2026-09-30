"""Structured / sandboxed / tool-scoped CLI agent execution.

Generalizes the schema-constrained, sandboxed, tool-allowlisted call
pattern that projects previously had to hand-roll themselves via direct
subprocess calls to their own local codex/claude binaries -- Herald is the
router, so this logic belongs here once, not duplicated per project.
ficsit-command's app/cli_runner.py was the first thing ported onto this.

Distinct from adapters.call_cli, which is a bare `<cli> -p <prompt>` text
wrapper used for simple model routing and has no notion of structured
output or tool scoping. This module drives the same codex/claude binaries
with real --output-schema/--json-schema, --sandbox, and tool-allowlist
flags -- the caller decides what tools, if any, a call may use (via
`allowed_tools`); nothing here grants tool access beyond what's passed in,
and an empty/None list means no tool access at all.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from . import telemetry

_BIN_CACHE: dict[str, str | None] = {}


def _binary(cli: str) -> str | None:
    if cli not in _BIN_CACHE:
        _BIN_CACHE[cli] = shutil.which(cli)
    return _BIN_CACHE[cli]


def _extract_codex(stdout: str) -> tuple[str, str | None]:
    text = ""
    thread_id: str | None = None
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        item = event.get("item") or {}
        if (
            event.get("type") == "item.completed"
            and item.get("type") == "agent_message"
            and item.get("text")
        ):
            text = str(item["text"])
    return text.strip(), thread_id


def _extract_claude(stdout: str) -> tuple[str, str | None, Any | None]:
    payload = json.loads(stdout)
    structured = payload.get("structured_output")
    text = str(payload.get("result") or "")
    return text.strip(), payload.get("session_id"), structured


async def run(
    cli: str,
    prompt: str,
    *,
    schema: dict[str, Any] | None = None,
    allowed_tools: list[str] | None = None,
    sandbox: bool = True,
    max_turns: int = 20,
    timeout: float = 360,
) -> dict[str, Any]:
    """Run one structured/sandboxed/tool-scoped agent turn.

    `allowed_tools=None`/`[]` means no tool access at all -- the
    schema-constrained, read-only call shape. A non-empty list is the
    caller's own curated allowlist, enforced via the CLI's own flag; this
    never widens whatever list the caller passed.
    """
    if cli not in ("codex", "claude"):
        return {"ok": False, "error": f"unsupported cli: {cli}"}
    binary = _binary(cli)
    if not binary:
        return {"ok": False, "error": f"{cli} not found on PATH"}

    schema_path: str | None = None
    if cli == "codex":
        args = [binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check"]
        if sandbox:
            args.extend(["--sandbox", "read-only"])
        if not allowed_tools:
            args.append("--ignore-user-config")
        if schema:
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                prefix="herald-schema-",
                delete=False,
                encoding="utf-8",
            )
            with handle:
                json.dump(schema, handle)
            schema_path = handle.name
            args.extend(["--output-schema", schema_path])
        args.append("-")
    else:
        args = [
            binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--max-turns",
            str(max_turns),
        ]
        if allowed_tools:
            args.extend(["--tools", "", "--allowedTools", *allowed_tools])
        else:
            args.extend(["--tools", "", "--strict-mcp-config"])
        if schema:
            args.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])

    start = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(prompt.encode()), timeout
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        if schema_path:
            Path(schema_path).unlink(missing_ok=True)
        return {"ok": False, "error": f"{cli} timed out after {int(timeout)}s"}
    finally:
        if schema_path:
            Path(schema_path).unlink(missing_ok=True)
    duration_ms = int((time.monotonic() - start) * 1000)

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    if process.returncode:
        detail = (stderr.strip() or stdout.strip() or "unknown CLI error")[-2000:]
        telemetry.log_call(
            backend_name=cli,
            backend_type="cli_agent",
            prompt=prompt,
            success=False,
            duration_ms=duration_ms,
            error=detail,
        )
        return {"ok": False, "error": f"{cli} CLI failed: {detail}"}

    if cli == "codex":
        text, session_id = _extract_codex(stdout)
        structured: Any | None = None
    else:
        text, session_id, structured = _extract_claude(stdout)

    if not text and structured is None:
        telemetry.log_call(
            backend_name=cli,
            backend_type="cli_agent",
            prompt=prompt,
            success=False,
            duration_ms=duration_ms,
            error="empty result",
        )
        return {"ok": False, "error": f"{cli} CLI returned no assistant result"}

    if schema and structured is None:
        try:
            structured = json.loads(text)
        except json.JSONDecodeError:
            telemetry.log_call(
                backend_name=cli,
                backend_type="cli_agent",
                prompt=prompt,
                success=False,
                duration_ms=duration_ms,
                error="invalid structured output",
            )
            return {"ok": False, "error": f"{cli} returned invalid structured output"}

    telemetry.log_call(
        backend_name=cli,
        backend_type="cli_agent",
        prompt=prompt,
        success=True,
        duration_ms=duration_ms,
        content=text,
    )
    return {
        "ok": True,
        "cli": cli,
        "text": text,
        "structured": structured,
        "session_id": session_id,
    }
