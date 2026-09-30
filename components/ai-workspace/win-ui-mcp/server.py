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


def _focus_hwnd(hwnd: int) -> None:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    win32api.Sleep(100)


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


@mcp.tool()
def list_windows() -> list[dict[str, Any]]:
    """List all visible top-level windows with their title, handle, and PID."""
    return _enum_top_level_windows()


@mcp.tool()
def focus_window(title: str) -> dict[str, Any]:
    """Bring a window to the foreground by partial title match."""
    try:
        hwnd, actual = _find_window_by_partial_title(title)
        _focus_hwnd(hwnd)
        return {"ok": True, "title": actual, "handle": hwnd}
    except WindowNotFoundError:
        return _window_error(title)
    except Exception as exc:
        return _exc_error(f"focus_window {title!r}", exc)


@mcp.tool()
def type_text(window_title: str, text: str) -> dict[str, Any]:
    """Focus a window and type text into it."""
    try:
        hwnd, _ = _find_window_by_partial_title(window_title)
        _focus_hwnd(hwnd)
        pyautogui.typewrite(text, interval=0.02)
        return {"ok": True, "chars_sent": len(text)}
    except WindowNotFoundError:
        return _window_error(window_title)
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
    """Send a global hotkey to whatever is focused (not tied to a window title).
    Syntax matches send_keys: '^c', '%{F4}', '{ENTER}', '^+{ESC}'."""
    try:
        pyautogui.hotkey(*_parse_send_keys(keys))
        return {"ok": True, "keys": keys}
    except Exception as exc:
        return _exc_error(f"press_hotkey {keys!r}", exc)


@mcp.tool()
def type_now(text: str) -> dict[str, Any]:
    """Type text into whatever currently has focus (no window targeting)."""
    try:
        pyautogui.typewrite(text, interval=0.02)
        return {"ok": True, "chars_sent": len(text)}
    except Exception as exc:
        return _exc_error("type_now", exc)


if __name__ == "__main__":
    mcp.run()
