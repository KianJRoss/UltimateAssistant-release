from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

import psutil
import pyautogui
import win32api
import win32con
import win32gui
import win32process


class WindowNotFoundError(Exception):
    pass


logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("win-ui")

mcp = FastMCP("win-ui")

ALLOWED_APPS = {
    "notepad.exe",
    "explorer.exe",
    "code.exe",
    "chrome.exe",
    "msedge.exe",
}

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

_KEY_NAME_MAP = {
    "ENTER": "enter", "TAB": "tab", "ESC": "esc", "ESCAPE": "esc",
    "SPACE": "space", "BACKSPACE": "backspace", "DELETE": "delete",
    "DEL": "delete", "UP": "up", "DOWN": "down", "LEFT": "left",
    "RIGHT": "right", "HOME": "home", "END": "end",
    "PGUP": "pageup", "PGDN": "pagedown", "PAGEUP": "pageup", "PAGEDOWN": "pagedown",
    "F1": "f1", "F2": "f2", "F3": "f3", "F4": "f4", "F5": "f5",
    "F6": "f6", "F7": "f7", "F8": "f8", "F9": "f9", "F10": "f10",
    "F11": "f11", "F12": "f12",
}

_MODIFIER_MAP = {"^": "ctrl", "%": "alt", "+": "shift"}


def _enum_top_level_windows() -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []

    def callback(hwnd: int, _: Any) -> bool:
        if not win32gui.IsWindow(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = None
        windows.append({
            "title": title,
            "handle": int(hwnd),
            "pid": pid,
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
        })
        return True

    win32gui.EnumWindows(callback, None)
    return windows


def _find_window_by_partial_title(query: str) -> tuple[int, str]:
    needle = query.strip().lower()
    if not needle:
        raise WindowNotFoundError("Window not found: empty title")
    windows = _enum_top_level_windows()
    matches = [w for w in windows if needle in str(w["title"]).lower()]
    if not matches:
        raise WindowNotFoundError(f"Window not found: {query}")
    exact = next((w for w in matches if str(w["title"]).lower() == needle), None)
    if exact:
        return int(exact["handle"]), str(exact["title"])
    starts = next((w for w in matches if str(w["title"]).lower().startswith(needle)), None)
    if starts:
        return int(starts["handle"]), str(starts["title"])
    return int(matches[0]["handle"]), str(matches[0]["title"])


def _normalize_key_name(token: str) -> str:
    if len(token) == 1:
        return token.lower()
    return _KEY_NAME_MAP.get(token.upper(), token.lower())


def _parse_send_keys(keys: str) -> tuple[str, ...]:
    sequence: list[str] = []
    index = 0
    while index < len(keys):
        char = keys[index]
        if char in _MODIFIER_MAP:
            sequence.append(_MODIFIER_MAP[char])
            index += 1
            continue
        if char == "{":
            end = keys.find("}", index)
            if end == -1:
                raise ValueError(f"Invalid key sequence: {keys}")
            token = keys[index + 1:end].strip()
            sequence.append(_normalize_key_name(token))
            index = end + 1
            continue
        sequence.append(_normalize_key_name(char))
        index += 1
    if not sequence:
        raise ValueError("Empty key sequence")
    return tuple(sequence)


class FocusRefusedError(Exception):
    pass


def _focus_hwnd(hwnd: int) -> None:
    """Bring a window to the front, or raise FocusRefusedError.

    Windows refuses SetForegroundWindow from background processes; attaching
    to the foreground thread's input queue is the supported way around that.
    Callers that send keystrokes must not continue if this raises: the keys
    would land in whatever the user is using."""
    import ctypes
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if win32gui.GetForegroundWindow() == hwnd:
        return
    foreground = win32gui.GetForegroundWindow()
    current = win32api.GetCurrentThreadId()
    target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
    fg_thread = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
    attached = []
    try:
        for thread in {fg_thread, target_thread} - {0, current}:
            if ctypes.windll.user32.AttachThreadInput(current, thread, True):
                attached.append(thread)
        win32gui.BringWindowToTop(hwnd)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001 - checked below
            pass
    finally:
        for thread in attached:
            ctypes.windll.user32.AttachThreadInput(current, thread, False)
    for _ in range(10):
        if win32gui.GetForegroundWindow() == hwnd:
            return
        win32api.Sleep(30)
    raise FocusRefusedError("Windows did not give this window the keyboard; nothing was typed. "
                            "Use ui_set_value / ui_invoke / send_keys_background, which need no focus.")


def _window_error(title: str) -> dict[str, Any]:
    return {"ok": False, "error": f"Window not found: {title}"}


def _exc_error(msg: str, exc: Exception) -> dict[str, Any]:
    logger.exception(msg)
    return {"ok": False, "error": str(exc)}


def _count_children(hwnd: int) -> int:
    count = 0
    def cb(h, _):
        nonlocal count
        if win32gui.IsWindow(h):
            count += 1
        return True
    win32gui.EnumChildWindows(hwnd, cb, None)
    return count


def _is_cloaked(hwnd: int) -> bool:
    """DWM-cloaked windows (other virtual desktops, suspended UWP shells) report
    IsWindowVisible but are not on screen."""
    import ctypes
    cloaked = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    except OSError:
        return False
    return bool(cloaked.value)


def _monitors() -> list[tuple[int, int, int, int]]:
    # Same order as mss / perception-mcp's `monitor` numbering (1..N).
    return [tuple(info[2]) for info in win32api.EnumDisplayMonitors()]


def _visible_fraction(rect: tuple[int, int, int, int], above: list[tuple[int, int, int, int]],
                      screens: list[tuple[int, int, int, int]], samples: int = 24) -> float:
    """Share of the window that is on a monitor and not covered by windows above it."""
    l, t, r, b = rect
    if r <= l or b <= t:
        return 0.0
    shown = total = 0
    for i in range(samples):
        x = l + (r - l) * (i + 0.5) / samples
        for j in range(samples):
            y = t + (b - t) * (j + 0.5) / samples
            total += 1
            if not any(sl <= x < sr and st <= y < sb for sl, st, sr, sb in screens):
                continue
            if any(al <= x < ar and at <= y < ab for al, at, ar, ab in above):
                continue
            shown += 1
    return round(shown / total, 2)


@mcp.tool()
def list_windows(include_hidden: bool = False) -> list[dict[str, Any]]:
    """List open application windows front-to-back (z-order) with process, rect,
    monitor (same numbering as perception-mcp), minimized/foreground state, and
    visible_fraction: how much of the window is actually showing after windows
    in front of it are accounted for. Cross-check with perception's screenshot /
    OCR to see what is really on screen. include_hidden adds hidden, cloaked and
    tool windows (tray helpers, background shells)."""
    screens = _monitors()
    foreground = win32gui.GetForegroundWindow()
    covering: list[tuple[int, int, int, int]] = []
    result: list[dict[str, Any]] = []
    for w in _enum_top_level_windows():  # EnumWindows order is top-to-bottom z-order
        hwnd = int(w["handle"])
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        minimized = bool(win32gui.IsIconic(hwnd))
        shown = (w["visible"] and not _is_cloaked(hwnd)
                 and not ex_style & win32con.WS_EX_TOOLWINDOW)
        if not shown and not include_hidden:
            continue
        rect = tuple(win32gui.GetWindowRect(hwnd))
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        monitor = next((i + 1 for i, (sl, st, sr, sb) in enumerate(screens)
                        if sl <= cx < sr and st <= cy < sb), None)
        try:
            process = psutil.Process(w["pid"]).name() if w["pid"] else None
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process = None
        fraction = 0.0 if (minimized or not shown) else _visible_fraction(rect, covering, screens)
        result.append({
            "title": w["title"], "handle": hwnd, "pid": w["pid"], "process": process,
            "rect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]},
            "monitor": monitor, "minimized": minimized, "foreground": hwnd == foreground,
            "visible_fraction": fraction,
            **({} if shown else {"hidden": True}),
        })
        if shown and not minimized and w["title"] != "Program Manager":
            covering.append(rect)
    return result


@mcp.tool()
def focus_window(title: str) -> dict[str, Any]:
    """Bring a window to the foreground by partial title match."""
    try:
        hwnd, actual = _find_window_by_partial_title(title)
        _focus_hwnd(hwnd)
        return {"ok": True, "title": actual, "handle": hwnd}
    except WindowNotFoundError:
        return _window_error(title)
    except FocusRefusedError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _exc_error(f"focus_window {title!r}", exc)


@mcp.tool()
def type_text(window_title: str, text: str) -> dict[str, Any]:
    """Focus a window and type into it with real keystrokes. Takes the user's keyboard focus:
    prefer ui_set_value (no focus). Refuses (types nothing) if Windows won't focus the window."""
    try:
        hwnd, _ = _find_window_by_partial_title(window_title)
        _focus_hwnd(hwnd)
        pyautogui.typewrite(text, interval=0.02)
        return {"ok": True, "chars_sent": len(text)}
    except WindowNotFoundError:
        return _window_error(window_title)
    except FocusRefusedError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _exc_error(f"type_text {window_title!r}", exc)


@mcp.tool(name="send_keys")
def send_keys_tool(window_title: str, keys: str) -> dict[str, Any]:
    """Send key combination to a window. Examples: '^c' (Ctrl+C), '^v' (Ctrl+V), '{ENTER}', '%{F4}' (Alt+F4)."""
    try:
        hwnd, _ = _find_window_by_partial_title(window_title)
        _focus_hwnd(hwnd)
        pyautogui.hotkey(*_parse_send_keys(keys))
        return {"ok": True}
    except WindowNotFoundError:
        return _window_error(window_title)
    except FocusRefusedError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _exc_error(f"send_keys {window_title!r}", exc)


@mcp.tool()
def get_window_info(window_title: str) -> dict[str, Any]:
    """Get detailed info about a window: PID, process name, position, size."""
    try:
        hwnd, actual = _find_window_by_partial_title(window_title)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc_name = psutil.Process(pid).name()
        except Exception:
            proc_name = None
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return {
            "title": actual, "pid": pid, "process_name": proc_name,
            "rect": {"left": l, "top": t, "right": r, "bottom": b},
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
            "children_count": _count_children(hwnd),
        }
    except WindowNotFoundError:
        return _window_error(window_title)
    except Exception as exc:
        return _exc_error(f"get_window_info {window_title!r}", exc)


@mcp.tool()
def run_app(path: str, args: list[str] | None = None) -> dict[str, Any]:
    """Launch an allowed application. Allowed: notepad.exe, explorer.exe, code.exe, chrome.exe, msedge.exe."""
    try:
        exe_name = os.path.basename(path).lower()
        if exe_name not in ALLOWED_APPS:
            return {"ok": False, "error": f"Executable not allowed: {exe_name}. Allowed: {', '.join(ALLOWED_APPS)}"}
        proc = subprocess.Popen([path, *(args or [])])
        return {"ok": True, "pid": proc.pid}
    except Exception as exc:
        return _exc_error(f"run_app {path!r}", exc)


@mcp.tool()
def close_window(window_title: str) -> dict[str, Any]:
    """Gracefully close a window by sending WM_CLOSE."""
    try:
        hwnd, _ = _find_window_by_partial_title(window_title)
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return {"ok": True}
    except WindowNotFoundError:
        return _window_error(window_title)
    except Exception as exc:
        return _exc_error(f"close_window {window_title!r}", exc)


@mcp.tool()
def get_system_metrics() -> dict[str, Any]:
    """Get deep system metrics: per-core CPU, memory breakdown, disk per partition, network I/O, top processes by CPU and RAM."""
    import time

    try:
        # CPU
        cpu_per_core = psutil.cpu_percent(interval=0.5, percpu=True)
        cpu_freq = psutil.cpu_freq()
        cpu_count_logical = psutil.cpu_count(logical=True)
        cpu_count_physical = psutil.cpu_count(logical=False)

        # Memory
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        # Disk
        disks = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_gb": round(usage.total / 1e9, 1),
                    "used_gb": round(usage.used / 1e9, 1),
                    "free_gb": round(usage.free / 1e9, 1),
                    "percent": usage.percent,
                })
            except PermissionError:
                continue

        # Disk I/O
        disk_io = psutil.disk_io_counters()

        # Network I/O per interface
        net_io = psutil.net_io_counters(pernic=True)
        net_stats = {}
        for iface, counters in net_io.items():
            if counters.bytes_sent == 0 and counters.bytes_recv == 0:
                continue
            net_stats[iface] = {
                "sent_mb": round(counters.bytes_sent / 1e6, 1),
                "recv_mb": round(counters.bytes_recv / 1e6, 1),
                "packets_sent": counters.packets_sent,
                "packets_recv": counters.packets_recv,
            }

        # Top 10 processes by CPU
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Need a second sample for accurate cpu_percent
        time.sleep(0.3)
        proc_cpu = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                info = p.info
                if info['cpu_percent'] > 0 or info['memory_percent'] > 0.1:
                    proc_cpu.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        top_cpu = sorted(proc_cpu, key=lambda x: x.get('cpu_percent', 0), reverse=True)[:10]
        top_mem = sorted(proc_cpu, key=lambda x: x.get('memory_percent', 0), reverse=True)[:10]

        # GPU (try WMI)
        gpu_info = []
        try:
            import wmi
            w = wmi.WMI(namespace="root\\CIMV2")
            for gpu in w.Win32_VideoController():
                gpu_info.append({
                    "name": gpu.Name,
                    "driver_version": gpu.DriverVersion,
                    "adapter_ram_gb": round(int(gpu.AdapterRAM or 0) / 1e9, 1) if gpu.AdapterRAM else None,
                })
        except Exception:
            pass

        # Uptime
        boot_time = psutil.boot_time()
        uptime_seconds = int(time.time() - boot_time)
        uptime_h = uptime_seconds // 3600
        uptime_m = (uptime_seconds % 3600) // 60

        return {
            "cpu": {
                "overall_percent": round(sum(cpu_per_core) / len(cpu_per_core), 1),
                "per_core_percent": cpu_per_core,
                "logical_cores": cpu_count_logical,
                "physical_cores": cpu_count_physical,
                "freq_mhz": round(cpu_freq.current) if cpu_freq else None,
                "freq_max_mhz": round(cpu_freq.max) if cpu_freq else None,
            },
            "memory": {
                "total_gb": round(vm.total / 1e9, 1),
                "used_gb": round(vm.used / 1e9, 1),
                "available_gb": round(vm.available / 1e9, 1),
                "percent": vm.percent,
                "swap_total_gb": round(swap.total / 1e9, 1),
                "swap_used_gb": round(swap.used / 1e9, 1),
                "swap_percent": swap.percent,
            },
            "disks": disks,
            "disk_io": {
                "read_gb": round(disk_io.read_bytes / 1e9, 2) if disk_io else None,
                "write_gb": round(disk_io.write_bytes / 1e9, 2) if disk_io else None,
            },
            "network": net_stats,
            "gpu": gpu_info,
            "top_processes_by_cpu": top_cpu,
            "top_processes_by_memory": top_mem,
            "uptime": f"{uptime_h}h {uptime_m}m",
            "uptime_seconds": uptime_seconds,
        }
    except Exception as exc:
        return _exc_error("get_system_metrics", exc)


@mcp.tool()
def list_running_apps() -> dict[str, Any]:
    """List all running applications with their window titles, PIDs, and CPU/memory usage."""
    try:
        windows = _enum_top_level_windows()
        result = []
        for w in windows:
            if not w["visible"]:
                continue
            pid = w.get("pid")
            proc_info: dict[str, Any] = {}
            if pid:
                try:
                    p = psutil.Process(pid)
                    proc_info = {
                        "process_name": p.name(),
                        "cpu_percent": p.cpu_percent(interval=0),
                        "memory_mb": round(p.memory_info().rss / 1e6, 1),
                        "status": p.status(),
                        "exe": p.exe(),
                    }
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            result.append({**w, **proc_info})
        return {"count": len(result), "apps": result}
    except Exception as exc:
        return _exc_error("list_running_apps", exc)


@mcp.tool()
def scan_integration_surfaces() -> dict[str, Any]:
    """Scan running apps for available integration surfaces: localhost APIs, COM objects, debug ports, known protocols."""
    import socket

    KNOWN_PORTS = {
        9222: "Chrome/Edge DevTools Protocol",
        9229: "Node.js debugger",
        6463: "Discord RPC",
        4380: "Steam",
        8888: "Jupyter Notebook",
        8080: "Generic HTTP API",
        3000: "Node.js dev server",
        5000: "Flask/generic dev server",
        7700: "net-os node-agent",
        8765: "PyWinAuto MCP",
        11434: "Ollama",
        1234: "LM Studio",
        4000: "LiteLLM relay",
    }

    COM_OBJECTS = {
        "Excel.Application": "Microsoft Excel",
        "Word.Application": "Microsoft Word",
        "Outlook.Application": "Microsoft Outlook",
        "PowerPoint.Application": "Microsoft PowerPoint",
        "WScript.Shell": "Windows Script Host",
        "Shell.Application": "Windows Shell",
        "InternetExplorer.Application": "Internet Explorer",
    }

    surfaces = []

    # Check localhost ports
    port_results = []
    for port, description in KNOWN_PORTS.items():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.3)
            result = sock.connect_ex(("127.0.0.1", port))
            sock.close()
            port_results.append({
                "port": port,
                "description": description,
                "open": result == 0,
            })
        except Exception:
            port_results.append({"port": port, "description": description, "open": False})

    active_ports = [p for p in port_results if p["open"]]
    surfaces.append({"type": "localhost_apis", "active": active_ports, "scanned": port_results})

    # Check COM objects
    com_results = []
    try:
        import win32com.client
        for prog_id, name in COM_OBJECTS.items():
            try:
                win32com.client.GetActiveObject(prog_id)
                com_results.append({"prog_id": prog_id, "name": name, "running": True})
            except Exception:
                com_results.append({"prog_id": prog_id, "name": name, "running": False})
    except ImportError:
        com_results = [{"error": "win32com not available"}]

    active_com = [c for c in com_results if c.get("running")]
    surfaces.append({"type": "com_objects", "active": active_com, "checked": com_results})

    # Check running processes for known integration-capable apps
    KNOWN_APPS = {
        "chrome.exe": {"integration": "Chrome DevTools Protocol", "port": 9222, "docs": "launch with --remote-debugging-port=9222"},
        "msedge.exe": {"integration": "Edge DevTools Protocol", "port": 9222, "docs": "launch with --remote-debugging-port=9222"},
        "code.exe": {"integration": "VS Code Extension API / LSP", "port": None, "docs": "vscode.window, vscode.workspace APIs"},
        "spotify.exe": {"integration": "Spotify Web API / MPRIS", "port": 4381, "docs": "localhost Spotify Connect API"},
        "discord.exe": {"integration": "Discord RPC", "port": 6463, "docs": "discord-rpc or discord.py"},
        "steam.exe": {"integration": "Steam Web API / Steamworks", "port": 4380, "docs": "steamapi, ISteamFriends"},
        "obs64.exe": {"integration": "OBS WebSocket API", "port": 4455, "docs": "obs-websocket plugin"},
        "slack.exe": {"integration": "Slack API / webhooks", "port": None, "docs": "slack_sdk"},
        "notepad.exe": {"integration": "Win32 SendMessage / pywinauto", "port": None, "docs": "pywinauto fallback"},
        "explorer.exe": {"integration": "Windows Shell COM", "port": None, "docs": "Shell.Application COM"},
        "taskmgr.exe": {"integration": "WMI / psutil", "port": None, "docs": "psutil, wmi module"},
        "python.exe": {"integration": "Debug protocol / RPC", "port": None, "docs": "debugpy, rpyc"},
        "node.exe": {"integration": "Node.js debugger", "port": 9229, "docs": "node --inspect"},
    }

    running_procs = set()
    try:
        for p in psutil.process_iter(['name']):
            try:
                running_procs.add(p.info['name'].lower())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass

    app_surfaces = []
    no_api_apps = []
    for proc_name, info in KNOWN_APPS.items():
        if proc_name.lower() in running_procs:
            if info.get("port"):
                # Check if port is already open
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.3)
                port_open = sock.connect_ex(("127.0.0.1", info["port"])) == 0
                sock.close()
                info = {**info, "port_active": port_open}
            app_surfaces.append({"process": proc_name, **info})
            if not info.get("port") and info["integration"] and "pywinauto" in info["integration"]:
                no_api_apps.append(proc_name)

    surfaces.append({"type": "running_app_integrations", "apps": app_surfaces})

    # Find unknown running apps with windows (pywinauto vision candidates)
    windows = _enum_top_level_windows()
    visible_procs = set()
    for w in windows:
        if w.get("visible") and w.get("pid"):
            try:
                name = psutil.Process(w["pid"]).name().lower()
                visible_procs.add(name)
            except Exception:
                pass

    unknown_windowed = [p for p in visible_procs if p not in KNOWN_APPS]
    surfaces.append({
        "type": "pywinauto_vision_candidates",
        "description": "Windowed apps with no known API — use PyWinAuto vision pipeline",
        "apps": unknown_windowed,
    })

    return {
        "summary": {
            "active_localhost_apis": len(active_ports),
            "active_com_objects": len(active_com),
            "known_app_integrations": len(app_surfaces),
            "vision_fallback_candidates": len(unknown_windowed),
        },
        "surfaces": surfaces,
    }


# ---------------------------------------------------------------------------
# Focus-free control: UI Automation and window messages. These act on a window
# without taking the user's keyboard or mouse, so the assistant can work while
# the user keeps using the PC (and on a monitor the user isn't looking at).
# ---------------------------------------------------------------------------

_PATH = __import__("re").compile(r"^\d+(\.\d+)*$")
_PATTERNS = ("Invoke", "Value", "Toggle", "ExpandCollapse", "SelectionItem", "Text", "Scroll", "RangeValue")


def _uia():
    import uiautomation as auto  # imported lazily: COM init happens in the calling thread
    auto.SetGlobalSearchTimeout(2)
    return auto


def _window_hwnd(window: str | int) -> int:
    if isinstance(window, int) or str(window).isdigit():
        hwnd = int(window)
        if not win32gui.IsWindow(hwnd):
            raise WindowNotFoundError(f"Window not found: handle {window}")
        return hwnd
    return _find_window_by_partial_title(str(window))[0]


def _patterns(control) -> list[str]:
    auto = _uia()
    found = []
    for name in _PATTERNS:
        try:
            if control.GetPattern(getattr(auto.PatternId, f"{name}Pattern")):
                found.append(name)
        except Exception:  # noqa: BLE001 - a pattern query may fail on odd providers
            continue
    return found


def _describe(control, path: str) -> dict[str, Any]:
    rect = control.BoundingRectangle
    item = {"path": path, "type": control.ControlTypeName.replace("Control", ""), "name": control.Name[:120],
            "automation_id": control.AutomationId[:80], "class": control.ClassName[:60],
            "enabled": bool(control.IsEnabled),
            "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom}}
    patterns = _patterns(control)
    if patterns:
        item["can"] = patterns
    if "Value" in patterns:
        try:
            item["value"] = control.GetValuePattern().Value[:200]
        except Exception:  # noqa: BLE001
            pass
    if "Toggle" in patterns:
        try:
            item["toggled"] = int(control.GetTogglePattern().ToggleState)
        except Exception:  # noqa: BLE001
            pass
    return item


def _resolve(root, element: str):
    """An element by tree path ("0.2.1" from ui_tree), automation id, or name."""
    element = (element or "").strip()
    if not element:
        return root
    if _PATH.match(element):
        node = root
        for index in (int(i) for i in element.split(".")):
            children = node.GetChildren()
            if index >= len(children):
                raise LookupError(f"No element at path {element}")
            node = children[index]
        return node
    auto = _uia()
    for criteria in ({"AutomationId": element}, {"Name": element}, {"SubName": element}):
        found = auto.Control(searchFromControl=root, searchDepth=30, **criteria)
        if found.Exists(0, 0):
            return found
    raise LookupError(f"No element matching {element!r}; use ui_tree to see what's there")


def _ui_call(window, fn):
    auto = _uia()
    with auto.UIAutomationInitializerInThread():
        try:
            root = auto.ControlFromHandle(_window_hwnd(window))
            return fn(root)
        except WindowNotFoundError as exc:
            return {"ok": False, "error": str(exc)}
        except LookupError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return _exc_error("ui call", exc)


@mcp.tool()
def ui_tree(window: str, depth: int = 4, max_nodes: int = 150) -> dict[str, Any]:
    """The controls inside a window (UI Automation), without focusing it: path, type, name,
    automation_id, value, what each can do (Invoke, Value, Toggle, ...). window: title text or
    handle from list_windows. Use the path/automation_id/name with ui_get, ui_set_value, ui_invoke."""
    def walk(root):
        out: list[dict[str, Any]] = []

        def visit(node, path, level):
            if len(out) >= max_nodes:
                return
            out.append(_describe(node, path))
            if level >= depth:
                return
            for i, child in enumerate(node.GetChildren()):
                visit(child, f"{path}.{i}" if path else str(i), level + 1)

        for i, child in enumerate(root.GetChildren()):
            visit(child, str(i), 1)
        return {"ok": True, "window": root.Name, "elements": out, "truncated": len(out) >= max_nodes}
    return _ui_call(window, walk)


@mcp.tool()
def ui_get(window: str, element: str = "") -> dict[str, Any]:
    """Read one control without focusing it: its value, toggle state, and text content
    (documents/editors, first 4000 characters)."""
    def get(root):
        control = _resolve(root, element)
        item = _describe(control, element)
        try:
            pattern = control.GetTextPattern()
            if pattern:
                item["text"] = pattern.DocumentRange.GetText(4000)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, **item}
    return _ui_call(window, get)


@mcp.tool()
def ui_set_value(window: str, element: str, text: str, append: bool = False) -> dict[str, Any]:
    """Put text into a field or editor without focusing it or typing (UI Automation Value pattern;
    falls back to a WM_SETTEXT window message for classic edit controls). Verifies the result."""
    def set_value(root):
        control = _resolve(root, element)
        new = text
        try:
            if append:
                new = (control.GetValuePattern().Value or "") + text
            control.GetValuePattern().SetValue(new)
            method = "uia-value"
        except Exception:  # noqa: BLE001 - not a Value-pattern control
            hwnd = control.NativeWindowHandle
            if not hwnd:
                return {"ok": False, "error": "This control can't take text without focus; "
                                              "use type_text (takes focus) after telling the user."}
            if append:
                length = win32gui.SendMessage(hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
                buffer = win32gui.PyMakeBuffer((length + 1) * 2)
                win32gui.SendMessage(hwnd, win32con.WM_GETTEXT, length + 1, buffer)
                new = buffer.tobytes().decode("utf-16-le", "ignore").rstrip("\x00") + text
            win32gui.SendMessage(hwnd, win32con.WM_SETTEXT, 0, new)
            method = "wm-settext"
        read = ""
        try:
            read = control.GetValuePattern().Value
        except Exception:  # noqa: BLE001
            try:
                read = control.GetTextPattern().DocumentRange.GetText(len(new) + 10)
            except Exception:  # noqa: BLE001
                pass
        verified = bool(read) and read.replace("\r", "").rstrip() == new.replace("\r", "").rstrip()
        return {"ok": True, "method": method, "verified": verified, "chars": len(new)}
    return _ui_call(window, set_value)


@mcp.tool()
def ui_invoke(window: str, element: str, action: str = "auto") -> dict[str, Any]:
    """Press a button, pick a menu item, tick a box, expand a list or select an item, without focus
    or the mouse. action: auto (best available) | invoke | toggle | expand | collapse | select."""
    def invoke(root):
        control = _resolve(root, element)
        order = {"auto": ("invoke", "toggle", "select", "expand", "default"),
                 "invoke": ("invoke",), "toggle": ("toggle",), "select": ("select",),
                 "expand": ("expand",), "collapse": ("collapse",)}.get(action)
        if not order:
            return {"ok": False, "error": f"unknown action {action!r}"}
        for kind in order:
            try:
                if kind == "invoke":
                    control.GetInvokePattern().Invoke()
                elif kind == "toggle":
                    control.GetTogglePattern().Toggle()
                elif kind == "select":
                    control.GetSelectionItemPattern().Select()
                elif kind == "expand":
                    control.GetExpandCollapsePattern().Expand()
                elif kind == "collapse":
                    control.GetExpandCollapsePattern().Collapse()
                else:
                    control.GetLegacyIAccessiblePattern().DoDefaultAction()
                return {"ok": True, "did": kind, "element": control.Name[:120]}
            except Exception:  # noqa: BLE001 - try the next way
                continue
        return {"ok": False, "error": f"{control.Name!r} supports none of: {', '.join(order)}"}
    return _ui_call(window, invoke)


@mcp.tool()
def send_keys_background(window: str, keys: str, element: str = "") -> dict[str, Any]:
    """Send keys to a window (or one of its controls) as window messages, without focusing it.
    Works for classic Windows apps (Notepad's text area, dialogs, most Win32 programs); many modern
    and browser apps ignore it. Same syntax as send_keys: plain text, '{ENTER}', '^s'."""
    def post(root):
        control = _resolve(root, element) if element else root
        hwnd = control.NativeWindowHandle or _window_hwnd(window)
        target = _deepest_native(hwnd) if not element else hwnd
        modifiers = {"ctrl": win32con.VK_CONTROL, "alt": win32con.VK_MENU, "shift": win32con.VK_SHIFT}
        held: list[int] = []
        sent = 0
        index = 0
        while index < len(keys):
            char = keys[index]
            if char in _MODIFIER_MAP:
                vk = modifiers[_MODIFIER_MAP[char]]
                win32api.PostMessage(target, win32con.WM_KEYDOWN, vk, 0)
                held.append(vk)
                index += 1
                continue
            if char == "{":
                end = keys.find("}", index)
                token = _normalize_key_name(keys[index + 1:end])
                vk = _VK_FOR.get(token)
                if vk is None:
                    return {"ok": False, "error": f"unknown key {token!r}"}
                win32api.PostMessage(target, win32con.WM_KEYDOWN, vk, 0)
                win32api.PostMessage(target, win32con.WM_KEYUP, vk, 0)
                index = end + 1
            else:
                if held:  # a shortcut like ^s: key down/up with modifiers held
                    vk = win32api.VkKeyScan(char) & 0xFF
                    win32api.PostMessage(target, win32con.WM_KEYDOWN, vk, 0)
                    win32api.PostMessage(target, win32con.WM_KEYUP, vk, 0)
                else:
                    win32api.PostMessage(target, win32con.WM_CHAR, ord(char), 0)
                index += 1
            for vk in reversed(held):
                win32api.PostMessage(target, win32con.WM_KEYUP, vk, 0)
            held.clear()
            sent += 1
        return {"ok": True, "sent": sent, "target_class": win32gui.GetClassName(target)}
    return _ui_call(window, post)


_VK_FOR = {"enter": win32con.VK_RETURN, "tab": win32con.VK_TAB, "esc": win32con.VK_ESCAPE,
           "space": win32con.VK_SPACE, "backspace": win32con.VK_BACK, "delete": win32con.VK_DELETE,
           "up": win32con.VK_UP, "down": win32con.VK_DOWN, "left": win32con.VK_LEFT, "right": win32con.VK_RIGHT,
           "home": win32con.VK_HOME, "end": win32con.VK_END, "pageup": win32con.VK_PRIOR,
           "pagedown": win32con.VK_NEXT, **{f"f{i}": getattr(win32con, f"VK_F{i}") for i in range(1, 13)}}


def _deepest_native(hwnd: int) -> int:
    """The child window that takes keyboard input (an app's edit/document control), else the window."""
    best = [hwnd]

    def visit(child, _):
        cls = win32gui.GetClassName(child).lower()
        if any(k in cls for k in ("edit", "richedit", "scintilla", "document")):
            best.append(child)
            return False
        return True

    try:
        win32gui.EnumChildWindows(hwnd, visit, None)
    except Exception:  # noqa: BLE001 - EnumChildWindows raises when the callback stops early
        pass
    return best[-1]


# ---------------------------------------------------------------------------
# Monitors and the assistant's own display. A virtual monitor (an IddCx virtual
# display driver) is the assistant's workspace: windows it works in live there,
# off the user's screens, and come over when they're ready.
# ---------------------------------------------------------------------------

def _settings_display() -> str:
    """Which monitor is the assistant's own: ULTIMATE_ASSISTANT_OPERATOR_DISPLAY or the
    "operator_display" setting (a monitor number or device name); default: the virtual one."""
    import json
    if os.environ.get("ULTIMATE_ASSISTANT_OPERATOR_DISPLAY"):
        return os.environ["ULTIMATE_ASSISTANT_OPERATOR_DISPLAY"]
    for folder in ("UltimateAssistantPersonal", "UltimateAssistant"):
        path = os.path.join(os.environ.get("LOCALAPPDATA", ""), folder, "settings.json")
        try:
            value = json.load(open(path, encoding="utf-8")).get("operator_display")
        except (OSError, ValueError):
            continue
        if value:
            return str(value)
    return ""


def _adapters() -> dict[str, str]:
    """Display device name (DISPLAY3 etc.) -> its adapter/driver ("Virtual Desktop Monitor", "NVIDIA ...")."""
    names: dict[str, str] = {}
    index = 0
    while True:
        try:
            device = win32api.EnumDisplayDevices(None, index)
        except Exception:  # noqa: BLE001 - past the last device
            break
        names[device.DeviceName] = device.DeviceString
        index += 1
    return names


def _monitor_list() -> list[dict[str, Any]]:
    monitors = []
    for index, (handle, _, rect) in enumerate(win32api.EnumDisplayMonitors(), start=1):
        info = win32api.GetMonitorInfo(handle)
        device = info.get("Device", "")
        adapter = _adapters().get(device, "")
        monitors.append({"monitor": index, "device": device, "adapter": adapter,
                         "primary": bool(info.get("Flags", 0) & 1),
                         "rect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]},
                         "virtual": "virtual" in adapter.lower() or "idd" in adapter.lower()})
    configured = _settings_display()
    for m in monitors:  # configured: monitor number, device name, or adapter name ("Virtual Display Driver")
        m["assistant_display"] = (configured in (m["device"], str(m["monitor"]))
                                  or configured.lower() in m["adapter"].lower()) if configured else m["virtual"]
    chosen = [m for m in monitors if m["assistant_display"]]
    for m in chosen[1:]:  # exactly one assistant display
        m["assistant_display"] = False
    return monitors


def _assistant_monitor() -> dict[str, Any] | None:
    return next((m for m in _monitor_list() if m["assistant_display"]), None)


def _move(hwnd: int, monitor: dict[str, Any], maximize: bool) -> None:
    r = monitor["rect"]
    if win32gui.IsIconic(hwnd) or win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    l, t, rr, b = win32gui.GetWindowRect(hwnd)
    width, height = min(rr - l, r["right"] - r["left"]), min(b - t, r["bottom"] - r["top"])
    flags = win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER
    win32gui.SetWindowPos(hwnd, 0, r["left"] + 40, r["top"] + 40, width, height, flags)
    if maximize:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWMAXNOACTIVE)


@mcp.tool()
def list_monitors() -> dict[str, Any]:
    """Monitors (same numbering as perception's screenshots), which is primary, which are virtual,
    and which one is the assistant's own display (its workspace off the user's screens)."""
    monitors = _monitor_list()
    return {"ok": True, "monitors": monitors,
            "assistant_display": next((m["monitor"] for m in monitors if m["assistant_display"]), None)}


@mcp.tool()
def move_window(window: str, monitor: int = 0, maximize: bool = False) -> dict[str, Any]:
    """Move a window to a monitor without focusing it. monitor 0 = the assistant's own display."""
    try:
        hwnd = _window_hwnd(window)
        monitors = _monitor_list()
        target = _assistant_monitor() if monitor == 0 else next((m for m in monitors if m["monitor"] == monitor), None)
        if target is None:
            return {"ok": False, "error": "No assistant display is set up; give a monitor number."
                    if monitor == 0 else f"No monitor {monitor}"}
        _move(hwnd, target, maximize)
        return {"ok": True, "monitor": target["monitor"], "title": win32gui.GetWindowText(hwnd)}
    except WindowNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _exc_error("move_window", exc)


@mcp.tool()
def bring_to_user(window: str, monitor: int = 0, focus: bool = False) -> dict[str, Any]:
    """Hand a finished window to the user: move it to their screen (monitor 0 = the primary, or the
    one they're using) and optionally focus it. Say so when you do; it appears in front of them."""
    try:
        hwnd = _window_hwnd(window)
        monitors = [m for m in _monitor_list() if not m["assistant_display"]]
        if monitor:
            target = next((m for m in monitors if m["monitor"] == monitor), None)
        else:
            fg = win32gui.GetForegroundWindow()
            cx = sum(win32gui.GetWindowRect(fg)[0::2]) / 2 if fg else 0
            target = next((m for m in monitors if m["rect"]["left"] <= cx < m["rect"]["right"]), None) \
                or next((m for m in monitors if m["primary"]), monitors[0] if monitors else None)
        if target is None:
            return {"ok": False, "error": "No user monitor found"}
        _move(hwnd, target, maximize=False)
        if focus:
            try:
                _focus_hwnd(hwnd)
            except FocusRefusedError:
                win32gui.FlashWindow(hwnd, True)
                return {"ok": True, "monitor": target["monitor"], "focused": False,
                        "note": "Windows kept focus on the user's app; the window is flashing in the taskbar."}
        return {"ok": True, "monitor": target["monitor"], "focused": focus}
    except WindowNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _exc_error("bring_to_user", exc)


@mcp.tool()
def open_app(command: str, args: list[str] | None = None, on_assistant_display: bool = True,
             wait_seconds: float = 8.0) -> dict[str, Any]:
    """Start an app without taking focus and return its new window(s). command: an exe, a path, or a
    URI/shell target (notepad, ms-settings:display, a file path, https://...). With
    on_assistant_display the new window opens on the assistant's own display when one exists."""
    import shutil
    import time
    before = {w["handle"] for w in _enum_top_level_windows()}
    previous = win32gui.GetForegroundWindow()
    try:
        exe = shutil.which(command) or shutil.which(command + ".exe")
        if exe:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = win32con.SW_SHOWNOACTIVATE
            subprocess.Popen([exe, *(args or [])], startupinfo=startup, close_fds=True)
        else:  # shell targets: URIs (ms-settings:, https:), documents, folders
            os.startfile(command, "open", " ".join(args or []) or None, None, win32con.SW_SHOWNOACTIVATE)
    except Exception as exc:  # noqa: BLE001
        return _exc_error(f"open_app {command!r}", exc)
    target = _assistant_monitor() if on_assistant_display else None
    deadline = time.time() + max(1.0, min(float(wait_seconds), 30.0))
    new: list[dict[str, Any]] = []
    while time.time() < deadline and not new:
        time.sleep(0.25)
        new = [w for w in _enum_top_level_windows()
               if w["handle"] not in before and w["visible"] and w["title"] != ""]
    for w in new:
        if target:
            try:
                _move(int(w["handle"]), target, maximize=False)
            except Exception:  # noqa: BLE001
                pass
    # Store apps (Windows 11 Notepad...) activate themselves despite SW_SHOWNOACTIVATE: hand the
    # keyboard straight back to whatever the user was in.
    focus_returned = False
    time.sleep(0.5)
    if previous and win32gui.IsWindow(previous) and win32gui.GetForegroundWindow() != previous:
        try:
            _focus_hwnd(previous)
            focus_returned = True
        except FocusRefusedError:
            pass
    return {"ok": True, "windows": [{"title": w["title"], "handle": w["handle"]} for w in new],
            "on_assistant_display": bool(target), "focus_returned_to_user": focus_returned,
            **({} if new else {"note": "no new window yet (it may have opened as a tab); check list_windows"})}


# ---------------------------------------------------------------------------
# Mouse + global input actuators — the "hands". Pair coordinates from the
# perception MCP (ocr_screen / find_on_screen) with click_at to act on what
# the agent sees.
# ---------------------------------------------------------------------------

@mcp.tool()
def move_mouse(x: int, y: int, duration: float = 0.0) -> dict[str, Any]:
    """Move the mouse cursor to absolute screen coordinates (x, y)."""
    try:
        pyautogui.moveTo(int(x), int(y), duration=max(0.0, float(duration)))
        return {"ok": True, "pos": {"x": int(x), "y": int(y)}}
    except Exception as exc:
        return _exc_error(f"move_mouse {x},{y}", exc)


@mcp.tool()
def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, Any]:
    """Click at absolute screen coordinates. button: left|right|middle. Feed
    coordinates from perception ocr_screen/find_on_screen to click an element."""
    try:
        b = button.lower()
        if b not in ("left", "right", "middle"):
            return {"ok": False, "error": f"bad button: {button}"}
        pyautogui.click(x=int(x), y=int(y), clicks=int(clicks), button=b, interval=0.05)
        return {"ok": True, "clicked": {"x": int(x), "y": int(y)}, "button": b, "clicks": int(clicks)}
    except Exception as exc:
        return _exc_error(f"click_at {x},{y}", exc)


@mcp.tool()
def double_click(x: int, y: int) -> dict[str, Any]:
    """Double-click at absolute screen coordinates."""
    try:
        pyautogui.doubleClick(x=int(x), y=int(y))
        return {"ok": True, "pos": {"x": int(x), "y": int(y)}}
    except Exception as exc:
        return _exc_error(f"double_click {x},{y}", exc)


@mcp.tool()
def scroll(amount: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    """Scroll the mouse wheel. Positive = up, negative = down. Optionally move to (x,y) first."""
    try:
        if x is not None and y is not None:
            pyautogui.moveTo(int(x), int(y))
        pyautogui.scroll(int(amount))
        return {"ok": True, "amount": int(amount)}
    except Exception as exc:
        return _exc_error("scroll", exc)


@mcp.tool()
def drag_to(start_x: int, start_y: int, end_x: int, end_y: int,
            duration: float = 0.3, button: str = "left") -> dict[str, Any]:
    """Drag from (start_x,start_y) to (end_x,end_y) with the given mouse button held."""
    try:
        pyautogui.moveTo(int(start_x), int(start_y))
        pyautogui.dragTo(int(end_x), int(end_y), duration=max(0.0, float(duration)), button=button.lower())
        return {"ok": True, "from": {"x": int(start_x), "y": int(start_y)},
                "to": {"x": int(end_x), "y": int(end_y)}}
    except Exception as exc:
        return _exc_error("drag_to", exc)


@mcp.tool()
def mouse_position() -> dict[str, Any]:
    """Get the current mouse cursor position."""
    try:
        pos = pyautogui.position()
        return {"ok": True, "x": pos.x, "y": pos.y}
    except Exception as exc:
        return _exc_error("mouse_position", exc)


@mcp.tool()
def press_hotkey(keys: str) -> dict[str, Any]:
    """Send a global hotkey to whatever has focus -- possibly the user's app; prefer send_keys_background.
    Syntax matches send_keys: '^c', '%{F4}', '{ENTER}', '^+{ESC}'."""
    try:
        pyautogui.hotkey(*_parse_send_keys(keys))
        return {"ok": True, "keys": keys}
    except Exception as exc:
        return _exc_error(f"press_hotkey {keys!r}", exc)


@mcp.tool()
def type_now(text: str) -> dict[str, Any]:
    """Type into whatever has focus right now -- possibly the app the user is using. Only after
    telling the user; prefer ui_set_value or type_text with a target window."""
    try:
        pyautogui.typewrite(text, interval=0.02)
        return {"ok": True, "chars_sent": len(text)}
    except Exception as exc:
        return _exc_error("type_now", exc)


if __name__ == "__main__":
    mcp.run()
