"""What the user is doing on each device right now, from the devices' own signals.

Every node's signals (signals.py) publish app.assistant.signal.focus / .idle / .active to Herald's
event bus. On the main node this follows Herald's live stream and keeps, per device, the app in
focus, since when, and whether the user is idle. The life clock reads it to hold the user to a
close deadline (life_clock.accountability).

app.assistant.signal.desktop carries each device's focused window and open windows (titles,
workspaces); desktop_context() turns them into the "Desktop right now" block every turn starts
with, the assistant's passive context.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import httpx

from .settings import settings

LOG = logging.getLogger(__name__)
PREFIX = "app.assistant.signal."


class Activity:
    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def observe(self, event_type: str, payload: dict[str, Any], at: float | None = None) -> None:
        if not event_type.startswith(PREFIX):
            return
        device = str(payload.get("device") or "device")
        at = at or time.time()
        with self._lock:
            state = self.devices.setdefault(device, {"process": "", "since": at, "idle": False})
            kind = event_type[len(PREFIX):]
            if kind == "focus":
                process = str(payload.get("process") or "")
                if process != state["process"]:
                    state.update(process=process, since=at)
                state["idle"] = False
            elif kind == "idle":
                state["idle"] = True
            elif kind == "active":
                state["idle"] = False
            elif kind == "desktop":
                state["desktop"] = {"focused": payload.get("focused"), "windows": list(payload.get("windows") or [])}
                state["desktop_at"] = at
            state["seen"] = at

    def current(self) -> dict[str, dict[str, Any]]:
        """{device: {"process", "minutes" in focus, "idle"}} for devices heard from in the last hour."""
        now = time.time()
        with self._lock:
            return {device: {"process": s["process"], "minutes": (now - s["since"]) / 60, "idle": s["idle"]}
                    for device, s in self.devices.items() if now - s.get("seen", 0) < 3600}

    def desktop_context(self, max_age: float = 1800) -> str:
        """What is on the user's devices right now, for the start of a turn ('' when nothing is known)."""
        now = time.time()
        lines = []
        with self._lock:
            devices = sorted(self.devices.items())
        for device, state in devices:
            desktop = state.get("desktop")
            if not desktop or now - state.get("desktop_at", 0) > max_age:
                continue
            idle = " (idle)" if state.get("idle") else ""
            focused = desktop.get("focused")
            lines.append(f"- {device}{idle}: focused " + (_describe(focused) if focused else "nothing"))
            others = [w for w in desktop.get("windows") or [] if w != focused]
            if others:
                lines.append("  open: " + "; ".join(_describe(w) for w in others))
        return "\n".join(lines)

    def follow(self) -> None:
        """Follow Herald's event stream until the process exits (reconnects on its own)."""
        def run() -> None:
            key = os.environ.get("HERALD_API_KEY")
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            while True:
                try:
                    url = settings.herald_url.rstrip("/") + "/event-bus/stream"
                    with httpx.stream("GET", url, headers=headers, timeout=httpx.Timeout(10, read=None)) as response:
                        for line in response.iter_lines():
                            if line.startswith("data: "):
                                event = json.loads(line[6:])
                                self.observe(str(event.get("event_type", "")), event.get("payload") or {})
                except Exception as exc:  # noqa: BLE001 - the Router restarts; follow again
                    LOG.debug("Activity stream lost: %s", exc)
                time.sleep(10)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=run, name="activity", daemon=True)
            self._thread.start()


def _describe(window: dict[str, Any]) -> str:
    app, title, workspace = (str(window.get(k) or "") for k in ("app", "title", "workspace"))
    text = f"{app}: {title}" if app and title else app or title or "a window"
    text += f" <{window['url']}>" if window.get("url") else ""
    return text + (f" [workspace {workspace}]" if workspace else "")


activity = Activity()
