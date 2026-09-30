"""Native subscription usage collection for CLI-backed accounts.

Codex is queried through its local app-server JSON-RPC protocol, the same
rate-limit state its interactive ``/status`` view displays. Claude Code's ``/usage`` view
fetches the authenticated OAuth usage endpoint, so Herald performs that same
read-only request with the CLI's existing credential. Local transcript totals
remain available for Claude alongside the live plan-limit percentages.

Credentials are only read into memory and are never returned, logged, or
copied into Herald's database.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


import httpx

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_BACKUP_HOME = Path(os.environ.get("HERALD_CODEX_BACKUP_HOME", Path.home() / ".codex_backup"))
CODEX_BACKUP_SESSIONS_DIR = CODEX_BACKUP_HOME / "sessions"
LOOKBACK_SECONDS = 24 * 60 * 60
CACHE_SECONDS = 60.0

_cache_lock = threading.Lock()
_cache: tuple[float, str, list[dict[str, Any]]] | None = None
_refresh_in_flight = False
_refresh_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="herald-usage-bg")


def _recent_files(root: Path, lookback_seconds: float) -> list[Path]:
    if not root.exists():
        return []
    cutoff = time.time() - lookback_seconds
    return [p for p in root.rglob("*.jsonl") if p.stat().st_mtime >= cutoff]


def _claude_transcript_usage(lookback_seconds: float = LOOKBACK_SECONDS) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    for path in _recent_files(CLAUDE_PROJECTS_DIR, lookback_seconds):
        try:
            with path.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    usage = entry.get("message", {}).get("usage") if isinstance(entry.get("message"), dict) else None
                    if usage:
                        for key in totals:
                            totals[key] += usage.get(key) or 0
        except OSError:
            continue
    return totals


def _limit(name: str, used: Any, resets_at: Any, **extra: Any) -> dict[str, Any]:
    try:
        used_percent = int(round(float(used)))
    except (TypeError, ValueError):
        used_percent = None
    return {
        "name": name,
        "used_percent": used_percent,
        "remaining_percent": None if used_percent is None else max(0, 100 - used_percent),
        "resets_at": resets_at,
        **extra,
    }


def claude_usage(
    lookback_seconds: float = LOOKBACK_SECONDS,
    *,
    config_dir: Path | None = None,
    cli: str = "claude-primary",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Read the exact plan limits displayed by Claude Code's ``/usage``."""
    root = config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR", CLAUDE_DIR))
    credentials = root / ".credentials.json"
    usage = _claude_transcript_usage(lookback_seconds)
    if not credentials.is_file():
        return {"cli": cli, "status": "unavailable", "usage": usage, "detail": f"credentials not found in {root}"}
    try:
        auth = json.loads(credentials.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        token = auth.get("accessToken")
        if not token:
            raise ValueError("Claude OAuth access token is unavailable")
        response = httpx.get(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "herald-cli-usage/0.2",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (OSError, ValueError, httpx.HTTPError) as exc:
        return {"cli": cli, "status": "unavailable", "usage": usage, "detail": f"native usage refresh failed: {exc}"}

    limits = []
    labels = {
        "five_hour": "5-hour session",
        "seven_day": "7-day weekly",
        "seven_day_opus": "7-day Opus",
        "seven_day_sonnet": "7-day Sonnet",
        "seven_day_oauth_apps": "7-day OAuth apps",
    }
    for key, label in labels.items():
        row = payload.get(key)
        if isinstance(row, dict) and row.get("utilization") is not None:
            limits.append(_limit(label, row.get("utilization"), row.get("resets_at"), limit_id=key))
    extra = payload.get("extra_usage") or {}
    return {
        "cli": cli,
        "status": "ok",
        "source": "claude_oauth_usage",
        "plan_type": auth.get("subscriptionType"),
        "limits": limits,
        "usage": usage,
        "extra_usage": {
            "enabled": bool(extra.get("is_enabled")),
            "utilization": extra.get("utilization"),
            "used_credits": extra.get("used_credits"),
            "monthly_limit": extra.get("monthly_limit"),
        },
    }


def _codex_command() -> list[str] | None:
    search_dirs = [
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / "AppData" / "Roaming" / "npm"),
        "/usr/local/bin",
    ]
    search_path = os.pathsep.join([*search_dirs, os.environ.get("PATH", "")])
    path = shutil.which("codex", path=search_path) or shutil.which("codex.cmd", path=search_path)
    if not path:
        return None
    # A native binary avoids cmd.exe quoting and process-tree cleanup issues.
    if os.name == "nt" and Path(path).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        npm_root = Path(path).parent / "node_modules" / "@openai" / "codex" / "node_modules"
        native = next(npm_root.glob("@openai/codex-win32-*/vendor/*/bin/codex.exe"), None)
        if native:
            return [str(native)]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", path]
    return [path]


def _codex_rpc(home: Path, timeout: float = 15.0) -> dict[str, Any]:
    command = _codex_command()
    if not command:
        raise FileNotFoundError("codex executable not found")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    proc = subprocess.Popen(
        [*command, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    messages: queue.Queue[str] = queue.Queue()

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            messages.put(line)

    threading.Thread(target=read_stdout, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = json.loads(messages.get(timeout=min(0.5, max(0.01, deadline - time.monotonic()))))
            except queue.Empty:
                if proc.poll() is not None:
                    raise RuntimeError(f"Codex app-server exited with code {proc.returncode}")
                continue
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
        raise TimeoutError("Codex app-server usage request timed out")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "herald", "title": "Herald", "version": "0.2"}}})
        receive(1)
        send({"method": "initialized", "params": None})
        send({"id": 2, "method": "account/rateLimits/read", "params": None})
        return receive(2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _codex_transcript_usage(sessions_dir: Path, lookback_seconds: float) -> dict[str, Any] | None:
    for path in sorted(_recent_files(sessions_dir, lookback_seconds), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with path.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"token_count"' not in line:
                        continue
                    try:
                        info = json.loads(line).get("payload", {}).get("info")
                    except json.JSONDecodeError:
                        continue
                    if info:
                        return info.get("total_token_usage") or info
        except OSError:
            continue
    return None


def _codex_window_name(minutes: Any, fallback: str) -> str:
    """Give Codex's native rate-limit windows stable user-facing names."""
    try:
        duration = int(minutes)
    except (TypeError, ValueError):
        return fallback
    if duration == 300:
        return "5-hour session"
    if duration == 10080:
        return "7-day weekly"
    if duration > 0:
        return f"{duration / 60:g}-hour"
    return fallback


def codex_usage(
    lookback_seconds: float = LOOKBACK_SECONDS,
    *,
    home: Path | None = None,
    sessions_dir: Path | None = None,
    cli: str = "codex-primary",
) -> dict[str, Any]:
    home = home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    sessions_dir = sessions_dir or home / "sessions"
    transcript = _codex_transcript_usage(sessions_dir, lookback_seconds)
    try:
        payload = _codex_rpc(home)
    except (OSError, RuntimeError, TimeoutError) as exc:
        return {"cli": cli, "status": "unavailable", "usage": transcript or {}, "detail": f"native usage refresh failed: {exc}"}

    snapshot = payload.get("rateLimits") or {}
    limits = []
    for field, default_name in (("primary", "primary"), ("secondary", "secondary")):
        window = snapshot.get(field)
        if isinstance(window, dict):
            minutes = window.get("windowDurationMins")
            name = _codex_window_name(minutes, default_name)
            limits.append(_limit(name, window.get("usedPercent"), window.get("resetsAt"), window_minutes=minutes))
    return {
        "cli": cli,
        "status": "ok",
        "source": "codex_app_server",
        "plan_type": snapshot.get("planType"),
        "limit_id": snapshot.get("limitId"),
        "limits": limits,
        "usage": transcript or {},
        "credits": snapshot.get("credits"),
    }


def _agy_command() -> list[str] | None:
    search_dirs = [
        str(Path.home() / "AppData" / "Local" / "agy" / "bin"),
        str(Path.home() / ".local" / "bin"),
        "/usr/local/bin",
    ]
    search_path = os.pathsep.join([*search_dirs, os.environ.get("PATH", "")])
    path = shutil.which("agy", path=search_path) or shutil.which("agy.exe", path=search_path)
    return [path] if path else None


def antigravity_usage(*, cli: str = "antigravity", timeout: float = 20.0) -> list[dict[str, Any]]:
    """Read Antigravity's own `/usage` slash command via `agy -p "/usage"
    --output-format json`, the same source its interactive UI shows. There is
    no lower-level RPC exposed the way Codex's app-server has one, so this
    shells out to the CLI in print mode instead."""
    command = _agy_command()
    if not command:
        return [{
            "cli": cli,
            "status": "unavailable",
            "detail": "agy CLI not found on PATH",
        }]

    try:
        proc = subprocess.run(
            [*command, "-p", "/usage", "--output-format", "json", "--print-timeout", f"{int(timeout)}s"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout + 10,
        )
        payload = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return [{
            "cli": cli,
            "status": "unavailable",
            "detail": f"native usage refresh failed: {exc}",
        }]

    if payload.get("status") != "SUCCESS":
        return [{
            "cli": cli,
            "status": "unavailable",
            "detail": f"agy /usage returned status={payload.get('status')!r}",
        }]

    groups = ((payload.get("command") or {}).get("data") or {}).get("groups") or []
    rows: list[dict[str, Any]] = []
    for group in groups:
        limits = []
        for bucket in group.get("buckets", []):
            remaining_fraction = bucket.get("remaining_fraction")
            used_percent = None if remaining_fraction is None else round((1 - remaining_fraction) * 100, 2)
            limits.append(_limit(
                bucket.get("name") or bucket.get("id") or "limit",
                used_percent, bucket.get("reset_time"),
                window=bucket.get("window"), limit_id=bucket.get("id"),
            ))
        rows.append({
            "cli": f"{cli} ({group.get('name', 'unknown')})",
            "status": "ok",
            "source": "agy_usage_command",
            "plan_type": group.get("description"),
            "limits": limits,
        })
    if not rows:
        return [{
            "cli": cli,
            "status": "unavailable",
            "detail": "agy /usage returned no model groups",
        }]
    return rows


def _default_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = [
        {"name": "claude-primary", "cli_name": "claude", "config": {}},
        {"name": "codex-primary", "cli_name": "codex", "config": {}},
        {"name": "antigravity", "cli_name": "antigravity", "config": {}},
    ]
    if CODEX_BACKUP_HOME.is_dir():
        profiles.append({
            "name": "codex-backup",
            "cli_name": "codex",
            "config": {"env": {"CODEX_HOME": str(CODEX_BACKUP_HOME)}},
        })
    return profiles


def _collector(profile: dict[str, Any]):
    name = str(profile.get("name") or profile.get("cli_name") or "cli")
    cli_name = str(profile.get("cli_name") or "").casefold()
    config = profile.get("config") if isinstance(profile.get("config"), dict) else {}
    environment = config.get("env") if isinstance(config.get("env"), dict) else {}

    if cli_name == "codex":
        home = Path(environment.get("CODEX_HOME") or config.get("home") or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        return lambda: codex_usage(home=home, cli=name)
    if cli_name == "claude":
        root = Path(environment.get("CLAUDE_CONFIG_DIR") or config.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR", CLAUDE_DIR))
        return lambda: claude_usage(config_dir=root, cli=name)
    if cli_name == "antigravity" or cli_name.startswith("profile_"):
        return lambda: antigravity_usage(cli=name)
    return None


def _profile_cache_key(profiles: list[dict[str, Any]]) -> str:
    return json.dumps(profiles, sort_keys=True, default=str, separators=(",", ":"))


def _collect_all_usage(profiles: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    selected = _default_profiles() if profiles is None else profiles
    collectors = [collector for profile in selected if (collector := _collector(profile))]
    if not collectors:
        return []

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(collectors), thread_name_prefix="herald-usage") as pool:
        futures = [pool.submit(c) for c in collectors]
        for f in futures:
            res = f.result()
            if isinstance(res, list):
                rows.extend(res)
            else:
                rows.append(res)
    return rows


def _refresh_cache_sync(profiles: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    global _cache
    selected = _default_profiles() if profiles is None else profiles
    key = _profile_cache_key(selected)
    rows = _collect_all_usage(selected)
    with _cache_lock:
        _cache = (time.monotonic(), key, rows)
    return rows


def _refresh_cache_background(profiles: list[dict[str, Any]] | None = None) -> None:
    global _refresh_in_flight
    try:
        _refresh_cache_sync(profiles)
    finally:
        with _cache_lock:
            _refresh_in_flight = False


def all_usage(*, refresh: bool = False, profiles: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Live subscription/quota usage across every CLI.

    ``refresh=True`` (an explicit ``herald usage`` run, a dashboard poll)
    blocks and fetches synchronously -- the caller is actively waiting.
    ``refresh=False`` is used on every routing decision via quota_router, so
    it must never block on a subprocess or network call: it serves whatever
    is cached (even if stale) and kicks a background refresh when the cache
    is missing or older than CACHE_SECONDS, rather than fetching inline.
    """
    global _refresh_in_flight
    if refresh:
        return _refresh_cache_sync(profiles)

    selected = _default_profiles() if profiles is None else profiles
    key = _profile_cache_key(selected)

    with _cache_lock:
        cached = _cache
        stale = cached is None or cached[1] != key or time.monotonic() - cached[0] >= CACHE_SECONDS
        should_start = stale and not _refresh_in_flight
        if should_start:
            _refresh_in_flight = True

    if should_start:
        _refresh_executor.submit(_refresh_cache_background, selected)

    return cached[2] if cached and cached[1] == key else []
