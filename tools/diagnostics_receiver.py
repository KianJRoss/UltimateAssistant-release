"""Small upload-only receiver. Run behind HTTPS; never serves saved reports."""
import io
import json
import os
import time
import uuid
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("ASSISTANT_DIAGNOSTICS_DIR", "reports")).resolve()
ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
REQUESTS = deque()


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(404, {"error": "Upload only"})

    def do_POST(self):
        now = time.time()
        while REQUESTS and REQUESTS[0] < now - 60:
            REQUESTS.popleft()
        if len(REQUESTS) >= 10:
            return self.reply(429, {"error": "Try later"})
        REQUESTS.append(now)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2 * 1024 * 1024 or self.headers.get("Content-Type") != "application/zip":
                return self.reply(413, {"error": "Invalid report size or type"})
            self.connection.settimeout(15)
            raw = self.rfile.read(size)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                items = archive.infolist()
                if len(items) != 2 or {item.filename for item in items} != {"system.json", "activity.json"}:
                    raise ValueError("Unexpected report files")
                if sum(item.file_size for item in items) > 4 * 1024 * 1024:
                    raise ValueError("Report too large")
                system = json.loads(archive.read("system.json"))
                activity = json.loads(archive.read("activity.json"))
                if system.get("schema_version") not in {1, 2} or not isinstance(activity, list) or len(activity) > 2000:
                    raise ValueError("Invalid report")
                if system["schema_version"] == 2:
                    allowed = {"schema_version", "created_at", "app_version", "python_version", "os", "os_release", "contents"}
                    if set(system) != allowed or any(not isinstance(v, str) or len(v) > 1000 for k, v in system.items() if k != "schema_version"):
                        raise ValueError("Unexpected system fields")
                    for event in activity:
                        if not isinstance(event, dict) or set(event) != {"status", "elapsed_seconds"}:
                            raise ValueError("Unexpected activity fields")
                        if event["status"] not in {"completed", "failed", "blocked", "cancelled", "activity"}:
                            raise ValueError("Invalid status")
                        duration = event["elapsed_seconds"]
                        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 <= duration <= 86400:
                            raise ValueError("Invalid duration")
            files = list(ROOT.glob("*.zip"))
            for file in files:
                if file.stat().st_mtime < now - 30 * 86400:
                    file.unlink()
            if sum(file.stat().st_size for file in ROOT.glob("*.zip")) + size > 128 * 1024 * 1024:
                return self.reply(507, {"error": "Report storage full"})
            receipt = uuid.uuid4().hex
            file = ROOT / (receipt + ".zip")
            with file.open("xb") as output:
                os.chmod(file, 0o600)
                output.write(raw)
            self.reply(201, {"receipt": receipt})
        except Exception:
            self.reply(400, {"error": "Invalid diagnostic report"})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 18891), Receiver).serve_forever()
