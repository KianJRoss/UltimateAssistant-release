"""Content-free support reports; automatic sharing requires revocable local consent."""
from contextlib import closing
from datetime import datetime, timezone
import io
import json
import math
import platform
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
import zipfile
import httpx
from .settings import settings
from .release_experience import REPORT_SCOPE, shared_checks

_LOCK = threading.RLock()
_INTERVAL = 24 * 60 * 60
DESCRIPTION = ("App/Python/OS versions, fixed activity status categories and elapsed seconds "
               "for the latest 2,000 actions, plus your fixed guided-feature test choices. No chats, labels, prompts, files, paths, account/model "
               "names, credentials, screenshots or raw logs. The receiving server sees your network IP.")


def report() -> dict:
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    system = {"schema_version": REPORT_SCOPE, "created_at": datetime.now(timezone.utc).isoformat(),
              "app_version": version_file.read_text().strip() if version_file.exists() else "development",
              "python_version": platform.python_version(), "os": platform.system(),
              "os_release": platform.release(), "contents": DESCRIPTION, "feature_checks": shared_checks()}
    events = []
    if settings.conversations_db.exists():
        with closing(sqlite3.connect(settings.conversations_db, timeout=2)) as connection:
            rows = connection.execute("SELECT elapsed_seconds,label FROM activity ORDER BY id DESC LIMIT 2000").fetchall()
            for elapsed, label in reversed(rows):
                ending = str(label).rsplit("·", 1)[-1].strip().upper()
                status = {"DONE": "completed", "COMPLETED": "completed", "FAILED": "failed",
                          "ERROR": "failed", "BLOCKED": "blocked", "CANCELLED": "cancelled"}.get(ending, "activity")
                duration = float(elapsed)
                events.append({"status": status, "elapsed_seconds": round(max(0, min(duration, 86400)), 2)
                               if math.isfinite(duration) else 0})
    return {"system": system, "activity": events}


def bundle(model: str = "") -> bytes:
    # Ignore the legacy model argument: backend names can identify users.
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in report().items():
            archive.writestr(name + ".json", json.dumps(value, indent=2))
    return output.getvalue()


def destination() -> dict:
    path = settings.user_settings_file.parent / "diagnostics-upload.json"
    if path.exists():
        config = json.loads(path.read_text("utf-8"))
    else:
        from .updater import diagnostic_destination
        config = {"url": diagnostic_destination(settings.user_settings_file.parent)}
    endpoint = config.get("url", "")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("Diagnostic uploads require an HTTPS endpoint without credentials or query parameters")
    return config


def preferences() -> dict:
    try:
        data = json.loads((settings.user_settings_file.parent / "diagnostics-consent.json").read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = settings.user_settings_file.parent / "diagnostics-consent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2), "utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def consent(enabled: bool, endpoint: str = "") -> dict:
    with _LOCK:
        if enabled and endpoint != destination()["url"]:
            raise ValueError("Support destination changed. Preview the report and approve again.")
        data = {"enabled": enabled, "url": endpoint if enabled else "", "schema_version": REPORT_SCOPE,
                "changed_at": datetime.now(timezone.utc).isoformat()}
        _save(data)
        return data


def upload(model: str = "", *, expected_url: str) -> dict:
    config = destination()
    if config["url"] != expected_url:
        raise ValueError("Support destination changed. Preview the report and approve again.")
    headers = {"Content-Type": "application/zip"}
    if config.get("token_ref"):
        from herald.router.account_registry import resolve_secret_ref
        headers["Authorization"] = "Bearer " + resolve_secret_ref(config["token_ref"])
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        result = client.post(config["url"], headers=headers, content=bundle())
    if not 200 <= result.status_code < 300:
        raise RuntimeError(f"Diagnostic upload was not accepted (HTTP {result.status_code})")
    return {"status": "accepted", "http_status": result.status_code}


def automatic_tick() -> None:
    # Serialize revocation with an in-flight send, bounded by the HTTP timeout.
    with _LOCK:
        data = preferences()
        if data.get("enabled") is not True or data.get("schema_version") != REPORT_SCOPE:
            return
        if time.time() - data.get("last_attempt", 0) < _INTERVAL:
            return
        data["last_attempt"] = time.time()
        _save(data)
        try:
            upload(expected_url=data.get("url", ""))
            data.update(last_sent=datetime.now(timezone.utc).isoformat(), last_result="accepted")
        except Exception:
            data["last_result"] = "not_sent"
        _save(data)


def start_reporter() -> threading.Event:
    stop = threading.Event()
    def run():
        while not stop.wait(60):
            try:
                automatic_tick()
            except Exception:
                pass  # Support reporting must never interrupt the assistant.
    threading.Thread(target=run, name="optional-support-reports", daemon=True).start()
    return stop
