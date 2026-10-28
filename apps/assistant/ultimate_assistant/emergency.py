"""Emergency mode: a device answers on its own while the main node is unreachable.

Normally every turn goes to the main node through Herald (memory, agents, schedules, canvas).
When that path is down, the device hands the turn straight to a coding CLI signed in on it
(Claude Code, then Codex), with a prompt that says what happened: the main node is away, and
the CLI stands in for the whole assistant on this device until it returns. The CLI has its own
tools (shell, files), so it can still do things here; what needs the main node is said plainly.

Each emergency exchange is queued on the device and handed to the main node's conversation
when it answers again (POST /api/conversations/{id}/import), so nothing said offline is lost.
The device also keeps the last few exchanges it saw, so the stand-in knows what was going on.

  <data dir>/emergency-queue.jsonl   exchanges waiting for the main node
  <data dir>/emergency-recent.json   recent exchanges per conversation, for context
"""
from __future__ import annotations

from contextlib import closing
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from .settings import settings

LOG = logging.getLogger(__name__)
CLIS = ("claude", "codex")
CLI_TIMEOUT = 600
RECENT = 12  # exchanges kept per conversation
_lock = threading.Lock()


def _queue_file() -> Path:
    return settings.user_data_dir / "emergency-queue.jsonl"


def _recent_file() -> Path:
    return settings.user_data_dir / "emergency-recent.json"


def unreachable(exc: BaseException) -> bool:
    """Whether a failed call to the main node means it (or Herald) is away, not a bad request."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (502, 503, 504)


def remember(conversation_id: str, user: str, reply: str) -> None:
    """Keep an exchange the device saw, so a stand-in later knows the recent conversation."""
    with _lock:
        _remember(conversation_id, user, reply, time.time())


def _remember(conversation_id: str, user: str, reply: str, at: float) -> None:
    try:
        recent = json.loads(_recent_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        recent = {}
    exchanges = (recent.get(conversation_id) or [])[-(RECENT - 1):]
    exchanges.append({"user": user[:4000], "reply": reply[:4000], "at": at})
    # this conversation first; the 20 most recently used are kept
    recent = dict(list({conversation_id: exchanges, **{k: v for k, v in recent.items() if k != conversation_id}}.items())[:20])
    _recent_file().parent.mkdir(parents=True, exist_ok=True)
    _recent_file().write_text(json.dumps(recent, ensure_ascii=False), encoding="utf-8")


def _recent(conversation_id: str) -> list[dict]:
    try:
        return json.loads(_recent_file().read_text(encoding="utf-8")).get(conversation_id) or []
    except (OSError, ValueError):
        return []


def device_conversation() -> str:
    """The conversation this device's voice and text use (desktop.py keeps it), or a new one."""
    import uuid
    try:
        return str(uuid.UUID(json.loads((settings.user_data_dir / "desktop-conversation.json").read_text())["id"]))
    except (OSError, ValueError, KeyError, TypeError):
        return str(uuid.uuid4())


def queued() -> list[dict]:
    try:
        lines = _queue_file().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def _find(name: str) -> str | None:
    """The CLI on PATH, or where Omarchy/mise put it (a service's PATH is often shorter)."""
    home = Path.home()
    extra = [home / ".local" / "bin", home / ".local" / "share" / "mise" / "shims", home / ".npm-global" / "bin"]
    if os.name == "nt":
        extra.append(Path(os.environ.get("APPDATA", "")) / "npm")
    path = os.pathsep.join([os.environ.get("PATH", ""), *map(str, extra)])
    return shutil.which(name, path=path)


def _command(binary: str) -> list[str]:
    """How to start a CLI. An npm shim on Windows (codex.cmd) is a batch file, and a prompt full of
    quotes and ampersands can't safely pass through cmd.exe, so run its script with node directly."""
    if Path(binary).suffix.lower() not in (".cmd", ".bat"):
        return [binary]
    import re
    shim = Path(binary).read_text(encoding="utf-8", errors="replace")
    script = re.search(r'"%dp0%\\([^"]+\.(?:c?js|mjs))"', shim)
    node = Path(binary).with_name("node.exe")
    node = str(node) if node.is_file() else shutil.which("node")
    if not script or not node:
        raise RuntimeError(f"can't run {Path(binary).name} without cmd.exe")
    return [node, str(Path(binary).parent / script.group(1))]


def available() -> list[str]:
    return [name for name in CLIS if _find(name)]


def prompt(conversation_id: str, *, device: str, spoken: bool) -> str:
    """What the stand-in is told: what happened, what it can and can't do, and the recent talk."""
    lines = [
        "EMERGENCY MODE. You are standing in for Ultimate Assistant, the user's personal assistant, on "
        f"their device {device} ({platform.system()}). The assistant's main node (its memory, agents, "
        "schedules, calendar, other devices and the canvas) and its router are unreachable right now, so "
        "until they return you are the whole assistant on this device.",
        "Act as the assistant would: answer, and do things on this device with your tools when asked "
        "(shell, files, the desktop's own commands). You have none of the assistant's memory or other "
        "devices: say so plainly when a request needs them, never pretend, and say what should be done "
        "once the main node is back. This exchange is handed to it then, so it will see what happened.",
        f"It is {datetime.now().astimezone().strftime('%A %Y-%m-%d %H:%M %Z')}.",
    ]
    if spoken:
        lines.append("The user spoke this request and hears your reply: answer in a few short spoken sentences, "
                     "no lists or formatting.")
    history = _recent(conversation_id) + [{"user": q["user"], "reply": q["reply"]} for q in queued()
                                          if q["conversation_id"] == conversation_id]
    if history:
        lines.append("Recent conversation (oldest first):")
        for item in history[-RECENT:]:
            lines.append(f"User: {item['user']}\nAssistant: {item['reply']}")
    return "\n\n".join(lines)


def _run_cli(name: str, binary: str, text: str, system: str) -> str:
    home = str(Path.home())
    if name == "claude":
        command = [*_command(binary), "-p", text, "--append-system-prompt", system,
                   "--dangerously-skip-permissions", "--output-format", "text"]
        done = subprocess.run(command, cwd=home, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=CLI_TIMEOUT, encoding="utf-8", errors="replace")
        if done.returncode or not done.stdout.strip():
            raise RuntimeError(f"claude exited {done.returncode}: {(done.stderr or done.stdout)[-400:]}")
        return done.stdout.strip()
    with tempfile.TemporaryDirectory(prefix="ua-emergency-") as folder:
        last = Path(folder) / "reply.txt"
        command = [*_command(binary), "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                   "-C", home, "-o", str(last), f"{system}\n\nThe user says:\n{text}"]
        done = subprocess.run(command, cwd=home, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=CLI_TIMEOUT, encoding="utf-8", errors="replace")
        reply = last.read_text(encoding="utf-8").strip() if last.is_file() else ""
        if done.returncode or not reply:
            raise RuntimeError(f"codex exited {done.returncode}: {(done.stderr or done.stdout)[-400:]}")
        return reply


def answer(text: str, conversation_id: str, *, spoken: bool = False) -> dict:
    """Answer one turn on this device without the main node, and queue it for the main node."""
    from .node_link import node_label, node_name
    system = prompt(conversation_id, device=node_label(), spoken=spoken)
    failures = []
    for name in CLIS:
        binary = _find(name)
        if not binary:
            continue
        try:
            reply = _run_cli(name, binary, text, system)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            LOG.warning("Emergency answer through %s failed: %s", name, exc)
            failures.append(f"{name}: {exc}")
            continue
        item = {"conversation_id": conversation_id, "device": node_name(), "at": time.time(),
                "user": text, "reply": reply, "via": name}
        with _lock:
            _queue_file().parent.mkdir(parents=True, exist_ok=True)
            with _queue_file().open("a", encoding="utf-8") as queue:
                queue.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item
    raise RuntimeError("The main assistant is unreachable and no assistant CLI on this device could answer"
                       + (f" ({'; '.join(failures)})" if failures else " (none is installed)") + ".")


def flush(post) -> int:
    """Hand the queued exchanges to the main node; `post(conversation_id, messages)` raises on failure.
    Returns how many were handed over; the rest stay queued."""
    with _lock:
        items = queued()
        if not items:
            return 0
        by_conversation: dict[str, list[dict]] = {}
        for item in items:
            by_conversation.setdefault(item["conversation_id"], []).append(item)
        sent: set[str] = set()
        for conversation_id, group in by_conversation.items():
            messages = []
            for item in group:
                messages.append({"role": "user", "content": item["user"], "at": item["at"], "device": item["device"]})
                messages.append({"role": "assistant", "content": item["reply"], "at": item["at"],
                                 "device": item["device"], "via": item["via"]})
            try:
                post(conversation_id, messages)
            except Exception:  # noqa: BLE001 - still away: keep them for the next try
                LOG.info("Main node not ready for %d queued emergency exchange(s)", len(group))
                continue
            sent.add(conversation_id)
        left = [item for item in items if item["conversation_id"] not in sent]
        _queue_file().write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in left), encoding="utf-8")
        for conversation_id in sent:  # handed over: now part of the conversation this device saw
            for item in by_conversation[conversation_id]:
                _remember(conversation_id, item["user"], item["reply"], item["at"])
        return len(items) - len(left)


def main_session() -> httpx.Client:
    """A client for the main node through Herald, holding its session token (raises if it's away)."""
    import re
    key = os.environ.get("HERALD_API_KEY")
    client = httpx.Client(base_url=settings.herald_url.rstrip("/") + "/nodes/main/http/",
                          headers={"Authorization": f"Bearer {key}"} if key else {},
                          timeout=httpx.Timeout(10, read=30))
    try:
        page = client.get("")
        page.raise_for_status()
        token = re.search(r'<meta name="app-token" content="([^"]+)"', page.text)
        if not token:
            raise RuntimeError("The main assistant did not provide its session token.")
        client.headers["x-ultimate-assistant-token"] = token.group(1)
        return client
    except BaseException:
        client.close()
        raise


def _post_import(conversation_id: str, messages: list[dict]) -> None:
    with closing(main_session()) as client:  # already used, so closing() rather than its own `with`
        client.post(f"api/conversations/{conversation_id}/import", json={"messages": messages}).raise_for_status()


def start_flusher(interval: float = 60.0) -> None:
    """Keep trying to hand queued exchanges over while any wait (runs in the node process)."""
    def run() -> None:
        while True:
            time.sleep(interval)
            if queued():
                handed = flush(_post_import)
                if handed:
                    LOG.info("Handed %d emergency exchange(s) to the main node", handed)
    threading.Thread(target=run, name="emergency-flush", daemon=True).start()
