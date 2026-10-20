"""What the user is doing on each device right now, from the devices' own signals.

Every node's signals (signals.py) publish app.assistant.signal.focus / .idle / .active to Herald's
event bus. On the main node this follows Herald's live stream and keeps, per device, the app in
focus, since when, and whether the user is idle. The life clock reads it to hold the user to a
close deadline (life_clock.accountability).
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
            state["seen"] = at

    def current(self) -> dict[str, dict[str, Any]]:
        """{device: {"process", "minutes" in focus, "idle"}} for devices heard from in the last hour."""
        now = time.time()
        with self._lock:
            return {device: {"process": s["process"], "minutes": (now - s["since"]) / 60, "idle": s["idle"]}
                    for device, s in self.devices.items() if now - s.get("seen", 0) < 3600}

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


activity = Activity()
