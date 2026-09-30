"""Herald's bundled cross-platform shell MCP server."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from herald.coding_tools import _parse_build_log, _parse_test_output, _pid_alive

# run_command is capped at 600s. That's too short for real build/package steps
# (an Unreal Engine mod build routinely takes 10-15+ minutes), so long jobs
# must go through run_command_background + check_command instead: they start
# detached, return a job_id immediately, and the caller polls to completion.
# Never report a long-running step as successful without polling it to
# done=True and reading the real exit_code.
_JOBS: dict[str, dict[str, Any]] = {}


def _persist_job_index(root: Path) -> None:
    """Same rationale as coding_tools._persist_job_index: a Popen handle
    can't survive a process restart, so persist just enough metadata (log
    path, PID, done/exit_code once known) that a restarted server can still
    find the job's log and check PID liveness instead of losing it outright."""
    snapshot = {
        job_id: {
            "log_path": str(job["log_path"]), "pid": job["proc"].pid,
            "started_at": job["started_at"], "done": job["done"], "exit_code": job["exit_code"],
        }
        for job_id, job in _JOBS.items()
    }
    try:
        index_path = root / ".herald_jobs" / "index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(snapshot), encoding="utf-8")
    except OSError:
        pass


def _shell_command(shell: str, command: str) -> list[str]:
    selected = shell
    if selected == "auto":
        selected = "powershell" if os.name == "nt" else "bash"
    if selected == "bash":
        candidates = []
        if os.name == "nt":
            candidates.extend([
                Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "usr" / "bin" / "bash.exe",
            ])
        executable = next((str(path) for path in candidates if path.is_file()), None) or shutil.which("bash")
        if not executable:
            raise RuntimeError("bash is not installed")
        return [executable, "-lc", command]
    if selected == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise RuntimeError("PowerShell is not installed")
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    if selected == "cmd":
        executable = os.environ.get("ComSpec") or shutil.which("cmd")
        if not executable:
            raise RuntimeError("cmd is not installed")
        return [executable, "/d", "/s", "/c", command]
    raise ValueError("shell must be auto, bash, powershell, or cmd")


def build_server(root: Path) -> Server:
    root = root.resolve()
    server = Server(
        "herald-native-shell", version="0.2.0",
        instructions=f"Command execution rooted at {root}. This is an execution tool, not an OS sandbox.",
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="run_command",
                title="Run shell command",
                description=(
                    "Run a command with Bash, PowerShell, cmd, or the platform default. "
                    "Capped at 600s -- for builds, packaging, or test suites that might run "
                    "longer, use run_command_background and poll it with check_command instead."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "shell": {"type": "string", "enum": ["auto", "bash", "powershell", "cmd"], "default": "auto"},
                        "cwd": {"type": "string", "description": "Working directory under the configured root"},
                        "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 120},
                    },
                    "required": ["command"],
                },
            ),
            types.Tool(
                name="which",
                title="Find executable",
                description="Resolve an executable available to the Herald process.",
                inputSchema={
                    "type": "object", "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            ),
            types.Tool(
                name="run_command_background",
                title="Run shell command in background",
                description=(
                    "Launch a long-running command (builds, packaging, test suites) without "
                    "blocking. Use this instead of run_command whenever a step might take "
                    "longer than a couple of minutes -- run_command's 600s cap will otherwise "
                    "return a timeout with no useful result, well before slow steps like an "
                    "Unreal Engine build finish. Poll the returned job_id with check_command "
                    "until done=true, then read its exit_code -- do not report success before that."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "shell": {"type": "string", "enum": ["auto", "bash", "powershell", "cmd"], "default": "auto"},
                        "cwd": {"type": "string", "description": "Working directory under the configured root"},
                    },
                    "required": ["command"],
                },
            ),
            types.Tool(
                name="run_tests",
                title="Run tests with a structured pass/fail summary",
                description=(
                    "Run a test command and get back a structured pass/fail summary "
                    "(pytest, dotnet test, jest/npm test, go test are recognized) instead "
                    "of raw text to parse yourself. If parsed=false, no framework's summary "
                    "line was recognized -- treat that as inconclusive, not a pass."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "shell": {"type": "string", "enum": ["auto", "bash", "powershell", "cmd"], "default": "auto"},
                        "cwd": {"type": "string", "description": "Working directory under the configured root"},
                        "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 300},
                    },
                    "required": ["command"],
                },
            ),
            types.Tool(
                name="check_build_log",
                title="Extract structured errors from a compile log",
                description=(
                    "Parse an MSVC/UBT or GCC/Clang compile log into deduplicated "
                    "{file, line, col, severity, code, message} entries plus a "
                    "build_result (success/failed/unknown -- never guessed). Give exactly "
                    "one of text, job_id (a run_command_background job), or path."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "job_id": {"type": "string"},
                        "path": {"type": "string", "description": "Path under the configured root"},
                        "max_errors": {"type": "number", "minimum": 1, "maximum": 500, "default": 50},
                    },
                },
            ),
            types.Tool(
                name="check_command",
                title="Poll a background command",
                description=(
                    "Poll a job started by run_command_background. Report a job as successful "
                    "only after this shows done=true and exit_code==0 -- a job still running, "
                    "or one you haven't polled at all, is not evidence of anything."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "tail_chars": {"type": "number", "minimum": 200, "maximum": 100000, "default": 4000},
                    },
                    "required": ["job_id"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        if name == "which":
            executable = shutil.which(str(arguments["name"]))
            return {"found": executable is not None, "path": executable}
        if name == "check_command":
            job_id = str(arguments["job_id"])
            job = _JOBS.get(job_id)
            tail_chars = int(arguments.get("tail_chars", 4000))
            if job is None:
                # Not in this process's memory: either a bad job_id, or this
                # process restarted since the job launched. No Popen handle
                # survives a restart, so the real exit code genuinely cannot
                # be recovered -- report the log (still on disk) and real PID
                # liveness instead of fabricating done=true/exit_code=0.
                try:
                    index = json.loads((root / ".herald_jobs" / "index.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    index = {}
                entry = index.get(job_id)
                if entry is None:
                    raise ValueError(f"unknown job_id '{job_id}'")
                log_path = Path(entry["log_path"])
                try:
                    text = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    text = f"[error reading log: {exc}]"
                if entry["done"]:
                    status = "done (recorded before a process restart)"
                elif _pid_alive(entry["pid"]):
                    status = "running (PID alive, but tracking process restarted; exit_code unknown until re-discovered)"
                else:
                    status = "unknown (PID no longer running, but no exit_code was recorded before a restart -- inspect output_tail yourself)"
                return {
                    "status": status,
                    "elapsed_sec": round(time.time() - entry["started_at"], 1),
                    "exit_code": entry["exit_code"],
                    "log_path": str(log_path),
                    "output_tail": text[-tail_chars:],
                }
            proc: subprocess.Popen = job["proc"]
            ret = proc.poll()
            if ret is not None and not job["done"]:
                job["done"] = True
                job["exit_code"] = ret
                _persist_job_index(root)
            elapsed = time.time() - job["started_at"]
            try:
                text = job["log_path"].read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                text = f"[error reading log: {exc}]"
            return {
                "status": "done" if job["done"] else "running",
                "elapsed_sec": round(elapsed, 1),
                "exit_code": job["exit_code"],
                "log_path": str(job["log_path"]),
                "output_tail": text[-tail_chars:],
            }

        if name == "check_build_log":
            text = str(arguments.get("text") or "")
            job_id = str(arguments.get("job_id") or "")
            rel_path = str(arguments.get("path") or "")
            sources = [bool(text), bool(job_id), bool(rel_path)]
            if sum(sources) != 1:
                raise ValueError("give exactly one of: text, job_id, path")
            if job_id:
                job = _JOBS.get(job_id)
                if job is None:
                    raise ValueError(f"unknown job_id '{job_id}'")
                text = job["log_path"].read_text(encoding="utf-8", errors="replace")
            elif rel_path:
                log_path = (root / rel_path).resolve()
                try:
                    log_path.relative_to(root)
                except ValueError as exc:
                    raise ValueError(f"path must remain under {root}") from exc
                text = log_path.read_text(encoding="utf-8", errors="replace")
            return _parse_build_log(text, max_errors=int(arguments.get("max_errors", 50)))

        if name == "run_tests":
            cwd = (root / str(arguments.get("cwd") or ".")).resolve()
            try:
                cwd.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"working directory must remain under {root}") from exc
            if not cwd.is_dir():
                raise ValueError(f"working directory does not exist: {cwd}")
            timeout = min(max(float(arguments.get("timeout_seconds", 300)), 1), 600)
            try:
                process = await asyncio.to_thread(
                    subprocess.run,
                    _shell_command(str(arguments.get("shell", "auto")), str(arguments["command"])),
                    cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                    errors="replace",
                )
            except subprocess.TimeoutExpired:
                return {
                    "parsed": False,
                    "error": f"test command timed out after {timeout}s -- use run_command_background "
                             "for a suite this slow; a timeout is not a pass or a fail.",
                }
            combined = (process.stdout or "") + "\n" + (process.stderr or "")
            summary = _parse_test_output(combined)
            tail = "\n".join(combined.strip().splitlines()[-40:])
            if summary is None:
                return {"parsed": False, "exit_code": process.returncode, "output_tail": tail}
            verdict = "pass" if summary["failed"] == 0 and process.returncode == 0 else "fail"
            return {"parsed": True, "verdict": verdict, "exit_code": process.returncode,
                     **summary, "output_tail": tail}

        if name == "run_command_background":
            cwd = (root / str(arguments.get("cwd") or ".")).resolve()
            try:
                cwd.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"working directory must remain under {root}") from exc
            if not cwd.is_dir():
                raise ValueError(f"working directory does not exist: {cwd}")
            job_id = uuid.uuid4().hex[:12]
            log_dir = root / ".herald_jobs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{job_id}.log"
            log_file = open(log_path, "w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(
                _shell_command(str(arguments.get("shell", "auto")), str(arguments["command"])),
                cwd=str(cwd), stdout=log_file, stderr=subprocess.STDOUT, text=True,
            )
            _JOBS[job_id] = {
                "proc": proc, "log_path": log_path, "log_file": log_file,
                "started_at": time.time(), "done": False, "exit_code": None,
            }
            _persist_job_index(root)
            return {
                "job_id": job_id,
                "pid": proc.pid,
                "log_path": str(log_path),
                "note": "Poll with check_command until done=true before drawing any conclusion about success.",
            }

        if name != "run_command":
            raise ValueError(f"unknown native shell tool '{name}'")
        cwd = (root / str(arguments.get("cwd") or ".")).resolve()
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"working directory must remain under {root}") from exc
        if not cwd.is_dir():
            raise ValueError(f"working directory does not exist: {cwd}")
        timeout = min(max(float(arguments.get("timeout_seconds", 120)), 1), 600)
        process = await asyncio.to_thread(
            subprocess.run,
            _shell_command(str(arguments.get("shell", "auto")), str(arguments["command"])),
            cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            errors="replace",
        )
        return {
            "exit_code": process.returncode,
            "stdout": process.stdout[-100_000:],
            "stderr": process.stderr[-100_000:],
            "cwd": str(cwd),
        }

    return server


async def _run(root: Path) -> None:
    server = build_server(root)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options(),
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Herald native shell MCP server")
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    asyncio.run(_run(Path(args.root)))


if __name__ == "__main__":
    main()
