"""Where the phone can turn when the main node is unreachable: desktops in emergency mode.

Each desktop node publishes a small emergency endpoint to the user's Tailscale network
(device side: phone_fallback.py) and registers its address here while the main node is up.
The paired phone fetches the list (GET /api/mobile/v1/fallbacks) and keeps it, so it still
knows where to go once this node is away. Entries a desktop hasn't renewed in a week drop out.

  <data dir>/phone-fallbacks.json   {node: {"url", "label", "at"}}
"""
from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlsplit

from ..settings import settings

STALE_SECONDS = 7 * 24 * 3600
_lock = threading.Lock()


def _file():
    return settings.user_data_dir / "phone-fallbacks.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def register(node: str, url: str, label: str = "") -> dict:
    node = str(node or "").strip().lower()[:40]
    parts = urlsplit(str(url or ""))
    if not node or parts.scheme != "https" or not parts.hostname or parts.username or parts.query or parts.fragment:
        raise ValueError("node and an https URL (no credentials, query or fragment) are required")
    entry = {"url": str(url).rstrip("/"), "label": str(label or node)[:80], "at": time.time()}
    with _lock:
        data = _load()
        data[node] = entry
        _file().parent.mkdir(parents=True, exist_ok=True)
        _file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"node": node, **entry}


def listing() -> list[dict]:
    """Live desktops, most recently renewed first."""
    cutoff = time.time() - STALE_SECONDS
    rows = [{"node": node, **entry} for node, entry in _load().items() if entry.get("at", 0) > cutoff]
    return sorted(rows, key=lambda row: row["at"], reverse=True)
