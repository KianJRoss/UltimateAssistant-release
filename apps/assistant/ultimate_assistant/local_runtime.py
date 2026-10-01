"""Start the bundled Router with its own per-user state on loopback."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


def _runtime_mutex():
    """Own the app across legacy launchers and updated Python installations."""
    if os.name != "nt":
        return None, True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, True, "Local\\UltimateAssistant-LocalRuntime")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return None, False
    return (kernel, handle), True


def main() -> None:
    while _run_once():
        os.environ.pop("ULTIMATE_ASSISTANT_UPDATE_CHILD", None)
        os.environ["ULTIMATE_ASSISTANT_RESTARTING"] = "1"
        # Restart the same version with fresh UI state and executors. A changed
        # release selection dispatches its own Python process in _run_once.
        import importlib
        from . import web_ui
        importlib.reload(web_ui)


def _run_once() -> bool:
    data = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "UltimateAssistant"
    data.mkdir(parents=True, exist_ok=True)
    # Existing installations gain the same sign-in launcher as new installs.
    # Respect explicit opt-out and isolated preview launchers.
    if (os.name == "nt"
            and not os.environ.get("ULTIMATE_ASSISTANT_NO_SHORTCUT")
            and not os.environ.get("ULTIMATE_ASSISTANT_NO_STARTUP")):
        startup_script = Path(__file__).resolve().parents[1] / "configure-startup.ps1"
        if startup_script.exists():
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                     "-File", str(startup_script), *(["-Disable"] if (data / "startup-disabled").exists() else [])], capture_output=True, timeout=30,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                print("Could not register launch at sign-in; use configure-startup.ps1 to retry.")
    import json
    from .updater import check_update
    updates_file = data / "updates.json"
    if updates_file.exists():
        update_config = json.loads(updates_file.read_text("utf-8"))
        if update_config.get("automatic") and not os.environ.get("ULTIMATE_ASSISTANT_UPDATE_CHILD"):
            try:
                result = check_update(data, install=True)
            except Exception:
                result = {"status": "blocked", "detail": "Update check failed; current installation retained. Check repository access and internet."}
            (data / "update-status.json").write_text(json.dumps(result), "utf-8")
    selected_file = data / "current-release.json"
    if selected_file.exists():
        selected = json.loads(selected_file.read_text("utf-8"))
        target = Path(selected["path"])
        if target.resolve() != Path(__file__).resolve().parents[3]:
            code = subprocess.call([str(target / ".venvs/assistant/Scripts/python.exe"), "-m", "ultimate_assistant.local_runtime"],
                                   cwd=target / "apps/assistant", env={**os.environ, "ULTIMATE_ASSISTANT_UPDATE_CHILD": "1"})
            if code:
                from .updater import rollback
                rollback(data)
                print("Updated app failed; previous installation selected for next launch.")
            return False
    os.environ["HERALD_DATA_DIR"] = str(data / "router")
    os.environ["HERALD_BIND_HOST"] = "127.0.0.1"
    os.environ["HERALD_SKIP_BACKEND_DISCOVERY"] = "1"
    os.environ["HERALD_URL"] = "http://127.0.0.1:18790"
    os.environ.pop("HERALD_API_KEY", None)
    from .settings import settings
    from .native_workspace import prepare_native_workspace
    os.environ["HERALD_WORKSPACE"] = str(settings.assistant_files_root)
    app_root = str(Path(__file__).resolve().parents[1])
    import_paths = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if app_root not in import_paths:
        os.environ["PYTHONPATH"] = os.pathsep.join([app_root, *filter(None, import_paths)])
    os.environ["HERALD_G4F_ACCOUNTS_FILE"] = str(data / "router/g4f/accounts.json")
    os.environ["HERALD_G4F_GATEWAY_URL"] = "http://127.0.0.1:18795/v1"
    node_path = Path(__file__).resolve().parents[3] / ".runtime/node-path.txt"
    if node_path.exists():
        os.environ["PATH"] = node_path.read_text().strip() + os.pathsep + os.environ.get("PATH", "")
    prepare_native_workspace(Path(os.environ["HERALD_WORKSPACE"]))
    mutex, owned = _runtime_mutex()
    if not owned:
        import webbrowser
        webbrowser.open("http://127.0.0.1:8765")
        return False
    # Fixed application port: fail rather than adopt another running Router.
    import socket
    try:
        return _run_owned(data)
    finally:
        if mutex:
            kernel, handle = mutex
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


def _run_owned(data: Path) -> bool:
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 18790))
    restart = False
    with (data / "router-startup.log").open("a", encoding="utf-8") as log:
        router = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "herald.router.server:app", "--host", "127.0.0.1", "--port", "18790"],
            stdout=log, stderr=log, env=os.environ.copy(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            for _ in range(120):
                if router.poll() is not None:
                    raise RuntimeError(f"Bundled Router failed to start. See {data / 'router-startup.log'}")
                try:
                    if httpx.get(os.environ["HERALD_URL"] + "/v1/models", timeout=1).is_success:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.5)
            else:
                raise RuntimeError("Bundled Router startup timed out.")
            from .web_ui import main as run_ui
            from .browser_setup import start_playwright
            from .assistant import Assistant
            if Assistant._capability_enabled("playwright_enabled", default=False):
                try: start_playwright()
                except Exception: print("Playwright MCP is blocked; use browser setup to retry.")
            from . import g4f_setup
            try:g4f_setup.start()
            except Exception:print("Local g4f setup is blocked; use account setup to retry.")
            restart = bool(run_ui())
        finally:
            from .browser_setup import stop_playwright
            stop_playwright()
            from . import g4f_setup
            g4f_setup.stop()
            router.terminate()
            try:
                router.wait(timeout=10)
            except subprocess.TimeoutExpired:
                router.kill()
                router.wait()
    return restart


if __name__ == "__main__":
    main()
