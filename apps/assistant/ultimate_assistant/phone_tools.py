"""The assistant's hands on the user's phone: what the paired phone app can do, sending it a
command and waiting for the result, and reading what it uploaded (files, screenshots)."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .phone import DEFAULT_DEVICE, phone_root
from .phone import services

mcp = FastMCP("ultimate-assistant-phone")
MAX_SOURCE_BYTES = 64 * 1024 * 1024


@mcp.tool()
def phone_status() -> str:
    """Whether the phone app is paired and connected, what the user granted it, and the commands
    it accepts. Check before phone_command; queue device_status for fresh evidence."""
    return services.describe()


@mcp.tool()
def pair_phone() -> str:
    """Pair the user's phone: a one-time invitation (5 minutes) as a QR code and link. Show the QR
    code on the canvas (qr_svg) for the user to scan in the phone app, or send the link to the phone.
    If the app isn't installed yet, give them phone_app's download link first (Android 8+).
    If the phone can't reach this computer yet, it says what's needed (set_up_phone_access)."""
    from .phone.pairing_invite import invitation
    return json.dumps(invitation(), ensure_ascii=False)


@mcp.tool()
def set_up_phone_access() -> str:
    """Let the phone reach the assistant: serve it to the user's own Tailscale network (HTTPS; not the
    internet). Needs Tailscale installed and signed in here and on the phone; ask before running."""
    from .phone.pairing_invite import set_up_access
    return json.dumps(set_up_access(), ensure_ascii=False)


@mcp.tool()
def phone_command(command: str, arguments: dict | None = None, wait_seconds: float = 30) -> str:
    """Send a command to the phone app (see phone_status for the list) and wait up to wait_seconds
    for its result. If it hasn't finished, the result has the command id to check again with
    phone_command_result. Ask before anything that deletes or sends on the user's behalf."""
    pairing = services.service("pairing").status(DEFAULT_DEVICE)
    if not pairing.get("paired") or pairing.get("pairing_revision") is None:
        return json.dumps({"state": "not_paired", "detail": "Pair the phone app first: on the computer open Settings > "
                                                            "Phone, then scan the code in the phone app."})
    try:
        queued = services.service("phone_commands").enqueue(
            command, arguments or {}, idempotency_key=uuid.uuid4().hex, expires_at=time.time() + 600,
            pairing_revision=pairing["pairing_revision"], correlation_id=uuid.uuid4().hex)
    except PermissionError as exc:
        return json.dumps({"state": "not_connected", "detail": f"The phone app isn't connected ({exc}). Ask the user to "
                                                               "open it (and Tailscale) on the phone."})
    return phone_command_result(queued["id"], wait_seconds)


@mcp.tool()
def phone_command_result(command_id: str, wait_seconds: float = 0) -> str:
    """A phone command's state and result (waits up to wait_seconds for it to finish)."""
    from .phone.control_store import TERMINAL_STATES
    control = services.service("control")
    deadline = time.time() + max(0.0, min(wait_seconds, 120))
    while True:
        record = control.get_command(command_id)
        if record is None:
            return json.dumps({"state": "unknown", "detail": "no command has that id"})
        if record.get("state") in TERMINAL_STATES or time.time() >= deadline:
            return json.dumps(record, ensure_ascii=False, default=str)
        time.sleep(1)


@mcp.tool()
def read_phone_source(source_id: str) -> str:
    """A file or screenshot the phone app uploaded (a source_id from a command result or a message):
    its path on this computer, checked against what the phone recorded, plus extracted text. Look
    at an image with your own image viewing tool on the returned path."""
    root = (phone_root() / "mobile" / "uploads").resolve()
    for metadata in root.glob("*/provenance.json"):
        if metadata.is_symlink() or not metadata.resolve().is_relative_to(root):
            raise ValueError("upload record is outside the phone's storage")
        record = json.loads(metadata.read_text(encoding="utf-8"))
        attachment = record.get("attachment", {})
        if attachment.get("source_id") != source_id:
            continue
        if record.get("device_id") != DEFAULT_DEVICE:
            raise ValueError("the upload was recorded for another device")
        files = list(metadata.parent.glob("content.*"))
        if len(files) != 1 or files[0].is_symlink():
            raise ValueError("the upload has no single stored file")
        path = files[0].resolve()
        if not path.is_relative_to(root) or path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("the upload is outside the phone's storage or too large")
        raw = path.read_bytes()
        if len(raw) != record.get("size") or hashlib.sha256(raw).hexdigest() != record.get("sha256"):
            raise ValueError("the stored file doesn't match what the phone recorded")
        result = {"path": str(path), "name": attachment.get("name"), "media_type": attachment.get("media_type"),
                  "bytes": len(raw), "sha256": record["sha256"], "source_id": source_id}
        try:
            from herald.router.ingestion import ingest_path
            evidence = ingest_path(Path(path)).to_dict()
            result["extracted_text"] = [str(text)[:30000] for text in evidence.get("extracted_text", [])][:20]
        except Exception as exc:  # noqa: BLE001 - the file itself is still readable
            result["extraction_error"] = str(exc)[:300]
        return json.dumps(result, ensure_ascii=False)
    raise ValueError("no phone upload has that source_id")


if __name__ == "__main__":
    mcp.run()
