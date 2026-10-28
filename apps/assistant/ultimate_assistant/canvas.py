"""The canvas: what the assistant is showing over the user's screens, on every device.

One store on the main node holds the live elements (text and markdown cards,
reminders, images, drawings, interactive "ui" cards, and "point" requests asking the user to
mark something on their screen), each aimed at devices
("pc", "phone", a node name, or "everywhere") and expiring on its own. Renderers
subscribe to a stream of changes: the PC overlay (components/canvas-overlay) on
the main node itself, other devices through Herald (/nodes/main/http/api/canvas/...).
Agents show things with the canvas tools (canvas_tools.py). See ARCHITECTURE.md.

A "ui" card is markup the agent writes (canvas_guide.md; templates in ui/templates; rendered by
ui/genui.js): it keeps its own state while the user clicks and types, and sends
events back (a message, a named action, close) that agents read with
canvas_events. Its state is saved here so agents can read what the user chose.

Pointing goes the other way: the user marks things on their screen (a point, a box, a freehand
stroke, a whole window) in the overlay's pointing mode, either because an agent asked (a "point"
element; canvas_point waits for it) or on their own (a hotkey, or while talking). The overlay sends
the marks with a screenshot that has them drawn in; the marks store keeps the recent ones, and a
device's unread marks ride along with that device's next turn.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

KINDS = {"text", "markdown", "reminder", "image", "drawing", "ui", "point"}
POSITIONS = {"top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right"}
PRIMITIVES = {"stroke", "rect", "ellipse", "line", "arrow", "label"}
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
MAX_ELEMENTS = 40
MAX_TTL = 24 * 3600
MAX_UI = 100_000          # characters of card markup
MAX_DATA = 64_000         # characters of JSON for a card's data or saved state
MAX_EVENTS = 200
EVENT_TYPES = {"message", "action", "close", "open_url", "error", "pointed"}
MARK_TYPES = {"point", "box", "draw", "window"}
MAX_MARKS = 50            # marks in one submission
MAX_MARK_RECORDS = 20     # submissions kept
MARK_RECORD_SECONDS = 3600
MAX_MARK_IMAGE = 8_000_000  # bytes of one screenshot


def _number(value: Any, name: str, low: float = 0.0, high: float = 1.0) -> float:
    if not isinstance(value, (int, float)) or not low <= float(value) <= high:
        raise ValueError(f"{name} must be a number from {low} to {high}")
    return float(value)


def validate(element: dict[str, Any]) -> dict[str, Any]:
    """A clean element, or ValueError. Coordinates are normalized (0..1) to the monitor."""
    kind = element.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    out: dict[str, Any] = {"kind": kind}
    element_id = str(element.get("id") or uuid.uuid4().hex[:12])
    if not _ID.match(element_id):
        raise ValueError("id: letters, digits, dot, dash, underscore (max 64)")
    out["id"] = element_id
    for key, limit in (("title", 200), ("body", 6000)):
        if element.get(key) is not None:
            text = str(element[key])
            if len(text) > limit:
                raise ValueError(f"{key} is longer than {limit} characters")
            out[key] = text
    if kind == "point":
        tools = element.get("tools") or sorted(MARK_TYPES)
        if not isinstance(tools, list) or not tools or not set(tools) <= MARK_TYPES:
            raise ValueError(f"tools must be a list from {sorted(MARK_TYPES)}")
        out["tools"] = [t for t in ("point", "box", "draw", "window") if t in tools]
    if kind in {"text", "markdown", "reminder"} and not (out.get("body") or out.get("title")):
        raise ValueError(f"a {kind} needs a title or body")
    if kind == "image":
        source = str(element.get("image") or "")
        if not (source.startswith(("https://", "http://")) or Path(source).is_file()):
            raise ValueError("image must be an http(s) URL or a file path on the main node")
        out["image"] = source
    if kind == "ui":
        template = element.get("template")
        if template:
            if template not in templates():
                raise ValueError(f"template must be one of {sorted(templates())}")
            out["template"] = template
            markup = templates()[template]
        else:
            markup = element.get("ui")
        if not isinstance(markup, str) or not markup.strip():
            raise ValueError("a ui card needs its markup in 'ui' or a 'template'")
        if len(markup) > MAX_UI:
            raise ValueError(f"ui is longer than {MAX_UI} characters")
        out["ui"] = markup
        out["data"] = _json_value(element.get("data") or {}, "data", dict)
        out["bare"] = bool(element.get("bare"))
        if element.get("conversation_id"):
            out["conversation_id"] = str(element["conversation_id"])[:64]
    if kind == "drawing":
        shapes = element.get("shapes")
        if not isinstance(shapes, list) or not 1 <= len(shapes) <= 256:
            raise ValueError("a drawing needs 1-256 shapes")
        out["shapes"] = [_shape(s) for s in shapes]
    devices = element.get("devices") or ["pc"]
    if isinstance(devices, str):
        devices = [devices]
    out["devices"] = [str(d).strip().lower() for d in devices if str(d).strip()][:8] or ["pc"]
    monitor = element.get("monitor", "active")
    if not (monitor in ("active", "all", "assistant") or (isinstance(monitor, int) and 1 <= monitor <= 16)):
        raise ValueError("monitor: active, all, assistant, or a monitor number")
    out["monitor"] = monitor
    position = element.get("position", "top-right")
    if isinstance(position, dict):
        out["position"] = {k: _number(position.get(k), f"position.{k}") for k in ("x", "y")}
        if "w" in position:
            out["position"]["w"] = _number(position["w"], "position.w", 0.02, 1.0)
    elif position in POSITIONS:
        out["position"] = position
    else:
        raise ValueError(f"position: one of {sorted(POSITIONS)} or {{x, y[, w]}} (0..1)")
    color = element.get("color")
    if color is not None:
        if not _COLOR.match(str(color)):
            raise ValueError("color must be #rrggbb or #rrggbbaa")
        out["color"] = str(color)
    for flag in ("draggable", "resizable", "monitor_locked"):
        value = element.get(flag, False)
        if type(value) is not bool:
            raise ValueError(f"{flag} must be a boolean")
        out[flag] = value
    ttl = element.get("ttl_seconds", 600 if kind in ("reminder", "point") else 120)
    out["ttl_seconds"] = int(_number(ttl, "ttl_seconds", 0, MAX_TTL))  # 0 = until cleared
    out["agent"] = str(element.get("agent") or "")[:40]
    return out


def templates() -> dict[str, str]:
    """Ready-made ui cards (ui/templates/*.ui): the agent sends only their data."""
    global _templates
    if _templates is None:
        folder = Path(__file__).parent / "ui" / "templates"
        _templates = {f.stem: f.read_text(encoding="utf-8") for f in sorted(folder.glob("*.ui"))}
    return _templates


_templates: dict[str, str] | None = None


def _json_value(value: Any, name: str, kind: type) -> Any:
    if not isinstance(value, kind):
        raise ValueError(f"{name} must be a JSON {'object' if kind is dict else 'list'}")
    try:
        size = len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be plain JSON") from exc
    if size > MAX_DATA:
        raise ValueError(f"{name} is larger than {MAX_DATA} characters of JSON")
    return value


def _shape(shape: dict[str, Any]) -> dict[str, Any]:
    kind = shape.get("type")
    if kind not in PRIMITIVES:
        raise ValueError(f"shape type must be one of {sorted(PRIMITIVES)}")
    out: dict[str, Any] = {"type": kind}
    if kind == "stroke":
        points = shape.get("points")
        if not isinstance(points, list) or not 2 <= len(points) <= 512:
            raise ValueError("a stroke needs 2-512 points")
        out["points"] = [[_number(p[0], "x"), _number(p[1], "y")] for p in points]
    else:
        for key in ("x", "y"):
            out[key] = _number(shape.get(key), f"{kind}.{key}")
        if kind in {"rect", "ellipse"}:
            out["w"], out["h"] = _number(shape.get("w"), "w"), _number(shape.get("h"), "h")
        if kind in {"line", "arrow"}:
            out["x2"], out["y2"] = _number(shape.get("x2"), "x2"), _number(shape.get("y2"), "y2")
        if kind == "label":
            out["text"] = str(shape.get("text", ""))[:300]
    if shape.get("color") is not None:
        if not _COLOR.match(str(shape["color"])):
            raise ValueError("shape color must be #rrggbb or #rrggbbaa")
        out["color"] = str(shape["color"])
    if shape.get("width") is not None:
        out["width"] = _number(shape["width"], "width", 1, 40)
    return out


def _mark(mark: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(mark, dict) or mark.get("type") not in MARK_TYPES:
        raise ValueError(f"each mark needs a type from {sorted(MARK_TYPES)}")
    out: dict[str, Any] = {"type": mark["type"], "monitor": str(mark.get("monitor") or "")[:64]}
    if mark["type"] == "draw":
        points = mark.get("points")
        if not isinstance(points, list) or not 2 <= len(points) <= 512:
            raise ValueError("a draw mark needs 2-512 points")
        out["points"] = [[round(_number(p[0], "x"), 4), round(_number(p[1], "y"), 4)] for p in points]
    else:
        for key in ("x", "y"):
            out[key] = round(_number(mark.get(key), f"{mark['type']}.{key}"), 4)
        if mark["type"] in ("box", "window"):
            out["w"], out["h"] = round(_number(mark.get("w"), "w"), 4), round(_number(mark.get("h"), "h"), 4)
    window = mark.get("window")
    if isinstance(window, dict):
        out["window"] = {k: str(window[k])[:300] for k in ("class", "title", "address") if window.get(k)}
    return out


def _mark_summary(index: int, mark: dict[str, Any]) -> str:
    """One mark in words, for the turn's context: where it is and what window it is over."""
    place = mark["monitor"] + " " if mark["monitor"] else ""
    if mark["type"] == "point":
        where = f"point at ({mark['x']:.2f}, {mark['y']:.2f})"
    elif mark["type"] == "draw":
        xs, ys = [p[0] for p in mark["points"]], [p[1] for p in mark["points"]]
        where = f"freehand mark around ({min(xs):.2f}, {min(ys):.2f})-({max(xs):.2f}, {max(ys):.2f})"
    else:
        where = (f"{'box' if mark['type'] == 'box' else 'window'} "
                 f"({mark['x']:.2f}, {mark['y']:.2f})-({mark['x'] + mark['w']:.2f}, {mark['y'] + mark['h']:.2f})")
    window = mark.get("window") or {}
    over = f" over {window.get('class', '')} \"{window.get('title', '')}\"" if window else ""
    return f"{index}. {place}{where}{over}"


def marks_context(records: list[dict[str, Any]]) -> str:
    """A device's unread marks as guidance for its next turn: served with the request, so the
    assistant knows what "this" means without asking for it. Screenshots accompany supported device turns automatically."""
    if not records:
        return ""
    lines = []
    for record in records[:3]:
        lines.append(f"Marks {record['id']}:\n{record['description']}")
    return ("The user pointed at things on this device's screen for this request (positions in 0-1 of "
            "the monitor; the same coordinates canvas drawings use):\n" + "\n".join(lines)
            + "\nThe attached screenshots show the marks. If no image is attached or you need it again, canvas_marks(id=...) retrieves it.")


def matches(element: dict[str, Any], device: str, *, main_node: bool) -> bool:
    devices = element["devices"]
    return ("everywhere" in devices or device in devices
            or (main_node and "pc" in devices))


class CanvasStore:
    """Live elements with expiry, and a revision counter renderers wait on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._elements: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._event_seq = 0
        self._marks: list[dict[str, Any]] = []
        self.removal_listeners = []
        self.revision = 0
        self._changed: asyncio.Condition | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _notify(self) -> None:
        self.revision += 1
        loop, changed = self._loop, self._changed
        if loop and changed:
            async def wake():
                async with changed:
                    changed.notify_all()
            try:
                asyncio.run_coroutine_threadsafe(wake(), loop)
            except RuntimeError:
                pass

    def _prune(self) -> None:
        now = time.time()
        expired = [k for k, e in self._elements.items() if e["expires_at"] and e["expires_at"] <= now]
        for key in expired:
            del self._elements[key]
        if expired:
            self._notify()

    def show(self, element: dict[str, Any]) -> dict[str, Any]:
        clean = validate(element)
        clean["expires_at"] = time.time() + clean["ttl_seconds"] if clean["ttl_seconds"] else 0
        clean["updated_at"] = time.time()
        with self._lock:
            self._prune()
            previous = self._elements.get(clean["id"])
            if clean["kind"] == "ui" and previous and previous.get("kind") == "ui" and "state" in previous:
                clean["state"] = previous["state"]  # updating a card in place keeps what the user did
            if clean["id"] not in self._elements and len(self._elements) >= MAX_ELEMENTS:
                oldest = min(self._elements.values(), key=lambda e: e["updated_at"])
                del self._elements[oldest["id"]]
            self._elements[clean["id"]] = clean
            self._notify()
        return clean

    def remove(self, element_id: str | None = None, *, device: str | None = None) -> int:
        with self._lock:
            if element_id:
                keys = [element_id]
                removed = 1 if self._elements.pop(element_id, None) else 0
            else:
                keys = [k for k, e in self._elements.items() if device is None or device in e["devices"]
                        or "everywhere" in e["devices"]]
                for key in keys:
                    del self._elements[key]
                removed = len(keys)
            if removed:
                self._notify()
        # Local close/clear wins over an in-flight data refresh. Call outside the store lock.
        for key in keys:
            for listener in self.removal_listeners:
                try:
                    listener(key)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception('Canvas removal listener failed')
        return removed

    def get(self, element_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune()
            element = self._elements.get(element_id)
            return dict(element) if element else None

    def set_state(self, element_id: str, state: list) -> bool:
        """Save a ui card's state (its useState values). No change notice: the card already shows it."""
        clean = _json_value(state, "state", list)
        with self._lock:
            element = self._elements.get(element_id)
            if not element or element.get("kind") != "ui":
                return False
            element["state"] = clean
            return True

    def add_event(self, element_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Record what the user did on a card; "close" also removes the card."""
        kind = event.get("type")
        if kind not in EVENT_TYPES:
            raise ValueError(f"event type must be one of {sorted(EVENT_TYPES)}")
        record: dict[str, Any] = {"type": kind, "id": element_id, "at": time.time()}
        for key, limit in (("text", 4000), ("name", 80), ("url", 2000)):
            if event.get(key) is not None:
                record[key] = str(event[key])[:limit]
        if event.get("data") is not None:
            record["data"] = _json_value({"v": event["data"]}, "data", dict)["v"]
        if isinstance(event.get("state"), list):
            self.set_state(element_id, event["state"])
        with self._lock:
            element = self._elements.get(element_id)
            if element:
                record["title"] = element.get("title", "")
                if "state" in element:
                    record["state"] = element["state"]
            self._event_seq += 1
            record["seq"] = self._event_seq
            self._events.append(record)
            del self._events[:-MAX_EVENTS]
            self._notify()
        if kind == "close":
            self.remove(element_id)
        return record

    def add_marks(self, body: dict[str, Any]) -> dict[str, Any]:
        """Keep what the user pointed at (marks in 0..1 of their monitor, plus screenshots with the
        marks drawn in). Answering a "point" request also tells whoever waits on it."""
        marks = body.get("marks")
        if not isinstance(marks, list) or not 1 <= len(marks) <= MAX_MARKS:
            raise ValueError(f"marks: a list of 1-{MAX_MARKS} marks")
        record: dict[str, Any] = {
            "id": "marks-" + uuid.uuid4().hex[:10], "at": time.time(),
            "device": str(body.get("device") or "").strip().lower()[:64],
            "request_id": str(body.get("request_id") or "")[:64],
            "note": str(body.get("note") or "")[:500],
            "marks": [_mark(m) for m in marks], "images": [], "read": False}
        if not record["device"]:
            raise ValueError("device is required")
        for image in (body.get("images") or [])[:4]:
            try:
                data = base64.b64decode(str(image.get("data") or ""), validate=True)
            except (ValueError, AttributeError) as exc:
                raise ValueError("images: base64 data") from exc
            media = str(image.get("type") or "image/jpeg")
            if media not in ("image/jpeg", "image/png") or not 0 < len(data) <= MAX_MARK_IMAGE:
                raise ValueError(f"images: JPEG or PNG up to {MAX_MARK_IMAGE} bytes")
            record["images"].append({"monitor": str(image.get("monitor") or "")[:64], "type": media, "data": data})
        with self._lock:
            cutoff = time.time() - MARK_RECORD_SECONDS
            self._marks = [m for m in self._marks if m["at"] > cutoff][-(MAX_MARK_RECORDS - 1):] + [record]
        request = record["request_id"]
        if request and self.get(request):
            record["read"] = True  # it answers an agent that is waiting for it
            self.add_event(request, {"type": "pointed", "data": {"marks_id": record["id"]}})
            self.remove(request)
        return self.marks_summary(record)

    @staticmethod
    def marks_summary(record: dict[str, Any]) -> dict[str, Any]:
        return {"id": record["id"], "device": record["device"], "request_id": record["request_id"],
                "at": record["at"], "note": record["note"], "marks": record["marks"],
                "images": [{"monitor": i["monitor"], "type": i["type"]} for i in record["images"]],
                "description": "\n".join(_mark_summary(n + 1, m) for n, m in enumerate(record["marks"]))}

    def marks(self, *, device: str | None = None, marks_id: str | None = None, unread: bool = False,
              take: bool = False) -> list[dict[str, Any]]:
        """Recent submissions, newest first; `take` marks the returned ones read."""
        with self._lock:
            found = [m for m in reversed(self._marks)
                     if (not device or m["device"] == device.lower()) and (not marks_id or m["id"] == marks_id)
                     and (not unread or not m["read"])]
            if take:
                for record in found:
                    record["read"] = True
            return [self.marks_summary(m) for m in found]

    def marks_image(self, marks_id: str, index: int) -> tuple[bytes, str] | None:
        with self._lock:
            record = next((m for m in self._marks if m["id"] == marks_id), None)
            if record is None or not 0 <= index < len(record["images"]):
                return None
            image = record["images"][index]
            return image["data"], image["type"]

    def events(self, after: int = 0, element_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events
                    if e["seq"] > after and (element_id is None or e["id"] == element_id)]

    def snapshot(self, device: str | None = None, *, main_node: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            self._prune()
            return [dict(e) for e in self._elements.values() if device is None or matches(e, device, main_node=main_node)]

    async def wait(self, after: int, timeout: float = 15.0) -> int:
        """Wait until the revision passes `after` (or the timeout, which also prunes expired items)."""
        if self._changed is None:
            self._loop, self._changed = asyncio.get_running_loop(), asyncio.Condition()
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: self.revision > after), timeout)
            except asyncio.TimeoutError:
                pass
        with self._lock:
            self._prune()
        return self.revision


store = CanvasStore()


# -- the PC renderer --------------------------------------------------------------

_overlay: subprocess.Popen | None = None


def overlay_command(workspace_root: Path) -> list[str] | None:
    """How to start the PC overlay on this machine, or None when it isn't installed."""
    script = workspace_root / "components" / "canvas-overlay" / "overlay.py"
    if sys.platform.startswith("linux"):
        # Wayland layer surfaces need layer-shell-qt built for the same Qt as PySide6: the system's
        python = Path("/usr/bin/python3")
        if not (os.environ.get("WAYLAND_DISPLAY") and Path("/usr/lib/libLayerShellQtInterface.so.6").exists()):
            return None
    else:
        python = workspace_root / ".venvs" / "desktop-tools" / "Scripts" / "pythonw.exe"
        if sys.platform != "win32":
            return None
    if not script.is_file() or not python.is_file():
        return None
    return [str(python), str(script)]


def start_overlay(workspace_root: Path, base_url: str, token: str, device: str = "pc", router_key: str = "",
                  main_node: bool = True) -> bool:
    """Start the canvas overlay for this device (once); it follows /api/canvas/stream. A device that
    isn't the main node follows the main node's canvas through Herald (router_key authenticates)."""
    global _overlay
    if _overlay and _overlay.poll() is None:
        return True
    command = overlay_command(workspace_root)
    if not command:
        return False
    env = {**os.environ, "ULTIMATE_ASSISTANT_URL": base_url.rstrip("/"),
           "ULTIMATE_ASSISTANT_APP_TOKEN": token, "ULTIMATE_ASSISTANT_CANVAS_DEVICE": device,
           "ULTIMATE_ASSISTANT_CANVAS_MAIN": "1" if main_node else "0",
           **({"ULTIMATE_ASSISTANT_ROUTER_KEY": router_key} if router_key else {})}
    _overlay = subprocess.Popen(command, env=env, close_fds=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True
