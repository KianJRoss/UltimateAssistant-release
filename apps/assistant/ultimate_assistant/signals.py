"""Signals: small, debounced facts about what the user is doing on this device.

Published to Herald's event bus as app.assistant.signal.* events with low
importance (they never notify anyone by themselves). The assistant reacts to
them only through event schedules the user asked for (schedule_agent with an
event_type and, usually, a payload filter), e.g. "when I open a game during
study time, remind me what's due". See ARCHITECTURE.md (Proactivity).

The desktop event is the assistant's passive context (HERALD_FAMILY_SPEC.md, "Awareness"): the
focused window and the open windows' titles and workspaces, so every turn knows what is on the
user's devices without looking. When the focused window is a Chromium-family browser, its page's
URL rides along, read from the browser's own History by the page title. Window contents and the screen never travel here; reading those
is active context (context_use.py).

  app.assistant.signal.focus   {"device", "process"}         settled in an app for a minute
  app.assistant.signal.idle    {"device", "minutes"}         no input for a while
  app.assistant.signal.active  {"device", "idle_minutes"}    back after being idle
  app.assistant.signal.desktop {"device", "focused", "windows"}  on change, and every 10 minutes
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
DESKTOP_REFRESH_SECONDS = 600  # the desktop is sent again unchanged so the main node never goes stale
MAX_WINDOWS = 25
MAX_TITLE = 160
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


def _hyprland_socket() -> str:
    """Hyprland's request socket. The instance in this process's environment goes stale when
    Hyprland restarts under a running service, so fall back to the newest live instance."""
    root = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "hypr")
    instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    path = os.path.join(root, instance, ".socket.sock")
    if instance and os.path.exists(path):
        return path
    if not os.path.isdir(root):
        return ""
    candidates = [os.path.join(root, name, ".socket.sock") for name in os.listdir(root)]
    return max((c for c in candidates if os.path.exists(c)), key=os.path.getmtime, default="")


def _hyprland_foreground_process() -> str:
    import json
    import socket
    path = _hyprland_socket()
    if not path:
        return ""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(path)
        sock.sendall(b"j/activewindow")
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    pid = (json.loads(data or b"{}") or {}).get("pid")  # {} when nothing has focus
    if not isinstance(pid, int) or pid <= 0:
        return ""
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as comm:
                return comm.read().strip()
        except OSError:
            return ""


def _hyprland_request(request: bytes):
    import json
    import socket
    path = _hyprland_socket()
    if not path:
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(path)
        sock.sendall(request)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return json.loads(data or b"null")


def _window(app: str, title: str, workspace: str) -> dict:
    return {"app": str(app or "")[:80], "title": str(title or "")[:MAX_TITLE], "workspace": str(workspace or "")}


def _hyprland_desktop() -> dict:
    """The focused window and the open windows, most recently focused first."""
    clients = [c for c in (_hyprland_request(b"j/clients") or []) if isinstance(c, dict) and c.get("mapped", True)]
    clients.sort(key=lambda c: c["focusHistoryID"] if c.get("focusHistoryID", -1) >= 0 else 1 << 30)  # 0 = focused
    windows = [_window(c.get("class"), c.get("title"), (c.get("workspace") or {}).get("name"))
               for c in clients if not c.get("hidden")]
    active = _hyprland_request(b"j/activewindow") or {}
    focused = (_window(active.get("class"), active.get("title"), (active.get("workspace") or {}).get("name"))
               if isinstance(active, dict) and active.get("address") else None)
    return {"focused": _with_url(focused), "windows": windows[:MAX_WINDOWS]}


def _windows_desktop() -> dict:
    """Windows: the foreground window and the visible, titled top-level windows."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    def title(hwnd) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    found: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4) and title(hwnd):  # 4 = GW_OWNER
            found.append({"hwnd": hwnd, "title": title(hwnd)})
        return True

    user32.EnumWindows(each, 0)
    foreground = user32.GetForegroundWindow()
    app = _foreground_process()
    focused = _window(app, title(foreground), "") if foreground and title(foreground) else None
    windows = [_window("", w["title"], "") for w in found if w["title"] not in ("Program Manager",)]
    return {"focused": _with_url(focused), "windows": windows[:MAX_WINDOWS]}


# Chromium-family browsers keep each profile's History (SQLite) under these folders; the window
# title is "<page title> - <browser>". The page's URL comes from the browser's own record of the
# page, matched by title, so no extension or debugging port is needed.
_BROWSERS = {
    "brave": ("Brave", ["~/.config/BraveSoftware/Brave-Browser", "%LOCALAPPDATA%/BraveSoftware/Brave-Browser/User Data"]),
    "chromium": ("Chromium", ["~/.config/chromium", "%LOCALAPPDATA%/Chromium/User Data"]),
    "chrome": ("Google Chrome", ["~/.config/google-chrome", "%LOCALAPPDATA%/Google/Chrome/User Data"]),
    "vivaldi": ("Vivaldi", ["~/.config/vivaldi", "%LOCALAPPDATA%/Vivaldi/User Data"]),
    "msedge": ("Microsoft Edge", ["~/.config/microsoft-edge", "%LOCALAPPDATA%/Microsoft/Edge/User Data"]),
}
_url_cache: dict[tuple[str, str], str] = {}


def _browser_url(app: str, title: str) -> str:
    """The URL of the page a Chromium-family browser window shows, from that browser's History."""
    import glob
    import shutil
    import sqlite3
    import tempfile
    key = next((k for k in _BROWSERS if k in app.lower()), None)
    if not key or not title:
        return ""
    name, roots = _BROWSERS[key]
    page = title[: -len(f" - {name}")] if title.endswith(f" - {name}") else title
    if (key, page) in _url_cache:
        return _url_cache[(key, page)]
    best, best_time = "", -1
    for root in roots:
        root = os.path.expandvars(os.path.expanduser(root))
        for history in glob.glob(os.path.join(root, "*", "History")):
            try:
                with tempfile.TemporaryDirectory() as tmp:  # the browser holds a lock on the live file
                    copy = os.path.join(tmp, "History")
                    shutil.copyfile(history, copy)
                    with sqlite3.connect(copy) as db:
                        row = db.execute("SELECT url, last_visit_time FROM urls WHERE title = ? "
                                         "ORDER BY last_visit_time DESC LIMIT 1", (page,)).fetchone()
            except (OSError, sqlite3.Error):
                continue
            if row and row[1] > best_time:
                best, best_time = row[0], row[1]
    if len(_url_cache) > 200:
        _url_cache.clear()
    if best:
        _url_cache[(key, page)] = best
    return best[:500]


def _with_url(window: dict | None) -> dict | None:
    if window:
        url = _browser_url(window.get("app", ""), window.get("title", ""))
        if url:
            window = {**window, "url": url}
    return window


class _WaylandIdle:
    """Seconds without input on Wayland. There is no last-input query, so swayidle
    (ext-idle-notify) reports when input stops for NOTICE seconds and when it resumes."""

    NOTICE = 60

    def __init__(self) -> None:
        import subprocess
        self._since: float | None = None
        try:
            self._process = subprocess.Popen(
                ["swayidle", "-w", "timeout", str(self.NOTICE), "echo idle", "resume", "echo active"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            LOG.warning("swayidle is not installed; idle signals are off on this device")
            return
        threading.Thread(target=self._read, name="assistant-idle", daemon=True).start()

    def _read(self) -> None:
        for line in self._process.stdout:
            self._since = time.time() - self.NOTICE if line.strip() == "idle" else None

    def seconds(self) -> float:
        return 0.0 if self._since is None else time.time() - self._since


class Signals:
    def __init__(self, device: str, emit) -> None:
        self.device, self.emit = device, emit
        self._candidate, self._since = "", 0.0
        self._announced: dict[str, float] = {}
        self._idle = False
        self._idle_started = 0.0
        self._desktop: dict | None = None
        self._desktop_sent = 0.0

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


    def desktop(self, now: float, snapshot: dict) -> None:
        """One look at the desktop; sent when it changed, and every few minutes regardless."""
        if snapshot == self._desktop and now - self._desktop_sent < DESKTOP_REFRESH_SECONDS:
            return
        self._desktop, self._desktop_sent = snapshot, now
        self.emit("app.assistant.signal.desktop", {"device": self.device, **snapshot})


def start(device: str) -> bool:
    """Publish this device's signals in the background (Windows and Hyprland; "signals": false turns it off)."""
    if sys.platform == "win32":
        foreground_process, idle_seconds, desktop = _foreground_process, _idle_seconds, _windows_desktop
    elif sys.platform.startswith("linux") and _hyprland_socket():
        foreground_process, idle_seconds, desktop = _hyprland_foreground_process, _WaylandIdle().seconds, _hyprland_desktop
    else:
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
                signals.tick(time.time(), foreground_process(), idle_seconds())
            except Exception:  # noqa: BLE001 - never let a signal glitch stop the loop
                LOG.debug("signal tick failed", exc_info=True)
            try:
                signals.desktop(time.time(), desktop())
            except Exception:  # noqa: BLE001
                LOG.debug("desktop snapshot failed", exc_info=True)
            time.sleep(15)

    threading.Thread(target=run, name="assistant-signals", daemon=True).start()
    return True
