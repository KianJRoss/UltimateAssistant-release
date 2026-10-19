"""Signals: small, debounced facts about what the user is doing on this device.

Published to Herald's event bus as app.assistant.signal.* events with low
importance (they never notify anyone by themselves). The assistant reacts to
them only through event schedules the user asked for (schedule_agent with an
event_type and, usually, a payload filter), e.g. "when I open a game during
study time, remind me what's due". Only process names travel, never window
titles or contents. See ARCHITECTURE.md (Proactivity).

  app.assistant.signal.focus   {"device", "process"}         settled in an app for a minute
  app.assistant.signal.idle    {"device", "minutes"}         no input for a while
  app.assistant.signal.active  {"device", "idle_minutes"}    back after being idle
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

import httpx

LOG = logging.getLogger(__name__)
SETTLE_SECONDS = 60          # an app counts once the user stays in it this long
REPEAT_SECONDS = 600         # the same app isn't announced again within this window
IDLE_MINUTES = 10
# Foreground "apps" that aren't something the user chose to work in: input-sharing helpers
# (Mouse Without Borders owns the foreground while the cursor is on another PC), the shell,
# lock/search/start surfaces. They never count as settling into an app.
NOT_APPS = {name.lower() for name in (
    "MouseWithoutBordersHelper.exe", "MouseWithoutBorders.exe", "PowerToys.MouseWithoutBordersHelper.exe",
    "explorer.exe", "ShellExperienceHost.exe", "StartMenuExperienceHost.exe", "SearchHost.exe",
    "SearchApp.exe", "LockApp.exe", "LogonUI.exe", "TextInputHost.exe", "ApplicationFrameHost.exe",
    "dwm.exe", "csrss.exe")}


def _foreground_process() -> str:
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _idle_seconds() -> float:
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


class Signals:
    def __init__(self, device: str, emit) -> None:
        self.device, self.emit = device, emit
        self._candidate, self._since = "", 0.0
        self._announced: dict[str, float] = {}
        self._idle = False
        self._idle_started = 0.0

    def tick(self, now: float, process: str, idle: float) -> None:
        """One observation; emits what changed (kept separate from the OS calls so it's testable)."""
        if idle >= IDLE_MINUTES * 60:
            if not self._idle:
                self._idle, self._idle_started = True, now - idle
                self.emit("app.assistant.signal.idle", {"device": self.device, "minutes": int(idle // 60)})
            return
        if self._idle:
            self._idle = False
            away = int((now - self._idle_started) // 60)
            self.emit("app.assistant.signal.active", {"device": self.device, "idle_minutes": away})
        if process.lower() in NOT_APPS:
            process = ""
        if process != self._candidate:
            self._candidate, self._since = process, now
            return
        if (process and now - self._since >= SETTLE_SECONDS
                and now - self._announced.get(process, -REPEAT_SECONDS) >= REPEAT_SECONDS):
            self._announced[process] = now
            self.emit("app.assistant.signal.focus", {"device": self.device, "process": process})


def start(device: str) -> bool:
    """Publish this device's signals in the background (Windows; "signals": false turns it off)."""
    if sys.platform != "win32":
        return False

    def emit(event_type: str, payload: dict) -> None:
        from .settings import settings
        key = os.environ.get("HERALD_API_KEY")
        try:
            httpx.post(f"{(os.environ.get('HERALD_URL') or settings.herald_url).rstrip('/')}/event-bus/emit",
                       headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=10,
                       json={"event_type": event_type, "importance": 0.2, "payload": payload,
                             "source": "ultimate-assistant-signals"})
        except httpx.HTTPError:
            LOG.debug("Signal %s not delivered", event_type)

    signals = Signals(device, emit)

    def run() -> None:
        while True:
            try:
                signals.tick(time.time(), _foreground_process(), _idle_seconds())
            except Exception:  # noqa: BLE001 - never let a signal glitch stop the loop
                LOG.debug("signal tick failed", exc_info=True)
            time.sleep(15)

    threading.Thread(target=run, name="assistant-signals", daemon=True).start()
    return True
