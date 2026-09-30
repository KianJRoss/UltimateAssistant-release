"""herald.router — the router engine.

Importing this package gives you:
    from herald.router import start_server, get_app
"""
from __future__ import annotations
import subprocess
import sys
import os


def get_app():
    """Return the FastAPI app (for embedding in other servers or testing)."""
    from herald.router.server import app
    return app


def start_server(port: int = 8790, background: bool = True, host: str = "127.0.0.1"):
    """Start the router. background=True returns immediately."""
    if host == "0.0.0.0" and not os.environ.get("HERALD_API_KEY"):
        raise RuntimeError("HERALD_API_KEY is required when binding Herald to 0.0.0.0")
    env = os.environ.copy()
    env["HERALD_BIND_HOST"] = host
    cmd = [sys.executable, "-m", "uvicorn", "herald.router.server:app",
           "--host", host, "--port", str(port)]
    if background:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            env=env,
        )
    else:
        subprocess.run(cmd, env=env)
