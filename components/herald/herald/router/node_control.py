"""Lifecycle control (load/unload/status) for local-model runtimes on other
nodes -- distinct from adapters.py, which only does inference calls. `lms`/
`ollama` CLI commands must run on the machine that actually hosts the
runtime, so these go over SSH to the node rather than as local subprocesses.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\r")

# Model keys/names look like "llama-3.1-8b-instruct" or "org/repo:tag".
# They are built as f-string fragments shipped to `ssh <node> <remote_cmd>`,
# where the remote shell interprets them -- there is no single quoting scheme
# that is safe against both POSIX shells and cmd.exe, so the defence is to
# never escape: just reject anything that isn't already safe.  A name that
# doesn't match this allowlist is refused before it ever reaches a command
# string.  Only alphanumerics plus '.', '_', ':', '/', '-' are permitted;
# the leading character must be alphanumeric so the string can't start with a
# flag or special shell token.
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")


class UnsafeModelNameError(ValueError):
    """Raised when a model name fails the shell-safety allowlist check."""


def _validate_model(model: str) -> str:
    """Return *model* unchanged if it is shell-safe, otherwise raise."""
    if not _SAFE_MODEL_RE.fullmatch(model):
        raise UnsafeModelNameError(
            f"model name {model!r} contains characters that are not safe to "
            "interpolate into a remote shell command; only alphanumerics and "
            "'.', '_', ':', '/', '@', '-' are allowed"
        )
    return model


def _strip_ansi(text: str) -> str:
    """lms's load/unload commands print a spinner via ANSI cursor/color
    codes -- fine in a terminal, noise in a JSON API response."""
    return _ANSI_RE.sub("", text).strip()

def _ssh_run(node: str, remote_cmd: str, timeout: int = 180) -> dict[str, Any]:
    if node in ("local", "localhost", "127.0.0.1"):
        try:
            proc = subprocess.run(
                remote_cmd, shell=True,
                capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            return {"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    from herald.tailscale import get_device
    device = get_device(node)
    alias = device.get("ssh_alias") or device.get("hostname") if device else node
    try:
        proc = subprocess.run(
            ["ssh", alias, remote_cmd],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ssh to {node} timed out after {timeout}s"}
    if proc.returncode != 0:
        return {"ok": False, "error": _strip_ansi(proc.stderr or proc.stdout)}
    return {"ok": True, "output": _strip_ansi(proc.stdout)}


def lmstudio_status(node: str = "local") -> dict[str, Any]:
    return _ssh_run(node, "lms ps")


def lmstudio_list_on_disk(node: str = "local") -> dict[str, Any]:
    return _ssh_run(node, "lms ls")


def lmstudio_load(model: str, node: str = "local") -> dict[str, Any]:
    return _ssh_run(node, f"lms load {_validate_model(model)}", timeout=300)


def lmstudio_unload(model: str, node: str = "local") -> dict[str, Any]:
    return _ssh_run(node, f"lms unload {_validate_model(model)}")


def ollama_status(node: str = "local") -> dict[str, Any]:
    return _ssh_run(node, "ollama ps")


def ollama_list_on_disk(node: str = "local") -> dict[str, Any]:
    return _ssh_run(node, "ollama list")


def ollama_load(node: str, model: str) -> dict[str, Any]:
    # Ollama's documented zero-generation load: an empty-string prompt to its
    # own REST API loads the model into memory without generating any
    # completion. `ollama run <model>` via a piped empty stdin mangles badly
    # through Windows' shell (the model receives a literal '""""' prompt and
    # actually answers it) -- this avoids shell quoting entirely.
    _validate_model(model)
    payload = '{\\"model\\":\\"' + model + '\\",\\"prompt\\":\\"\\"}'
    cmd = f"curl -s http://localhost:11434/api/generate -d \"{payload}\""
    return _ssh_run(node, cmd, timeout=300)


def ollama_unload(node: str, model: str) -> dict[str, Any]:
    return _ssh_run(node, f"ollama stop {_validate_model(model)}")


# ---------------------------------------------------------------------------
# Discovery -- every downloaded model, not just the ones currently loaded,
# so the registry can be populated with backends that just need `/control/
# local/load` before they're callable, rather than requiring each one to be
# hand-registered. `loaded` is checked at discovery time (lms ps / ollama ps)
# so the caller can decide enabled state -- registering a downloaded-but-
# unloaded model as immediately enabled would mean its first real call fails
# and trips its own circuit breaker for a state that isn't actually broken.
# ---------------------------------------------------------------------------

def lmstudio_discover(node: str = "local") -> list[dict[str, Any]]:
    ls_result = _ssh_run(node, "lms ls --json", timeout=30)
    if not ls_result["ok"]:
        return []
    try:
        models = json.loads(ls_result["output"])
    except json.JSONDecodeError:
        return []

    ps_result = _ssh_run(node, "lms ps --json", timeout=30)
    loaded_keys: set[str] = set()
    if ps_result["ok"]:
        try:
            loaded_keys = {m.get("modelKey") or m.get("path") for m in json.loads(ps_result["output"])}
        except json.JSONDecodeError:
            pass

    discovered = []
    for m in models:
        if m.get("type") != "llm":  # skip embedding models -- not chat-callable
            continue
        model_key = m["modelKey"]
        discovered.append({
            "runtime": "lmstudio", "model_key": model_key,
            "display_name": m.get("displayName", model_key),
            "loaded": model_key in loaded_keys,
        })
    return discovered


def ollama_discover(node: str = "local") -> list[dict[str, Any]]:
    tags_result = _ssh_run(node, "curl -s http://localhost:11434/api/tags", timeout=30)
    if not tags_result["ok"]:
        return []
    try:
        tags = json.loads(tags_result["output"])
    except json.JSONDecodeError:
        return []

    ps_result = _ssh_run(node, "curl -s http://localhost:11434/api/ps", timeout=30)
    loaded_names: set[str] = set()
    if ps_result["ok"]:
        try:
            loaded_names = {m.get("name") for m in json.loads(ps_result["output"]).get("models", [])}
        except json.JSONDecodeError:
            pass

    discovered = []
    for m in tags.get("models", []):
        name = m["name"]
        discovered.append({
            "runtime": "ollama", "model_key": name,
            "display_name": name,
            "loaded": name in loaded_names,
        })
    return discovered
