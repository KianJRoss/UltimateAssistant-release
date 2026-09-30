"""Herald built-in coding tools -- an MCP server that gives any agentic session
the file I/O, shell execution, and code-search primitives needed to act as a
coding assistant.

All file/directory paths are resolved relative to the *workspace root* and
validated against it so a model cannot escape the working tree.  The workspace
root is set by the ``HERALD_WORKSPACE`` environment variable; it falls back to
the process working directory at import time, then to the user's home directory.

Transport: stdio (launched as a subprocess by Herald's bootstrap).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Workspace root
# ---------------------------------------------------------------------------

def _resolve_workspace() -> Path:
    env = os.environ.get("HERALD_WORKSPACE", "").strip()
    if env:
        candidate = Path(env).expanduser().resolve()
        if candidate.exists():
            return candidate
    cwd = Path.cwd().resolve()
    if str(cwd) not in {"/", "C:\\"}:
        return cwd
    return Path.home().resolve()


WORKSPACE = _resolve_workspace()
MAX_READ_BYTES = 256 * 1024    # 256 KB per file read
MAX_OUTPUT_BYTES = 128 * 1024  # 128 KB for command stdout/stderr

mcp = FastMCP("herald-coding")


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _safe_path(raw: str, must_exist: bool = False) -> Path:
    """Resolve *raw* relative to WORKSPACE and reject traversal attempts."""
    expanded = os.path.expanduser(raw) if raw.startswith("~") else raw
    p = Path(expanded)
    if not p.is_absolute():
        p = WORKSPACE / p
    p = p.resolve()
    if WORKSPACE not in (p, *p.parents):
        raise ValueError(
            f"Path '{raw}' resolves outside the workspace root "
            f"({WORKSPACE}).  Use a relative path or a path inside the workspace."
        )
    if must_exist and not p.exists():
        raise FileNotFoundError(f"'{p}' does not exist")
    return p


def _trunc(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="replace") + (
        f"\n\n[...truncated -- {len(encoded) - limit} bytes omitted]"
    )


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

@mcp.tool()
def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Read a file (or a line range) from the workspace.

    Args:
        path:       Path to the file, relative to the workspace root.
        start_line: First line to return, 1-indexed (default 1).
        end_line:   Last line to return, inclusive (default: all lines).

    Returns:
        File content as text, possibly truncated if very large.
    """
    try:
        p = _safe_path(path, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return f"[error] {exc}"
    if p.is_dir():
        return f"[error] '{path}' is a directory -- use list_directory instead"
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return f"[error] cannot read '{path}': {exc}"
    sample = raw[:8192]
    non_text = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
    if non_text / max(len(sample), 1) > 0.05:
        return f"[error] '{path}' appears to be a binary file; reading skipped"
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    start = max(1, start_line) - 1
    stop = min(end_line if end_line else len(lines), len(lines))
    return _trunc("".join(lines[start:stop]), MAX_READ_BYTES)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write (overwrite) an entire file in the workspace.

    Creates parent directories as needed. Writing the file is not the same
    as the task being done -- if a build/test step is available, run it and
    read the real result before reporting success.

    Args:
        path:    Path to the file, relative to the workspace root.
        content: Full new content of the file.

    Returns:
        Confirmation with byte/line counts, or an error.
    """
    try:
        p = _safe_path(path)
    except ValueError as exc:
        return f"[error] {exc}"
    if p.is_dir():
        return f"[error] '{path}' is an existing directory"

    from herald.router.approval_gate import gate_call, CeilingExceeded, ApprovalPending
    try:
        gate_call("write_file", {"path": path, "content": content}, preview=content[:2000])
    except CeilingExceeded as exc:
        return f"[error] {exc}"
    except ApprovalPending as exc:
        return f"[pending approval] {exc}"

    return _do_write_file(path, content)


def _do_write_file(path: str, content: str) -> str:
    """The actual write, past the gate. Callable directly by an approval
    executor (which reconstructs args from a decided ApprovalStore row) so
    approving a pending write doesn't re-trigger gate_call and re-pend."""
    p = _safe_path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Wrote {len(content.encode())} bytes ({lines} lines) to {p.relative_to(WORKSPACE)}"
    except OSError as exc:
        return f"[error] cannot write '{path}': {exc}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

def _closest_match_hint(original: str, old_content: str, context_lines: int = 2) -> str:
    """Best-effort diagnostic for a failed edit_file match.

    Never auto-applies a fuzzy match -- a wrong guess here would silently
    corrupt the file, worse than the current exact-match failure. Only
    reports the closest candidate region so the caller can see *why* their
    old_content didn't match (stale read, whitespace drift, wrong file) and
    correct it themselves.
    """
    import difflib
    file_lines = original.splitlines()
    needle_lines = old_content.splitlines()
    if not file_lines or not needle_lines:
        return ""
    window = len(needle_lines)
    best_ratio = 0.0
    best_start = 0
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(needle_lines)
    for start in range(0, max(1, len(file_lines) - window + 1)):
        candidate = file_lines[start:start + window]
        matcher.set_seq1(candidate)
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, start
    if best_ratio < 0.4:
        return "No similar region found in the file -- old_content may be from a different file or a stale read."
    lo = max(0, best_start - context_lines)
    hi = min(len(file_lines), best_start + window + context_lines)
    snippet = "\n".join(f"{i + 1}: {file_lines[i]}" for i in range(lo, hi))
    return (
        f"Closest match ({best_ratio:.0%} similar) is around line {best_start + 1}:\n{snippet}"
    )


@mcp.tool()
def edit_file(path: str, old_content: str, new_content: str, occurrence: int = 1) -> str:
    """Replace an exact block of text in a file (surgical, non-destructive).

    Finds the *n*-th occurrence of ``old_content`` and replaces it with
    ``new_content`` without touching the rest of the file.

    Args:
        path:        File path relative to workspace root.
        old_content: Exact text to find, including whitespace and indentation.
        new_content: Replacement text.
        occurrence:  Which occurrence to replace (1 = first).

    Returns:
        Confirmation showing the line number of the replacement, or an error.
    """
    try:
        p = _safe_path(path, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return f"[error] {exc}"
    try:
        original = p.read_text(encoding="utf-8")
    except OSError as exc:
        return f"[error] cannot read '{path}': {exc}"

    from herald.router.approval_gate import gate_call, CeilingExceeded, ApprovalPending
    try:
        gate_call("edit_file", {"path": path, "old_content": old_content, "new_content": new_content, "occurrence": occurrence}, preview=new_content[:2000])
    except CeilingExceeded as exc:
        return f"[error] {exc}"
    except ApprovalPending as exc:
        return f"[pending approval] {exc}"

    return _do_edit_file(path, old_content, new_content, occurrence)


def _do_edit_file(path: str, old_content: str, new_content: str, occurrence: int = 1) -> str:
    """The actual edit, past the gate. See _do_write_file's docstring."""
    p = _safe_path(path, must_exist=True)
    original = p.read_text(encoding="utf-8")
    pos = -1
    for _ in range(occurrence):
        pos = original.find(old_content, pos + 1)
        if pos == -1:
            count = original.count(old_content)
            if count == 0:
                return (
                    f"[error] old_content not found in '{path}'.  "
                    "Ensure the exact text (including indentation and newlines) matches.\n"
                    f"{_closest_match_hint(original, old_content)}"
                )
            return (
                f"[error] occurrence {occurrence} requested but only "
                f"{count} occurrence(s) exist in '{path}'"
            )
    updated = original[:pos] + new_content + original[pos + len(old_content):]
    try:
        p.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return f"[error] cannot write '{path}': {exc}"
    line_no = original[:pos].count("\n") + 1
    return f"Replaced occurrence {occurrence} at line ~{line_no} in {p.relative_to(WORKSPACE)}"


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------

@mcp.tool()
def list_directory(
    path: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    max_results: int = 200,
) -> str:
    """List files and subdirectories in a workspace directory.

    Args:
        path:        Directory path relative to workspace root (default: root).
        pattern:     Glob pattern to filter entries (default ``*``).
        recursive:   Walk all subdirectories if True.
        max_results: Cap on returned entries (default 200).

    Returns:
        Newline-separated list of relative paths, or an error.
    """
    try:
        p = _safe_path(path, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return f"[error] {exc}"
    if not p.is_dir():
        return f"[error] '{path}' is not a directory"
    try:
        entries = sorted(p.rglob(pattern) if recursive else p.glob(pattern))
    except (ValueError, OSError) as exc:
        return f"[error] {exc}"
    lines: list[str] = []
    for entry in entries[:max_results]:
        rel = entry.relative_to(WORKSPACE)
        lines.append(f"{rel}{'/' if entry.is_dir() else ''}")
    result = "\n".join(lines)
    if len(entries) > max_results:
        result += f"\n\n[...{len(entries) - max_results} more -- use a narrower pattern]"
    return result or "(empty directory)"


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------

@mcp.tool()
def run_command(
    command: str,
    cwd: str = ".",
    timeout: int = 60,
    env_extra: dict[str, str] | None = None,
) -> str:
    """Run a shell command inside the workspace and return its output.

    Capped at 300s. For builds, packaging, or test suites that might run
    longer than a couple of minutes (an Unreal Engine build, for example,
    routinely takes 10-15+ minutes), use run_command_background instead and
    poll it with check_command -- do not assume a slow step succeeded just
    because this timed out or because an older log file looks clean.

    Args:
        command:   Command string to execute (passed to the system shell).
        cwd:       Working directory, relative to workspace (default: root).
        timeout:   Maximum seconds to wait (default 60, max 300).
        env_extra: Extra environment variables merged into os.environ.

    Returns:
        stdout/stderr output and exit code, or a timeout/launch error.
    """
    try:
        work_dir = _safe_path(cwd, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return f"[error] {exc}"
    if not work_dir.is_dir():
        return f"[error] cwd '{cwd}' is not a directory"

    from herald.router.approval_gate import gate_call, CeilingExceeded, ApprovalPending
    try:
        gate_call("run_command", {"command": command, "cwd": cwd}, preview=command)
    except CeilingExceeded as exc:
        return f"[error] {exc}"
    except ApprovalPending as exc:
        return f"[pending approval] {exc}"

    return _do_run_command(command, cwd, timeout, env_extra)


def _do_run_command(command: str, cwd: str = ".", timeout: int = 60, env_extra: dict[str, str] | None = None) -> str:
    """The actual shell execution, past the gate. See _do_write_file's docstring."""
    work_dir = _safe_path(cwd, must_exist=True)
    timeout = min(max(1, timeout), 300)
    env = {**os.environ, **(env_extra or {})}

    # Smart Shell Normalizer for Windows:
    # Auto-route PowerShell cmdlets or normalize SSH commands to prevent syntax crashes across CLIs.
    exec_cmd = command.strip()
    if os.name == "nt":
        # Fix classic Windows trailing backslash escaping the closing quote: e.g. "dir D:\" -> "dir D:\\"
        import re
        exec_cmd = re.sub(r'(?<!\\)\\("|\')', r'\\\\\1', exec_cmd)

        # Check if the command is a PowerShell-specific command or pipeline
        ps_indicators = ("Test-Path", "Get-ChildItem", "New-Item", "Select-Object", "Where-Object", "Get-Content", "Set-Content", "$")
        if any(ind in exec_cmd for ind in ps_indicators) and not exec_cmd.lower().startswith("powershell"):
            exec_cmd = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "{exec_cmd}"'

    try:
        proc = subprocess.run(
            exec_cmd, shell=True, cwd=str(work_dir),
            capture_output=True, text=True, timeout=timeout, env=env,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"[error] command timed out after {timeout}s"
    except OSError as exc:
        return f"[error] failed to launch command: {exc}"
    stdout = _trunc(proc.stdout or "", MAX_OUTPUT_BYTES // 2)
    stderr = _trunc(proc.stderr or "", MAX_OUTPUT_BYTES // 2)
    parts = [p for p in (stdout, f"[stderr]\n{stderr}" if stderr else "") if p]
    parts.append(f"[exit code: {proc.returncode}]")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# run_command_background / check_command
#
# run_command is capped at 300s, which is far too short for real build steps
# (e.g. a Satisfactory/Unreal Engine mod package build routinely takes
# 10-15+ minutes). Blocking the whole agent turn on a multi-minute HTTP call
# also risks tripping unrelated upstream timeouts. So long-running commands
# (builds, packaging, test suites) must be launched here instead: they start
# detached, return a job_id immediately, and the caller polls check_command
# until the job reports done=True. Never assume success just because a build
# *started* -- always poll to completion and read the real exit code before
# reporting a build as successful.
# ---------------------------------------------------------------------------

_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_INDEX_PATH = WORKSPACE / ".herald_jobs" / "index.json"


def _persist_job_index() -> None:
    """Write job metadata (not the live Popen handle, which can't survive a
    restart) to disk so a restarted process can still find a job's log and
    check whether its PID is still alive -- not vanish it without a trace,
    which is what happened before this existed."""
    with _JOBS_LOCK:
        snapshot = {
            job_id: {
                "log_path": str(job["log_path"]), "command": job["command"],
                "pid": job["proc"].pid, "started_at": job["started_at"],
                "done": job["done"], "exit_code": job["exit_code"],
            }
            for job_id, job in _JOBS.items()
        }
    try:
        _JOBS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        _JOBS_INDEX_PATH.write_text(json.dumps(snapshot), encoding="utf-8")
    except OSError:
        pass  # best-effort: the in-memory dict is still authoritative for this process's own lifetime


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


@mcp.tool()
def run_command_background(
    command: str,
    cwd: str = ".",
    env_extra: dict[str, str] | None = None,
) -> str:
    """Launch a long-running shell command (builds, packaging, test suites) without blocking.

    Use this instead of run_command whenever the command might take longer
    than a couple of minutes -- run_command's 300s cap will otherwise return
    a timeout with no useful result, well before slow steps like an Unreal
    Engine build finish. Poll the returned job_id with check_command until
    done=True, then read its exit_code -- do not report success before that.

    Args:
        command:   Command string to execute (passed to the system shell).
        cwd:       Working directory, relative to workspace (default: root).
        env_extra: Extra environment variables merged into os.environ.

    Returns:
        A job_id to pass to check_command, or an error string.
    """
    try:
        work_dir = _safe_path(cwd, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return f"[error] {exc}"
    if not work_dir.is_dir():
        return f"[error] cwd '{cwd}' is not a directory"
    env = {**os.environ, **(env_extra or {})}

    exec_cmd = command.strip()
    if os.name == "nt":
        exec_cmd = re.sub(r'(?<!\\)\\("|\')', r'\\\\\1', exec_cmd)
        ps_indicators = ("Test-Path", "Get-ChildItem", "New-Item", "Select-Object", "Where-Object", "Get-Content", "Set-Content", "$")
        if any(ind in exec_cmd for ind in ps_indicators) and not exec_cmd.lower().startswith("powershell"):
            exec_cmd = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "{exec_cmd}"'

    job_id = uuid.uuid4().hex[:12]
    log_dir = WORKSPACE / ".herald_jobs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"[error] cannot create job log directory: {exc}"
    log_path = log_dir / f"{job_id}.log"

    try:
        log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[error] cannot open job log file: {exc}"

    try:
        proc = subprocess.Popen(
            exec_cmd, shell=True, cwd=str(work_dir), env=env,
            stdout=log_file, stderr=subprocess.STDOUT, text=True,
        )
    except OSError as exc:
        log_file.close()
        return f"[error] failed to launch command: {exc}"

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "proc": proc,
            "log_path": log_path,
            "log_file": log_file,
            "command": exec_cmd,
            "started_at": time.time(),
            "done": False,
            "exit_code": None,
        }
    _persist_job_index()
    return (
        f"job_id: {job_id}\n"
        f"Started in background (PID {proc.pid}), logging to {log_path.relative_to(WORKSPACE)}.\n"
        f"Poll with check_command(job_id=\"{job_id}\") until done=True before drawing any conclusion about success."
    )


@mcp.tool()
def check_command(job_id: str, tail_lines: int = 80) -> str:
    """Poll a background job started by run_command_background.

    Args:
        job_id:     The job_id returned by run_command_background.
        tail_lines: How many lines of recent output to include (default 80).

    Returns:
        Status (running/done), elapsed seconds, exit code if finished, and
        a tail of the command's output. Report a job as successful only
        after this shows done=True and exit_code==0 -- a job still running,
        or one you haven't polled at all, is not evidence of anything.
    """
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)

    if job is None:
        # Not in this process's memory -- either a bad job_id, or this
        # process restarted since the job was launched. A restarted
        # process has no Popen handle to poll and genuinely cannot recover
        # the real exit code (that information only ever existed in the
        # dead process's memory); the honest answer is the job's log (still
        # on disk) and whether its PID is still alive, not a fabricated
        # done=true/exit_code=0 the caller would build on incorrectly.
        try:
            index = json.loads(_JOBS_INDEX_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            index = {}
        entry = index.get(job_id)
        if entry is None:
            return f"[error] unknown job_id '{job_id}'"
        log_path = Path(entry["log_path"])
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            text = f"[error reading log: {exc}]"
        tail = "\n".join(text.splitlines()[-max(1, tail_lines):])
        elapsed = time.time() - entry["started_at"]
        if entry["done"]:
            status_line = f"status: done (recorded before a process restart)\nexit_code: {entry['exit_code']}\n"
        elif _pid_alive(entry["pid"]):
            status_line = (
                "status: running (PID still alive, but the tracking process restarted -- "
                "keep polling; exit_code will remain unknown until this job is re-discovered "
                "as done or the log clearly shows completion)\n"
            )
        else:
            status_line = (
                "status: unknown -- PID is no longer running but the tracking process "
                "restarted before recording an exit code. Inspect the log tail below yourself; "
                "do not assume success or failure.\n"
            )
        return f"{status_line}elapsed_sec: {elapsed:.1f}\nlog: {log_path}\n---\n{tail}"

    proc: subprocess.Popen = job["proc"]
    ret = proc.poll()
    elapsed = time.time() - job["started_at"]
    if ret is not None and not job["done"]:
        with _JOBS_LOCK:
            job["done"] = True
            job["exit_code"] = ret
            try:
                job["log_file"].close()
            except OSError:
                pass
        _persist_job_index()

    try:
        text = job["log_path"].read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        text = f"[error reading log: {exc}]"
    lines = text.splitlines()
    tail = "\n".join(lines[-max(1, tail_lines):])

    status = "done" if job["done"] else "running"
    header = f"status: {status}\nelapsed_sec: {elapsed:.1f}\n"
    if job["done"]:
        header += f"exit_code: {job['exit_code']}\n"
    header += f"log: {job['log_path'].relative_to(WORKSPACE)}\n---\n"
    return header + tail


# ---------------------------------------------------------------------------
# run_tests
#
# run_command/run_command_background return raw stdout/stderr for everything,
# which forces the caller to parse pass/fail/count out of arbitrary text --
# exactly the kind of ambiguity that let a stale, differently-shaped build
# log get misread as "clean" on a real task. This normalizes the common
# runners' own summary line into structured counts so there's nothing to
# misread. Falls back to raw output (with parsed=false) for anything it
# doesn't recognize, rather than guessing at a shape that isn't there.
# ---------------------------------------------------------------------------

_TEST_SUMMARY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # pytest: "3 failed, 12 passed, 1 skipped in 4.21s" (order/subset varies)
    ("pytest", re.compile(
        r"(?:(?P<failed>\d+) failed)|(?:(?P<passed>\d+) passed)|"
        r"(?:(?P<skipped>\d+) skipped)|(?:(?P<errors>\d+) error)"
    )),
]

_DOTNET_SUMMARY = re.compile(
    r"Passed!\s*-\s*Failed:\s*(?P<failed>\d+),\s*Passed:\s*(?P<passed>\d+),\s*Skipped:\s*(?P<skipped>\d+)"
    r"|Failed!\s*-\s*Failed:\s*(?P<failed2>\d+),\s*Passed:\s*(?P<passed2>\d+),\s*Skipped:\s*(?P<skipped2>\d+)"
)
_JEST_SUMMARY = re.compile(
    r"Tests:\s*(?:(?P<failed>\d+) failed,\s*)?(?:(?P<skipped>\d+) skipped,\s*)?(?P<passed>\d+) passed,\s*(?P<total>\d+) total"
)
_GO_TEST_FAIL = re.compile(r"^---\s+FAIL:", re.MULTILINE)
_GO_TEST_PASS = re.compile(r"^---\s+PASS:", re.MULTILINE)
_GO_TEST_SUMMARY = re.compile(r"^(ok|FAIL)\s+\S+", re.MULTILINE)


def _parse_test_output(text: str) -> dict[str, Any] | None:
    """Best-effort structured summary. Returns None (not a guess) when no
    known framework's summary line is recognized."""
    dotnet = _DOTNET_SUMMARY.search(text)
    if dotnet:
        g = dotnet.groupdict()
        failed = int(g["failed"] or g["failed2"] or 0)
        passed = int(g["passed"] or g["passed2"] or 0)
        skipped = int(g["skipped"] or g["skipped2"] or 0)
        return {"framework": "dotnet", "passed": passed, "failed": failed, "skipped": skipped}

    jest = _JEST_SUMMARY.search(text)
    if jest:
        g = jest.groupdict()
        return {
            "framework": "jest",
            "passed": int(g["passed"]), "failed": int(g["failed"] or 0),
            "skipped": int(g["skipped"] or 0),
        }

    if _GO_TEST_SUMMARY.search(text):
        return {
            "framework": "go test",
            "passed": len(_GO_TEST_PASS.findall(text)),
            "failed": len(_GO_TEST_FAIL.findall(text)),
            "skipped": 0,
        }

    # pytest's final summary line looks like "=== 3 failed, 12 passed in 4.2s ==="
    tail = "\n".join(text.splitlines()[-15:])
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    found_any = False
    for match in _TEST_SUMMARY_PATTERNS[0][1].finditer(tail):
        for key, value in match.groupdict().items():
            if value is not None:
                counts[key] = int(value)
                found_any = True
    if found_any:
        return {"framework": "pytest-like", **counts}
    return None


@mcp.tool()
def run_tests(
    command: str,
    cwd: str = ".",
    timeout: int = 300,
    env_extra: dict[str, str] | None = None,
) -> str:
    """Run a test command and return a structured pass/fail summary.

    Recognizes pytest, dotnet test, jest/npm test, and go test summary
    lines and normalizes them to explicit passed/failed/skipped counts so
    there's nothing to misread. Falls back to "parsed": false with the raw
    tail of output for anything else -- report parsed=false results as
    inconclusive, not as a pass, since no framework's summary was confirmed.

    For a suite that might run longer than a few minutes, use
    run_command_background instead and parse its final output yourself with
    the same discipline: a job still running is not a passing test suite.

    Args:
        command:   Test command to run (e.g. "pytest -q", "dotnet test").
        cwd:       Working directory, relative to workspace (default: root).
        timeout:   Maximum seconds to wait (default 300, max 600).
        env_extra: Extra environment variables merged into os.environ.

    Returns:
        A structured summary (framework, passed/failed/skipped, exit_code)
        plus a tail of raw output for context.
    """
    try:
        work_dir = _safe_path(cwd, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return f"[error] {exc}"
    if not work_dir.is_dir():
        return f"[error] cwd '{cwd}' is not a directory"
    timeout = min(max(1, timeout), 600)
    env = {**os.environ, **(env_extra or {})}
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(work_dir),
            capture_output=True, text=True, timeout=timeout, env=env,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return (
            f"[error] test command timed out after {timeout}s -- use run_command_background "
            "for a suite this slow, and do not treat the timeout as a pass or a fail."
        )
    except OSError as exc:
        return f"[error] failed to launch command: {exc}"

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    summary = _parse_test_output(combined)
    tail = "\n".join(combined.strip().splitlines()[-40:])
    if summary is None:
        return (
            f"parsed: false\nexit_code: {proc.returncode}\n"
            "No recognized test-framework summary line was found -- do not report this as a "
            f"pass or fail, inspect the raw output below yourself.\n---\n{tail}"
        )
    verdict = "pass" if summary["failed"] == 0 and proc.returncode == 0 else "fail"
    lines = ["parsed: true", f"framework: {summary['framework']}", f"verdict: {verdict}",
             f"exit_code: {proc.returncode}"]
    for key in ("passed", "failed", "skipped", "errors"):
        if key in summary:
            lines.append(f"{key}: {summary[key]}")
    lines.append("---")
    lines.append(tail)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# check_build_log
#
# Tonight's actual coding task (an Unreal Engine C++ build) needed its
# compile errors extracted from a raw UBT log by hand, repeatedly, via ad-hoc
# grep -- exactly the kind of tedious, error-prone step a tool should do
# instead. This turns an MSVC or GCC/Clang-style compile log into a
# structured list of {file, line, code, message} instead of a wall of text
# to re-read every time.
# ---------------------------------------------------------------------------

_MSVC_ERROR_RE = re.compile(
    r"^(?P<file>[^\r\n(]+)\((?P<line>\d+)(?:,(?P<col>\d+))?\):\s*"
    r"(?P<severity>fatal error|error|warning)\s+(?P<code>[A-Z]+\d+):\s*(?P<message>.+)$",
    re.MULTILINE,
)
_GCC_ERROR_RE = re.compile(
    r"^(?P<file>[^\r\n:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<severity>fatal error|error|warning):\s*(?P<message>.+)$",
    re.MULTILINE,
)
_BUILD_SUCCESS_RE = re.compile(r"\bBUILD SUCCESSFUL\b|\bBuild succeeded\b", re.IGNORECASE)
_BUILD_FAILED_RE = re.compile(r"\bBUILD FAILED\b|\bBuild FAILED\b|\bResult:\s*Failed\b", re.IGNORECASE)


def _parse_build_log(text: str, max_errors: int = 50) -> dict[str, Any]:
    """Structured extraction from an MSVC/UBT or GCC/Clang compile log.

    Never guesses a result when there's no evidence -- build_result is
    "unknown" unless an explicit success/failure marker or at least one
    parsed error/warning was found.
    """
    entries: list[dict[str, Any]] = []
    for pattern in (_MSVC_ERROR_RE, _GCC_ERROR_RE):
        for match in pattern.finditer(text):
            g = match.groupdict()
            entries.append({
                "file": g["file"].strip(),
                "line": int(g["line"]),
                "col": int(g["col"]) if g.get("col") else None,
                "severity": g["severity"].lower(),
                "code": g.get("code"),
                "message": g["message"].strip(),
            })
    # Same physical error is often reported once per translation unit that
    # includes the offending header/file; dedupe by (file, line, message).
    seen: set[tuple[str, int, str]] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        key = (entry["file"], entry["line"], entry["message"])
        if key not in seen:
            seen.add(key)
            deduped.append(entry)

    errors = [e for e in deduped if e["severity"] in ("error", "fatal error")]
    warnings = [e for e in deduped if e["severity"] == "warning"]

    if _BUILD_SUCCESS_RE.search(text) and not errors:
        build_result = "success"
    elif _BUILD_FAILED_RE.search(text) or errors:
        build_result = "failed"
    else:
        build_result = "unknown"

    return {
        "build_result": build_result,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors[:max_errors],
        "errors_truncated": max(0, len(errors) - max_errors),
    }


@mcp.tool()
def check_build_log(text: str = "", job_id: str = "", path: str = "", max_errors: int = 50) -> str:
    """Extract structured errors from an MSVC/UBT or GCC/Clang compile log.

    Give it exactly one source: raw log `text`, a `job_id` from
    run_command_background (reads that job's current log), or a `path` to a
    log file already in the workspace. Returns build_result
    (success/failed/unknown -- "unknown" only when there's no success marker,
    no failure marker, and no parsed error, never a guess) plus deduplicated
    {file, line, col, severity, code, message} entries.

    Args:
        text:       Raw log text to parse (use this OR job_id OR path).
        job_id:     A run_command_background job_id; parses its log so far.
        path:       Path to a log file, relative to the workspace root.
        max_errors: Cap on returned error entries (default 50).

    Returns:
        A structured summary, or an error if zero or more than one source
        was given.
    """
    sources = [bool(text), bool(job_id), bool(path)]
    if sum(sources) != 1:
        return "[error] give exactly one of: text, job_id, path"
    if job_id:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            return f"[error] unknown job_id '{job_id}'"
        try:
            text = job["log_path"].read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"[error] cannot read job log: {exc}"
    elif path:
        try:
            p = _safe_path(path, must_exist=True)
        except (ValueError, FileNotFoundError) as exc:
            return f"[error] {exc}"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"[error] cannot read '{path}': {exc}"

    result = _parse_build_log(text, max_errors=max_errors)
    lines = [
        f"build_result: {result['build_result']}",
        f"error_count: {result['error_count']}",
        f"warning_count: {result['warning_count']}",
    ]
    if result["build_result"] == "unknown":
        lines.append(
            "No success/failure marker and no parsed error/warning were found -- "
            "this log may not be from a recognized MSVC/UBT or GCC/Clang build."
        )
    for e in result["errors"]:
        loc = f"{e['file']}:{e['line']}" + (f":{e['col']}" if e["col"] else "")
        lines.append(f"[{e['code'] or e['severity']}] {loc}: {e['message']}")
    if result["errors_truncated"]:
        lines.append(f"... {result['errors_truncated']} more error(s) not shown, raise max_errors to see them")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------

@mcp.tool()
def search_code(
    pattern: str,
    path: str = ".",
    extensions: list[str] | None = None,
    case_sensitive: bool = False,
    max_results: int = 100,
) -> str:
    """Search for a regex pattern across source files in the workspace.

    No external tools required -- uses Python's re module.

    Args:
        pattern:        Regex or literal string to search for.
        path:           Directory or file to search (default: workspace root).
        extensions:     Optional file extensions to limit search, e.g. ``["py", "ts"]``.
        case_sensitive: Case-sensitive match (default False).
        max_results:    Maximum matches returned (default 100).

    Returns:
        Matching lines in ``file:line: content`` format, or a summary.
    """
    try:
        base = _safe_path(path, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return f"[error] {exc}"
    try:
        compiled = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        return f"[error] invalid regex: {exc}"
    ext_set: set[str] | None = {("." + e.lstrip(".")).lower() for e in extensions} if extensions else None
    results: list[str] = []
    files_searched = 0
    candidates: list[Path] = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
    for fp in candidates:
        if ext_set and fp.suffix.lower() not in ext_set:
            continue
        try:
            if fp.stat().st_size > 2 * 1024 * 1024:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_searched += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if compiled.search(line):
                results.append(f"{fp.relative_to(WORKSPACE)}:{lineno}: {line.rstrip()}")
                if len(results) >= max_results:
                    results.append(
                        f"\n[...stopped at {max_results} results -- narrow pattern or scope]"
                    )
                    return "\n".join(results)
    if not results:
        return f"No matches for '{pattern}' in {files_searched} file(s) searched."
    return f"{len(results)} match(es) in {files_searched} file(s):\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# git_run
# ---------------------------------------------------------------------------

_ALLOWED_GIT = frozenset({
    "status", "log", "diff", "show", "branch", "add", "commit", "stash",
    "reset", "checkout", "restore", "fetch", "pull", "push", "remote",
    "tag", "blame", "shortlog", "rev-parse", "ls-files", "rev-list", "describe",
})


@mcp.tool()
def git_run(args: str, cwd: str = ".") -> str:
    """Run a git command inside the workspace repository.

    A safe allowlist of subcommands is enforced.  Destructive administration
    commands (``clean -fdx``, ``filter-branch``, etc.) are blocked.

    Args:
        args: Git arguments after ``git`` itself, e.g. ``"status --short"``
              or ``"diff HEAD~1 -- path/to/file.py"``.
        cwd:  Working directory, relative to workspace (default: root).

    Returns:
        git output (stdout + stderr) and exit code.
    """
    if not shutil.which("git"):
        return "[error] git is not installed or not on PATH"
    stripped = args.strip()
    subcommand = stripped.split()[0].lower() if stripped else ""
    two_word = " ".join(stripped.lower().split()[:2])
    if subcommand not in _ALLOWED_GIT and two_word not in _ALLOWED_GIT:
        return (
            f"[error] git subcommand '{subcommand}' is not allowed.  "
            f"Allowed: {', '.join(sorted(_ALLOWED_GIT))}"
        )
    try:
        work_dir = _safe_path(cwd, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return f"[error] {exc}"
    try:
        proc = subprocess.run(
            ["git", *stripped.split()],
            cwd=str(work_dir), capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "[error] git timed out after 60s"
    except OSError as exc:
        return f"[error] failed to run git: {exc}"
    out = _trunc((proc.stdout or "") + (proc.stderr or ""))
    return f"{out}\n[exit code: {proc.returncode}]"


# ---------------------------------------------------------------------------
# propose_improvement
# ---------------------------------------------------------------------------

@mcp.tool()
def propose_improvement(description: str, tool_name: str, source_context: str = "") -> str:
    """Propose a real, adoptable Herald improvement for human review.

    Feeds the same draft -> sandbox -> risk-scan -> queue pipeline used for
    reactive missing-tool gaps (herald/router/capability_drafting.py), but
    for a PROACTIVELY researched improvement -- something found while
    researching other projects/ecosystems that's genuinely worth adding.
    Never auto-deploys; lands as a pending proposal reviewable via
    `herald capability list/show/approve/deny`.

    Args:
        description:    What was found and why it's worth adding. Be
                         specific -- name the real project/pattern/library
                         this comes from, not a vague idea.
        tool_name:      A short snake_case name for the proposed new tool.
        source_context: Where this came from (a URL, a file path, a repo
                         name) -- optional but strongly preferred so a human
                         reviewer can verify the source.

    Returns:
        Confirmation with the new proposal's id, or an error.
    """
    try:
        from herald.router.capability_drafting import run_research_pipeline
        proposal = run_research_pipeline(description, tool_name=tool_name, source_context=source_context)
    except Exception as exc:
        return f"[error] failed to draft proposal: {exc}"
    if proposal is None:
        return "[error] drafting failed, see router logs for details"
    return (
        f"Proposal #{proposal.id} created ({proposal.gap_type}, risk={proposal.risk_level}, "
        f"sandbox_ok={proposal.sandbox_ok}). Review with: herald capability show {proposal.id}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
