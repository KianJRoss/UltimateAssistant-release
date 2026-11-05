"""Install the exact Herald wheels selected by the assistant, verifying their hashes."""
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
bundled = Path(__file__).parent / "wheels"
with tempfile.TemporaryDirectory() as directory:
    wheels = []
    for part in manifest["parts"]:
        if not part["url"].startswith("https://"):
            raise SystemExit("Herald wheel URLs must use HTTPS")
        wheel = Path(directory) / part["file"]
        if bundled.is_dir():
            # Release Docker archives carry the already-verified public wheels.
            # Never fall back to downloading if a supplied bundle is incomplete.
            data = (bundled / part["file"]).read_bytes()
        else:
            with urllib.request.urlopen(part["url"], timeout=120) as response:
                data = response.read()
        if hashlib.sha256(data).hexdigest() != part["sha256"]:
            raise SystemExit(f"Herald wheel checksum mismatch: {part['name']}")
        wheel.write_bytes(data)
        wheels.append(str(wheel))
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", *wheels], check=True)
