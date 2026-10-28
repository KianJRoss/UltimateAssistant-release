"""Teaching recordings: the user showed the assistant a task once, and it learns from that.

A device's recorder (teach.py) watches the user do the task: which windows they used, the
controls they used by name (AT-SPI), what they typed into which field (passwords hidden), and a
screenshot at each step. The recording comes here, on the main node, where an agent turns it into
a skill with parameters (learn_from_recording in skill_tools.py): not a replay of clicks, the
procedure with what could differ next time made changeable.

Recordings live on disk next to the user's data, so the web API and the skill tools (a separate
process on the main node) see the same ones:

  <data dir>/teach/<id>/recording.json   steps, note, device, read flag
  <data dir>/teach/<id>/<n>.jpg|png      the screenshots
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .settings import settings

STEP_KINDS = {"window", "focus", "activate", "text", "toggle", "note"}
MAX_STEPS = 300
MAX_IMAGES = 40
MAX_IMAGE = 8_000_000  # bytes of one screenshot
KEEP = 20               # recordings kept
KEEP_SECONDS = 14 * 24 * 3600
_lock = threading.Lock()


def root() -> Path:
    # The skill tools run as their own process; the app tells them where its recordings are.
    configured = os.environ.get("ULTIMATE_ASSISTANT_TEACH_HOME")
    return Path(configured) if configured else settings.user_data_dir / "teach"


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _step(step: dict[str, Any], images: int) -> dict[str, Any]:
    if not isinstance(step, dict) or step.get("kind") not in STEP_KINDS:
        raise ValueError(f"each step needs a kind from {sorted(STEP_KINDS)}")
    out: dict[str, Any] = {"kind": step["kind"]}
    if isinstance(step.get("at"), (int, float)):
        out["at"] = float(step["at"])
    for key, limit in (("app", 120), ("title", 300), ("name", 300), ("role", 60), ("text", 2000)):
        if step.get(key):
            out[key] = _text(step[key], limit)
    if step.get("secret") is True:
        out["secret"] = True
        out.pop("text", None)
    shot = step.get("screenshot")
    if shot is not None:
        if not isinstance(shot, int) or not 0 <= shot < images:
            raise ValueError("a step's screenshot must index the recording's images")
        out["screenshot"] = shot
    return out


def save(body: dict[str, Any]) -> dict[str, Any]:
    """Keep a recording a device sent; returns its summary."""
    device = _text(body.get("device"), 64).strip().lower()
    if not device:
        raise ValueError("device is required")
    images = body.get("images") or []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError(f"images: up to {MAX_IMAGES}")
    decoded = []
    for image in images:
        try:
            data = base64.b64decode(str((image or {}).get("data") or ""), validate=True)
        except ValueError as exc:
            raise ValueError("images: base64 data") from exc
        media = str(image.get("type") or "image/jpeg")
        if media not in ("image/jpeg", "image/png") or not 0 < len(data) <= MAX_IMAGE:
            raise ValueError(f"images: JPEG or PNG up to {MAX_IMAGE} bytes")
        decoded.append((media, data))
    steps = body.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"steps: a list of 1-{MAX_STEPS} steps")
    record = {"id": "teach-" + uuid.uuid4().hex[:10], "at": time.time(), "device": device,
              "note": _text(body.get("note"), 1000), "started_at": body.get("started_at"),
              "seconds": body.get("seconds"), "steps": [_step(s, len(decoded)) for s in steps],
              "images": [], "read": False}
    with _lock:
        folder = root() / record["id"]
        folder.mkdir(parents=True)
        for index, (media, data) in enumerate(decoded):
            name = f"{index}.{'png' if media == 'image/png' else 'jpg'}"
            (folder / name).write_bytes(data)
            record["images"].append({"file": name, "type": media})
        (folder / "recording.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        _prune()
    return summary(record)


def _prune() -> None:
    folders = sorted((p for p in root().glob("teach-*") if (p / "recording.json").is_file()),
                     key=lambda p: (p / "recording.json").stat().st_mtime, reverse=True)
    cutoff = time.time() - KEEP_SECONDS
    for index, folder in enumerate(folders):
        if index >= KEEP or (folder / "recording.json").stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


def _load(folder: Path) -> dict[str, Any] | None:
    try:
        return json.loads((folder / "recording.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def get(recording_id: str) -> dict[str, Any] | None:
    if not recording_id.startswith("teach-") or "/" in recording_id or ".." in recording_id:
        return None
    return _load(root() / recording_id)


def image(recording_id: str, index: int) -> tuple[bytes, str] | None:
    record = get(recording_id)
    if record is None or not 0 <= index < len(record["images"]):
        return None
    entry = record["images"][index]
    return (root() / recording_id / entry["file"]).read_bytes(), entry["type"]


def recordings(*, device: str | None = None, unread: bool = False, take: bool = False) -> list[dict[str, Any]]:
    """Recent recordings, newest first; `take` marks the returned ones read."""
    with _lock:
        found = []
        for folder in root().glob("teach-*") if root().is_dir() else []:
            record = _load(folder)
            if record and (not device or record["device"] == device.lower()) and (not unread or not record["read"]):
                found.append(record)
        found.sort(key=lambda r: r["at"], reverse=True)
        if take:
            for record in found:
                record["read"] = True
                (root() / record["id"] / "recording.json").write_text(json.dumps(record, ensure_ascii=False),
                                                                     encoding="utf-8")
        return [summary(r) for r in found]


def describe_step(number: int, step: dict[str, Any]) -> str:
    """One step in words: what the user did and where."""
    where = f" in {step['app']}" + (f" \"{step['title']}\"" if step.get("title") else "") if step.get("app") else ""
    control = f"{step.get('role') or 'control'} \"{step.get('name') or ''}\"".replace(' ""', "")
    kind = step["kind"]
    if kind == "window":
        line = f"switched to{where or ' a window'}"
    elif kind == "focus":
        line = f"went to {control}{where}"
    elif kind == "activate":
        line = f"used {control}{where}"
    elif kind == "toggle":
        line = f"changed {control}{where} to {step.get('text') or 'on/off'}"
    elif kind == "text":
        typed = "(hidden: a password)" if step.get("secret") else f"\"{step.get('text', '')}\""
        line = f"typed {typed} into {control}{where}"
    else:
        line = f"note: {step.get('text', '')}"
    shot = f" [screenshot {step['screenshot'] + 1}]" if "screenshot" in step else ""
    return f"{number}. {line}{shot}"


def summary(record: dict[str, Any]) -> dict[str, Any]:
    return {"id": record["id"], "device": record["device"], "at": record["at"], "note": record["note"],
            "seconds": record.get("seconds"), "steps": len(record["steps"]), "images": len(record["images"]),
            "read": record["read"]}


def steps_text(record: dict[str, Any]) -> str:
    head = f"Recording {record['id']} from {record['device']}"
    if record.get("seconds"):
        head += f", {round(record['seconds'])} s"
    if record.get("note"):
        head += f"; the user said what it is: {record['note']}"
    return head + "\n" + "\n".join(describe_step(n + 1, s) for n, s in enumerate(record["steps"]))


def context(records: list[dict[str, Any]]) -> str:
    """A device's unread recordings as guidance for its next turn (like canvas.marks_context)."""
    if not records:
        return ""
    lines = [f"- {r['id']}: {r['steps']} steps, {r['images']} screenshots"
             + (f", about: {r['note']}" if r.get("note") else "") for r in records[:3]]
    return ("The user just showed you a task on this device (teach mode):\n" + "\n".join(lines)
            + "\nTo learn it, call learn_from_recording(id): it returns the steps and screenshots. Ask what "
            "it's for if they haven't said, and what should change between runs.")
