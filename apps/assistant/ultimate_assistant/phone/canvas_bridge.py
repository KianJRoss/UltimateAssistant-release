"""The assistant's canvas on the phone: canvas elements -> the phone app's overlay presentation.

The base assistant keeps one canvas (ultimate_assistant/canvas.py) and streams
each device's elements. The phone companion app already renders presentations
(overlay_presentation.py, published through MobileConversation), so this
follows the canvas stream for the phone and republishes it as one presentation
("assistant-canvas") in the phone's conversation. Text, markdown and reminders
map directly, drawings map to the drawing primitives; images show their title
on the phone (phone images go through its own asset store). Interactive ("ui")
cards become "card" references: the phone app fetches the card and its renderer
with its device credential (mobile_api) and runs it in a WebView.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import httpx

LOG = logging.getLogger(__name__)
PRESENTATION_ID = "assistant-canvas"
PHONE_STALE_SECONDS = 20    # the phone's event stream sends a keepalive at least every 5s
ACTIVE: "PhoneCanvasBridge | None" = None   # the bridge this process runs, for delivery checks
_ANCHORS = {"top-left": "top_left", "top": "top", "top-right": "top_right", "left": "left",
            "center": "center", "right": "right", "bottom-left": "bottom_left", "bottom": "bottom",
            "bottom-right": "bottom_right"}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _primitive(shape: dict, index: int) -> dict:
    style = {k: shape[k] for k in ("color", "width") if k in shape}
    base = {"id": f"p{index}", "revision": 1, **({"style": style} if style else {})}
    kind = shape["type"]
    if kind == "stroke":
        return {**base, "type": "stroke", "points": shape["points"]}
    if kind in ("rect", "ellipse"):
        w, h = min(shape["w"], 1.0 - shape["x"]), min(shape["h"], 1.0 - shape["y"])
        return {**base, "type": "shape", "shape": "rect" if kind == "rect" else "ellipse",
                "x": shape["x"], "y": shape["y"], "width": w, "height": h}
    if kind in ("line", "arrow"):
        ends = {"from": [shape["x"], shape["y"]], "to": [shape["x2"], shape["y2"]]}
        return {**base, "type": "arrow", **ends} if kind == "arrow" else {**base, "type": "shape", "shape": "line", **ends}
    return {**base, "type": "label", "text": shape.get("text") or " ", "at": [shape["x"], shape["y"]]}


def to_presentation(elements: list[dict], *, conversation_id: str, revision: int) -> dict:
    out = []
    for index, element in enumerate(elements):
        # The phone applies an element only when its revision grows: use the element's update time,
        # so a thing shown again with the same id (new text, a live score) refreshes there too.
        revision_ms = max(1, int(element.get("updated_at", 0) * 1000))
        common = {"id": element["id"][:128], "revision": revision_ms, "z_order": index,
                  "interaction_policy": "touch_through", "draggable": element.get("draggable", False)}
        if element.get("expires_at"):
            common["expires_at"] = _iso(element["expires_at"])
        position = element.get("position", "top-right")
        common["bounds"] = ({"x": position["x"], "y": position["y"], **({"width": position["w"]} if "w" in position else {})}
                            if isinstance(position, dict) else {"anchor": _ANCHORS.get(position, "top_right")})
        kind = element["kind"]
        if kind == "point":
            continue  # pointing mode is the PC overlay's; the phone has no way to answer it yet
        if kind == "ui":
            if not isinstance(position, dict) or "w" not in position:
                common["bounds"] = {**common["bounds"], "width": 0.94}  # cards want most of a phone's width
            out.append({**common, "kind": "card", "interaction_policy": "interactive",
                        "card": {"id": element["id"][:128], "revision": revision_ms},
                        **({"title": element["title"]} if element.get("title") else {})})
        elif kind == "drawing":
            out.append({**common, "kind": "drawing", "drawing": {
                "coordinate_space": "normalized", "anchor": {"kind": "screen", "ref": "phone"},
                "primitives": [_primitive(s, i) for i, s in enumerate(element["shapes"])]}})
        else:
            body = element.get("body") or ""
            if kind == "image":
                kind, body = "text", body or "(image on your PC)"
            out.append({**common, "kind": kind, "content": body,
                        **({"title": element["title"]} if element.get("title") else {})})
    value = {"schema": "assistant.presentation", "version": 1, "id": PRESENTATION_ID, "revision": revision,
             "provenance": {"conversation_id": conversation_id, "correlation_id": f"canvas-{revision}",
                            "source_id": "assistant-canvas"}}
    return {**value, "elements": out} if out else {**value, "empty_canvas": True, "elements": []}


class PhoneCanvasBridge:
    def __init__(self, runtime_factory, base_url: str, token: str) -> None:
        # the runtime's service graph is built lazily; resolve it at the first publish
        self.runtime_factory, self.base_url, self.token = runtime_factory, base_url.rstrip("/"), token
        from . import conversation_id
        self.conversation_id = conversation_id()
        self._last: list | None = None
        # what reached the phone's conversation, so tools can say "sent" rather than guess
        self.status = {"following": False, "error": None, "published_at": None,
                       "event_revision": None, "element_ids": []}

    def start(self) -> None:
        global ACTIVE
        ACTIVE = self
        _register_delivery_route()
        threading.Thread(target=self._follow, name="phone-canvas", daemon=True).start()

    def _follow(self) -> None:
        headers = {"x-ultimate-assistant-token": self.token, "Accept": "text/event-stream"}
        while True:
            try:
                with httpx.stream("GET", f"{self.base_url}/api/canvas/stream",
                                  params={"device": "phone", "main": "false"}, headers=headers,
                                  timeout=httpx.Timeout(10, read=60)) as response:
                    response.raise_for_status()
                    self.status.update(following=True, error=None)
                    event = None
                    for line in response.iter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event == "canvas":
                            self.publish(json.loads(line[5:])["elements"])
            except Exception as exc:  # noqa: BLE001 - runtime restart or network blip: retry
                if self.status["following"] or not self.status["error"]:
                    LOG.warning("Phone canvas bridge lost the canvas stream: %s", exc)
                self.status.update(following=False, error=str(exc)[:300])
                time.sleep(5)

    def publish(self, elements: list[dict]) -> None:
        if elements == self._last:
            return  # unchanged snapshot
        presentation = to_presentation(elements, conversation_id=self.conversation_id,
                                       revision=int(time.time() * 1000))
        try:
            result = self.runtime_factory().service("mobile_conversation").publish_presentation(
                presentation, conversation_id=self.conversation_id)
            self._last = elements
            self.status.update(published_at=time.time(), event_revision=result.get("event_revision"),
                               element_ids=[e["id"] for e in elements], error=None)
        except Exception as exc:  # noqa: BLE001 - a bad element must not stop the bridge
            LOG.exception("Could not publish the canvas to the phone")
            self.status["error"] = f"publish failed: {exc}"[:300]


def delivery(element_id: str) -> dict:
    """Whether a canvas element reached the phone app: sent | queued | pending | not_forwarded.

    "sent" means the phone app's event stream received it; the app doesn't confirm drawing it."""
    from .mobile_api import STREAMS
    bridge = ACTIVE
    if bridge is None:
        return {"state": "not_forwarded", "detail": "the phone canvas bridge isn't running on the main node"}
    status = bridge.status
    if element_id not in status["element_ids"]:
        if not status["following"]:
            return {"state": "not_forwarded", "detail": f"the phone canvas bridge can't read the canvas ({status['error']})"}
        return {"state": "pending", "detail": status["error"] or "not forwarded to the phone yet"}
    phone = STREAMS.get(bridge.conversation_id)
    if phone and phone["revision"] >= (status["event_revision"] or 0):
        return {"state": "sent", "detail": "the phone app received it"}
    if not phone or time.time() - phone["at"] > PHONE_STALE_SECONDS:
        seen = f"last connected {int(time.time() - phone['at'])}s ago" if phone else "not connected since the server started"
        return {"state": "queued", "detail": f"the phone app isn't connected ({seen}); it gets it when the app reconnects, if it hasn't expired"}
    return {"state": "queued", "detail": "on its way to the phone app"}


def _register_delivery_route() -> None:
    from .. import web_ui as shared
    if any(getattr(r, "path", "") == "/api/canvas/phone-delivery" for r in shared.app.routes):
        return
    from fastapi import Depends
    shared.app.add_api_route("/api/canvas/phone-delivery", lambda id: delivery(id), methods=["GET"],
                             dependencies=[Depends(shared._require_app_token)])
