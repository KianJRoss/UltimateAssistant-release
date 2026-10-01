"""Explicitly shared diagnostic bundles, excluding conversations and credentials."""
import io
from contextlib import closing
import json
import platform
import sqlite3
import zipfile
from urllib.parse import urlsplit

import httpx
from datetime import datetime, timezone
from pathlib import Path

from .settings import settings


def bundle(model: str) -> bytes:
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": version_file.read_text().strip() if version_file.exists() else "development",
        "python_version": platform.python_version(),
        "os": platform.system(), "os_release": platform.release(),
        "backend": model,
        "contents": "Version and timestamped action/status history only. No chat text, files, credentials, or raw provider logs.",
    }
    events = []
    if settings.conversations_db.exists():
        with closing(sqlite3.connect(settings.conversations_db)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT user_message_id,elapsed_seconds,label,created_at FROM activity ORDER BY id DESC LIMIT 2000").fetchall()
            events = [dict(row) for row in reversed(rows)]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("system.json", json.dumps(report, indent=2))
        archive.writestr("activity.json", json.dumps(events, indent=2))
    return output.getvalue()


def upload(model: str) -> dict:
    config_file = settings.user_settings_file.parent / "diagnostics-upload.json"
    if not config_file.exists():
        from .updater import diagnostic_destination
        endpoint = diagnostic_destination(settings.user_settings_file.parent)
        config = {"url": endpoint}
    else:
        config = json.loads(config_file.read_text("utf-8"))
    endpoint = config.get("url", "")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Diagnostic uploads require an HTTPS endpoint without embedded credentials")
    headers = {}
    if config.get("token_ref"):
        from herald.router.account_registry import resolve_secret_ref
        headers["Authorization"] = "Bearer " + resolve_secret_ref(config["token_ref"])
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        result = client.post(endpoint, headers={**headers, "Content-Type": "application/zip"}, content=bundle(model))
    if not 200 <= result.status_code < 300:
        raise RuntimeError(f"Diagnostic upload was not accepted (HTTP {result.status_code})")
    return {"status": "accepted", "http_status": result.status_code}
