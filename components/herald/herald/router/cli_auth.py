"""Auth status + login-driving for CLI-backed tools registered as `cli`
backends. Distinct from node_control.py -- these CLIs run on the same host
as the router itself (this device), so it's a local subprocess, not SSH.

Login-driving is NOT one-click/automatic: it starts the CLI's own login
command, hands back whatever it printed (a URL, and a code if there is one),
and the human completes the actual sign-in themselves in a browser --
exactly the same shape as the Kapture g4f refresh flow. If a flow needs a
code pasted back into the running process (rather than just typed into a
web page), submit_login_code() writes it to that process's stdin.

Confirmed live (a Linux Router, 2026-08-09): `codex login --device-auth` prints
a URL + one-time code immediately and needs nothing pasted back -- the
background process itself polls until the browser step completes. Also
confirmed the hard way: starting it invalidates the CLI's existing session
the moment it starts, success or not -- so start_login must never be called
opportunistically/automatically, only on a deliberate, informed request.
Confirmed live: `claude auth login` opens a hosted callback page
(platform.claude.com) that shows a code to paste back -- doesn't invalidate
the existing session until the new login actually completes, unlike codex.

antigravity (`agy`) is a third, harder shape: no login/status subcommand at
all, and its interactive login only runs inside a full-screen TUI (bubbletea)
that refuses to start without a real PTY -- a plain stdin/stdout pipe (like
_LoginSession uses for claude/codex) doesn't work. It also blocks forever on
DECRQM terminal-capability queries (modes 2026/2027/2004/1049) unless
something answers them, since there's no real terminal to reply. Confirmed
live: allocating a real PTY (pty.fork), replying "not supported" to those
queries, and rendering the byte stream through a real terminal emulator
(pyte) works -- see _PtyLoginSession.
"""
from __future__ import annotations

import shutil
import os
import re
import subprocess
import threading
import time
import signal
import pyte
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:
    import fcntl
    import pty
    import select
    import struct
    import termios

    import pyte
    _PTY_AVAILABLE = True
except ImportError:
    _PTY_AVAILABLE = False

try:
    from winpty import PtyProcess
    _WINDOWS_PTY_AVAILABLE = os.name == "nt"
except ImportError:
    _WINDOWS_PTY_AVAILABLE = False

# One entry per CLI this dashboard cares about. `cmd` must be a plain,
# non-interactive status check -- verified by hand for each of these before
# adding it here, not assumed from --help output. antigravity (`agy`) has no
# dedicated status/whoami subcommand at all -- `agy models` is used as a
# proxy instead, since fetching the model list requires a valid session;
# success/failure of that call is the only signal available.
_STATUS_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "auth", "status"],
    "codex": ["codex", "login", "status"],
    "antigravity": ["agy", "models"],
}
_DETAIL_MAX_CHARS = 300
_CREDENTIAL_OUTPUT = re.compile(
    r"(?i)(?:\b(?:access[_ -]?token|refresh[_ -]?token|api[_ -]?key|authorization|password|credential)"
    r"(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[^\s]+|\bbearer\s+[^\s]+)"
)


def _sanitize_process_output(output: str) -> str:
    """Remove credential-shaped diagnostics while preserving URLs/device instructions."""
    return _CREDENTIAL_OUTPUT.sub("credential=[redacted]", output)


def _run_status(cli: str, cmd: list[str], timeout: float = 15.0, env: dict[str, str] | None = None) -> dict[str, Any]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    extra_paths = [
        str(Path.home() / "AppData" / "Roaming" / "npm"),
        str(Path.home() / "AppData" / "Local" / "agy" / "bin"),
        str(Path.home() / ".local" / "bin"),
        "/usr/local/bin",
    ]
    current_path = run_env.get("PATH", "")
    for p in extra_paths:
        if p not in current_path.split(os.pathsep):
            current_path = f"{p}{os.pathsep}{current_path}"
    run_env["PATH"] = current_path
    resolved = shutil.which(cmd[0], path=current_path)
    if resolved:
        cmd = [resolved, *cmd[1:]]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=run_env)
    except FileNotFoundError:
        return {"cli": cli, "status": "not_installed", "detail": f"{cmd[0]} not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"cli": cli, "status": "timeout", "detail": f"{' '.join(cmd)} did not return within {timeout}s"}
    output = _sanitize_process_output((proc.stdout or proc.stderr).strip())
    if len(output) > _DETAIL_MAX_CHARS:
        output = output[:_DETAIL_MAX_CHARS] + f"... ({len(output)} chars total)"
    if proc.returncode != 0:
        return {"cli": cli, "status": "logged_out", "detail": output}
    return {"cli": cli, "status": "logged_in", "detail": output}


def check_g4f_status() -> dict[str, Any]:
    """Check G4F gateway and account session health."""
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:4900/v1/models", timeout=3)
        if r.status_code == 200:
            return {"cli": "g4f-gateway", "status": "logged_in", "detail": "Gateway active on port 4900"}
    except Exception:
        pass
    return {"cli": "g4f-gateway", "status": "logged_out", "detail": "Gateway offline or restarting"}


def check_one_status(cli: str, *, timeout: float = 10.0) -> dict[str, Any] | None:
    """Status for a single CLI, for use as a post-failure diagnostic (see
    adapters.call_cli) rather than the dashboard's all-CLI poll. Returns
    None if `cli` has no registered status command (nothing to check)."""
    cmd = _STATUS_COMMANDS.get(cli)
    if cmd is None:
        return None
    return _run_status(cli, cmd, timeout=timeout)


def all_status() -> list[dict[str, Any]]:
    # Dashboard polling must cost one timeout window, not one per CLI.
    with ThreadPoolExecutor(max_workers=len(_STATUS_COMMANDS) or 1) as executor:
        futures = [executor.submit(_run_status, cli, cmd) for cli, cmd in _STATUS_COMMANDS.items()]
        statuses = [future.result() for future in futures]
    statuses.append(check_g4f_status())
    return statuses


def configured_status(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check only configured Router CLI profiles and preserve their names."""
    checks: list[tuple[str, list[str], dict[str, str] | None]] = []
    for profile in profiles:
        name = str(profile.get("name") or "cli")
        cli_name = str(profile.get("cli_name") or "").casefold()
        command = _STATUS_COMMANDS.get(cli_name)
        if command is None:
            continue
        config = profile.get("config") if isinstance(profile.get("config"), dict) else {}
        raw_env = config.get("env") if isinstance(config.get("env"), dict) else {}
        environment = {str(key): str(value) for key, value in raw_env.items()}
        checks.append((name, command, environment or None))
    if not checks:
        return []
    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = [
            executor.submit(_run_status, name, command, env=environment)
            for name, command, environment in checks
        ]
        return [future.result() for future in futures]


def auth_capabilities() -> dict[str, Any]:
    """Describe operations honestly; status checks never refresh credentials."""
    return {
        "clis": {
            cli: {
                "status": True,
                "login": cli in _LOGIN_COMMANDS or cli in _PTY_LOGIN_COMMANDS,
                "submit_code": cli in {"claude", "antigravity"},
                "poll": cli in _LOGIN_COMMANDS or cli in _PTY_LOGIN_COMMANDS,
                "cancel": cli in _LOGIN_COMMANDS or cli in _PTY_LOGIN_COMMANDS,
                "credential_refresh": False,
                "reauthentication": cli in _LOGIN_COMMANDS or cli in _PTY_LOGIN_COMMANDS,
            }
            for cli in _STATUS_COMMANDS
        },
        "g4f-gateway": {"status": True, "credential_refresh": False, "reauthentication": "capture"},
    }


def refresh_all_auth() -> dict[str, Any]:
    """Compatibility status recheck. This does not refresh credentials."""
    results = {s["cli"]: s for s in all_status()}
    return {
        "status": "ok",
        "operation": "status_check",
        "credentials_refreshed": False,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": "Authentication status rechecked; no credentials were refreshed.",
        "details": results,
    }


_LOGIN_COMMANDS: dict[str, list[str]] = {
    "codex": ["codex", "login", "--device-auth"],
    "claude": ["claude", "auth", "login"],
}


class _LoginSession:
    """Wraps one running login subprocess: a background thread drains its
    stdout (merged with stderr) into a buffer so start_login can return
    whatever's been printed so far without blocking on the process, which
    stays alive waiting on the browser step (or a pasted-back code)."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._redactions: set[str] = set()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._lock:
                self._lines.append(line.rstrip("\n"))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            output = "\n".join(self._lines)
            for secret in self._redactions:
                output = output.replace(secret, "[redacted]")
            output = _sanitize_process_output(output)
        exit_code = self.proc.poll()
        state = "pending" if exit_code is None else ("succeeded" if exit_code == 0 else "denied_or_expired")
        return {"state": state, "output": output, "running": exit_code is None, "exit_code": exit_code}

    def send_code(self, code: str) -> None:
        if self.proc.stdin is None or self.proc.poll() is not None:
            raise RuntimeError("login process is not running / has no stdin")
        self._redactions.add(code)
        self.proc.stdin.write(code + "\n")
        self.proc.stdin.flush()

    def cancel(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()


# CLIs whose login only runs inside a full-screen TUI needing a real PTY --
# see the module docstring for why _LoginSession's plain pipe doesn't work
# for these. `agy`'s login menu currently has exactly one real option
# ("Google OAuth"), so _PtyLoginSession auto-selects it with an Enter
# keypress rather than exposing menu navigation as its own API.
_PTY_LOGIN_COMMANDS: dict[str, list[str]] = {
    "antigravity": ["agy"],
}

# DECRQM modes agy queries on startup and blocks on until answered -- value
# 2 = "reset"/not supported, which is honest (nothing is answering as a real
# terminal) and lets the app fall back to its non-fancy rendering path.
_DECRQM_MODES = (2026, 2027, 2004, 1049)


class _PtyLoginSession:
    """Same role as _LoginSession, but for a TUI login: allocates a real
    PTY (pty.fork), auto-answers the DECRQM queries the app blocks on,
    sends one Enter to select the (only) login method, and reconstructs
    readable text from the raw byte stream via a real terminal emulator
    (pyte) since the app draws via cursor-addressed screen updates, not
    linear stdout."""

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None, cols: int = 120, rows: int = 50):
        if shutil.which(cmd[0]) is None:
            raise FileNotFoundError(cmd[0])
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._lock = threading.Lock()
        self._answered_decrqm = False
        self._sent_enter = False
        self._enter_at = time.time() + 4  # let the initial screen render first
        self._running = True
        self._cancelled = False
        self._redactions: set[str] = set()

        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(cmd[0], cmd, run_env)  # child -- replaced, never returns
        self._pid = pid
        self._fd = fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            if not self._sent_enter and time.time() >= self._enter_at:
                try:
                    os.write(self._fd, b"\r")
                except OSError:
                    pass
                self._sent_enter = True
            try:
                r, _, _ = select.select([self._fd], [], [], 1)
            except OSError:
                break
            if self._fd in r:
                try:
                    data = os.read(self._fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                with self._lock:
                    self._stream.feed(data.decode("utf-8", errors="replace"))
                    if not self._answered_decrqm and b"$p" in data:
                        for mode in _DECRQM_MODES:
                            try:
                                os.write(self._fd, f"\x1b[?{mode};2$y".encode())
                            except OSError:
                                pass
                        self._answered_decrqm = True
        with self._lock:
            self._running = False
        try:
            os.waitpid(self._pid, 0)
        except ChildProcessError:
            pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lines = [line.rstrip() for line in self._screen.display if line.strip()]
            running = self._running
        output = "\n".join(lines)
        for secret in self._redactions:
            output = output.replace(secret, "[redacted]")
        output = _sanitize_process_output(output)
        state = "pending" if running else ("cancelled" if self._cancelled else "succeeded")
        return {"state": state, "output": output, "running": running, "exit_code": None if running else 0}

    def send_code(self, code: str) -> None:
        with self._lock:
            running = self._running
        if not running:
            raise RuntimeError("login process is not running")
        try:
            self._redactions.add(code)
            os.write(self._fd, code.encode() + b"\r")
        except OSError as exc:
            raise RuntimeError(f"failed to write code: {exc}") from exc

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
        try:
            os.kill(self._pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


_sessions: dict[tuple[str, str], "_LoginSession | _PtyLoginSession"] = {}
_sessions_lock = threading.Lock()


class _WindowsPtyLoginSession:
    """Run the existing TUI login flow in a Windows pseudoterminal."""

    def __init__(self, cmd: list[str], env: dict[str, str] | None = None, cols: int = 120, rows: int = 50):
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        executable = shutil.which(cmd[0], path=run_env.get("PATH"))
        if not executable:
            raise FileNotFoundError(cmd[0])
        self._proc = PtyProcess.spawn([executable, *cmd[1:]], env=run_env, dimensions=(rows, cols))
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._lock = threading.Lock()
        self._redactions: set[str] = set()
        self._cancelled = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._enter_timer = threading.Timer(4, self._select_method)
        self._enter_timer.daemon = True
        self._enter_timer.start()

    def _select_method(self) -> None:
        if self._proc.isalive():
            self._proc.write("\r")

    def _read_loop(self) -> None:
        try:
            while True:
                data = self._proc.read(65536)
                if not data:
                    break
                with self._lock:
                    self._stream.feed(data)
                if "$p" in data:
                    for mode in _DECRQM_MODES:
                        self._proc.write(f"\x1b[?{mode};2$y")
        except (EOFError, OSError):
            return

    def snapshot(self) -> dict[str, Any]:
        running = self._proc.isalive()
        exit_code = None if running else self._proc.exitstatus
        with self._lock:
            output = "\n".join(line.rstrip() for line in self._screen.display if line.strip())
            for code in self._redactions:
                output = output.replace(code, "[redacted]")
        state = "pending" if running else ("cancelled" if self._cancelled else
                "succeeded" if exit_code == 0 else "denied_or_expired")
        return {"state": state, "output": _sanitize_process_output(output),
                "running": running, "exit_code": exit_code}

    def send_code(self, code: str) -> None:
        if not self._proc.isalive():
            raise RuntimeError("login process is not running")
        with self._lock:
            self._redactions.add(code)
        self._proc.write(code + "\r")

    def cancel(self) -> None:
        self._cancelled = True
        self._enter_timer.cancel()
        self._proc.terminate(force=True)


def _session_key(cli: str, account: str | None) -> tuple[str, str]:
    """Sessions are keyed by (cli, account) so two accounts of the same CLI
    (two registered `antigravity` profiles, for example) get independent
    login processes instead of colliding on one shared session. Callers that
    don't pass an account keep the old one-session-per-CLI behavior."""
    return (cli, account or cli)


def supported_login_clis() -> set[str]:
    """Provider names accepted by every login lifecycle surface."""
    return set(_LOGIN_COMMANDS) | set(_PTY_LOGIN_COMMANDS)


def _invalid(cli: str, error: str) -> dict[str, Any]:
    return {"cli": cli, "state": "invalid", "running": False, "error": error}


def start_login(
    cli: str, account: str | None = None, env: dict[str, str] | None = None, wait_seconds: float = 4.0,
) -> dict[str, Any]:
    """Starts `cli`'s own login command and returns whatever it's printed
    after a short wait -- normally enough for a URL/code to appear. The
    process is left running in the background (tracked in _sessions) so a
    later submit_login_code() call, or just time passing while the human
    completes the browser step, can carry it to completion.

    `account` distinguishes one registered profile from another for the same
    CLI (two `antigravity` accounts, for example); `env` is that account's
    config (its own HOME/CODEX_HOME/etc.) so each account's login
    lands in its own credential storage instead of overwriting a shared one."""
    cli = cli.strip().lower()
    if cli not in supported_login_clis():
        return _invalid(cli, f"unsupported login provider '{cli}'")
    key = _session_key(cli, account)
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    locations = [Path.home() / "AppData/Roaming/npm", Path.home() / "AppData/Local/agy/bin", Path.home() / ".local/bin"]
    run_env["PATH"] = os.pathsep.join([*(str(path) for path in locations), run_env.get("PATH", "")])
    with _sessions_lock:
        existing = _sessions.get(key)
        if existing is not None and existing.snapshot()["running"]:
            return {"cli": cli, "account": key[1], "error": "a login is already in progress for this account", **existing.snapshot()}

        session: _LoginSession | _PtyLoginSession
        if cli in _LOGIN_COMMANDS:
            cmd = _LOGIN_COMMANDS[cli]
            resolved = shutil.which(cmd[0], path=run_env["PATH"])
            if resolved:
                cmd = [resolved, *cmd[1:]]
                if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
                    cmd = [run_env.get("ComSpec", "cmd.exe"), "/d", "/c", *cmd]
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=run_env,
                )
            except FileNotFoundError:
                return _invalid(cli, f"{cmd[0]} not found on PATH")
            session = _LoginSession(proc)
        elif cli in _PTY_LOGIN_COMMANDS:
            if not _PTY_AVAILABLE and not _WINDOWS_PTY_AVAILABLE:
                return _invalid(cli, "PTY login support unavailable on this platform (needs pty/termios/pyte -- Linux only)")
            cmd = _PTY_LOGIN_COMMANDS[cli]
            try:
                session = (_WindowsPtyLoginSession if _WINDOWS_PTY_AVAILABLE else _PtyLoginSession)(cmd, env=run_env)
            except FileNotFoundError:
                return _invalid(cli, f"{cmd[0]} not found on PATH")
        _sessions[key] = session
    time.sleep(wait_seconds)
    return {"cli": cli, "account": key[1], **session.snapshot()}


def submit_login_code(cli: str, code: str, account: str | None = None, wait_seconds: float = 3.0) -> dict[str, Any]:
    cli = cli.strip().lower()
    if cli not in supported_login_clis():
        return _invalid(cli, f"unsupported login provider '{cli}'")
    if not code.strip():
        return _invalid(cli, "code must not be empty")
    key = _session_key(cli, account)
    with _sessions_lock:
        session = _sessions.get(key)
    if session is None:
        return _invalid(cli, "no login session started for this account")
    try:
        session.send_code(code)
    except RuntimeError:
        # Process/PTY errors are deliberately collapsed: implementations can
        # echo stdin in an exception and submitted codes are write-only.
        return {"cli": cli, "account": key[1], "error": "login process rejected the submitted code", **session.snapshot()}
    time.sleep(wait_seconds)
    return {"cli": cli, "account": key[1], **session.snapshot()}


def login_snapshot(cli: str, account: str | None = None) -> dict[str, Any]:
    cli = cli.strip().lower()
    if cli not in supported_login_clis():
        return _invalid(cli, f"unsupported login provider '{cli}'")
    key = _session_key(cli, account)
    with _sessions_lock:
        session = _sessions.get(key)
    if session is None:
        return {"cli": cli, "account": key[1], "state": "idle", "running": False, "error": "no login session started for this account"}
    return {"cli": cli, "account": key[1], **session.snapshot()}


def cancel_login(cli: str, account: str | None = None) -> dict[str, Any]:
    cli = cli.strip().lower()
    if cli not in supported_login_clis():
        return _invalid(cli, f"unsupported login provider '{cli}'")
    key = _session_key(cli, account)
    with _sessions_lock:
        session = _sessions.get(key)
    if session is None:
        return {"cli": cli, "account": key[1], "state": "idle", "running": False, "error": "no login session started for this account"}
    before = session.snapshot()
    if not before["running"]:
        return {"cli": cli, "account": key[1], **before, "cancelled": False, "error": "login session is not running"}
    session.cancel()
    # Popen state changes immediately in practice; avoid making cancellation block.
    snapshot = session.snapshot()
    return {"cli": cli, "account": key[1], **snapshot, "state": "cancelled", "running": False, "cancelled": True}
