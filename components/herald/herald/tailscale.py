"""Tailscale + SSH fleet awareness for Herald agents.

This module gives agents a live, structured view of every device on the
tailnet, enriched with SSH alias and capability metadata drawn from the
static SSH config and DEVICE_ACCESS.md.

Typical usage
-------------
    from herald.tailscale import fleet_status, ssh_reachable, get_device

    # Everything online right now
    devices = fleet_status()

    # Quick reachability check before `ssh pc hostname`
    if ssh_reachable("pc"):
        ...

    # Get a single device's full record
    device = get_device("configured-device")  # or by alias: get_device("pc")

Data returned
-------------
Each device dict has:
    hostname   – canonical Tailscale HostName
    tailscale_ips – list of Tailscale IPv4/v6 addresses
    os         – "linux" | "windows" | "android" | ...
    online     – bool (Tailscale has a live session)
    relay      – DERP relay in use ("ord", "nyc", etc.) or None
    rx_bytes / tx_bytes – traffic counters for this session
    last_seen  – ISO-8601 string or None
    last_handshake – ISO-8601 string or None
    ssh_alias  – primary SSH alias (e.g. "pc", "pi", "laptop") or None
    ssh_aliases – all aliases including alternates
    ssh_user   – OS user for SSH (e.g. "your-user") or None
    ssh_reachable – True if online and a configured SSH alias exists
    self       – True for the machine running this code
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
def _load_fleet_metadata() -> dict[str, dict[str, Any]]:
    """Load user-defined device/fleet metadata from ~/.herald/fleet.yaml or HERALD_FLEET_CONFIG."""
    config_path = Path(os.environ.get("HERALD_FLEET_CONFIG", "~/.herald/fleet.yaml")).expanduser()
    if not config_path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return data.get("devices", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


#: Map from lowercase HostName → SSH / fleet metadata (loaded dynamically from fleet config)
_SSH_META: dict[str, dict[str, Any]] = _load_fleet_metadata()


def _ssh_config_hosts(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Read concrete hosts from OpenSSH config without expanding wildcards.

    This intentionally implements only the small, useful subset needed for
    fleet discovery (Host, HostName and User).  It never reads private keys or
    executes Match/Include directives.
    """
    path = path or Path(os.environ.get("HERALD_SSH_CONFIG", "~/.ssh/config")).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return {}
    result: dict[str, dict[str, Any]] = {}
    current: list[str] = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = re.split(r"\s+", line, maxsplit=1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) == 2 else ""
        if key == "host":
            current = [h for h in value.split() if not any(c in h for c in "*!?")]
            for alias in current:
                result.setdefault(alias.lower(), {"alias": alias, "hostname": alias})
        elif key in {"hostname", "user"}:
            for alias in current:
                result[alias.lower()][key] = value
    return result


def _ssh_metadata(devices: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge maintained metadata with aliases discovered from SSH config."""
    merged = {host: {**meta, "aliases": list(meta.get("aliases", []))}
              for host, meta in _SSH_META.items()}
    by_ip = {ip.lower(): str(d.get("HostName", "")).lower() for d in devices
             for ip in d.get("TailscaleIPs", [])}
    known_hosts = {str(d.get("HostName", "")).lower(): str(d.get("HostName", "")).lower()
                   for d in devices if d.get("HostName")}
    for entry in _ssh_config_hosts().values():
        target = str(entry.get("hostname", "")).lower().rstrip(".")
        canonical = known_hosts.get(target) or by_ip.get(target)
        if not canonical:
            # Also accept the MagicDNS fully-qualified form.
            canonical = next((h for h in known_hosts if target.startswith(h + ".")), None)
        if not canonical:
            continue
        meta = merged.setdefault(canonical, {"aliases": []})
        alias = entry["alias"]
        if alias not in meta["aliases"]:
            meta["aliases"].append(alias)
        meta.setdefault("primary_alias", alias)
        if entry.get("user"):
            meta["user"] = entry["user"]
    return merged


# ---------------------------------------------------------------------------
# Tailscale JSON parsing
# ---------------------------------------------------------------------------

def _parse_ts_time(ts: str | None) -> str | None:
    """Return an ISO-8601 string, or None for zero/missing Tailscale timestamps."""
    if not ts or ts.startswith("0001-01-01"):
        return None
    return ts


def _build_device(
    peer: dict[str, Any], *, is_self: bool = False,
    ssh_meta: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    hostname_raw = peer.get("HostName", "")
    hostname = hostname_raw.lower()
    meta = (ssh_meta or _SSH_META).get(hostname, {})

    ips = peer.get("TailscaleIPs", [])
    online = True if is_self else bool(peer.get("Online", False))
    primary_alias = meta.get("primary_alias")
    ssh_aliases = meta.get("aliases", [])
    ssh_user = meta.get("user")

    return {
        "hostname": hostname_raw,
        "tailscale_ips": ips,
        "os": peer.get("OS", "unknown"),
        "online": online,
        "relay": peer.get("Relay") or None,
        "rx_bytes": peer.get("RxBytes", 0),
        "tx_bytes": peer.get("TxBytes", 0),
        "last_seen": _parse_ts_time(peer.get("LastSeen")),
        "last_handshake": _parse_ts_time(peer.get("LastHandshake")),
        "ssh_alias": primary_alias,
        "ssh_aliases": ssh_aliases,
        "ssh_user": ssh_user,
        # True when you can actually `ssh <alias>` right now:
        "ssh_reachable": online and bool(primary_alias),
        "self": is_self,
        "notes": meta.get("notes", ""),
        **({"warn": meta["warn"]} if "warn" in meta else {}),
    }


def _tailscale_json() -> dict[str, Any]:
    """Run `tailscale status --json` and return parsed output.

    Raises RuntimeError if tailscale is not found or returns a non-zero exit.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("tailscale CLI not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("tailscale status did not return within 10s") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"tailscale status failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fleet_status(*, include_offline: bool = True) -> list[dict[str, Any]]:
    """Return a list of device dicts for every node on the tailnet.

    Args:
        include_offline: When False, only return devices that are currently
                         online.

    Returns:
        List of device dicts (see module docstring for schema).  Self
        (this machine) is always first.
    """
    data = _tailscale_json()
    devices: list[dict[str, Any]] = []
    raw_peers = ([data["Self"]] if data.get("Self") else []) + list(data.get("Peer", {}).values())
    ssh_meta = _ssh_metadata(raw_peers)

    # Self
    self_peer = data.get("Self")
    if self_peer:
        devices.append(_build_device(self_peer, is_self=True, ssh_meta=ssh_meta))

    # Peers (keyed by node public key in the JSON)
    for peer in data.get("Peer", {}).values():
        device = _build_device(peer, ssh_meta=ssh_meta)
        if include_offline or device["online"]:
            devices.append(device)

    return devices


def _relationships(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = next((d["hostname"] for d in devices if d["self"]), "this-device")
    return [
        {
            "source": source,
            "target": d["hostname"],
            "kind": "ssh",
            "alias": d["ssh_alias"],
            "user": d["ssh_user"],
            "available": d["ssh_reachable"],
            "tailscale_ips": d["tailscale_ips"],
        }
        for d in devices if not d["self"] and d["ssh_alias"]
    ]


def ssh_relationships(*, include_offline: bool = True) -> list[dict[str, Any]]:
    """Describe SSH edges from this Herald host to tailnet devices.

    An edge means OpenSSH knows an alias for the target. ``available`` reflects
    current Tailscale presence; it is awareness, not an intrusive port probe.
    """
    return _relationships(fleet_status(include_offline=include_offline))


def get_device(name: str) -> dict[str, Any] | None:
    """Look up a single device by hostname or SSH alias.

    Args:
        name: Tailscale hostname (case-insensitive) or SSH alias
              (e.g. "pc", "pi", "laptop", "server").

    Returns:
        Device dict, or None if not found.
    """
    key = name.lower()
    for device in fleet_status():
        aliases = {str(alias).lower() for alias in device.get("ssh_aliases", [])}
        primary = str(device.get("ssh_alias") or "").lower()
        if device["hostname"].lower() == key or key in aliases or key == primary:
            return device
    return None


def ssh_reachable(name: str) -> bool:
    """Return True if the named device is currently reachable via SSH.

    Args:
        name: Hostname or SSH alias.

    Returns:
        True when the device is online and has a configured SSH alias.
    """
    device = get_device(name)
    return bool(device and device.get("ssh_reachable"))


def online_ssh_targets() -> list[dict[str, Any]]:
    """Return only devices that are online *and* have a valid SSH alias.

    Convenience helper for agents that iterate over reachable hosts.
    """
    return [d for d in fleet_status() if d.get("ssh_reachable")]


def _format_summary(devices: list[dict[str, Any]]) -> str:
    lines = ["Tailscale fleet status:"]
    for d in devices:
        tag = "SELF" if d["self"] else ("ONLINE" if d["online"] else "offline")
        alias = d["ssh_alias"] or "-"
        ips = ", ".join(d["tailscale_ips"][:1])
        line = (
            f"  {d['hostname']:20s}  {ips:17s}  OS={d['os']:8s}  {tag:7s}  ssh={alias}"
        )
        if d.get("warn"):
            line += f"  [!] {d['warn']}"
        lines.append(line)
    return "\n".join(lines)


def fleet_summary() -> str:
    """Return a compact human-readable table of fleet status.

    Suitable for injecting into a system prompt or agent context block.
    """
    return _format_summary(fleet_status())


def agent_context() -> str:
    """Compact, failure-tolerant device context suitable for every agent turn."""
    try:
        devices = fleet_status()
        summary = _format_summary(devices)
        edges = _relationships(devices)
    except (RuntimeError, json.JSONDecodeError) as exc:
        return f"Tailscale fleet status unavailable: {exc}"
    if edges:
        summary += "\nSSH relationships: " + "; ".join(
            f"{e['source']} -> {e['target']} via ssh {e['alias']} "
            f"({'available' if e['available'] else 'offline'})" for e in edges
        )
    return summary
