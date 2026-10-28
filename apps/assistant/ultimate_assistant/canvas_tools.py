"""Canvas tools for every agent: show, update and clear things over the user's screens.

Calls the main node's canvas (canvas.py) through Herald, from whichever device
runs it. The long reference lives in canvas_guide.md and is read on demand with
canvas_guide, so every agent's tool list stays short. See ARCHITECTURE.md (The canvas).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from urllib.parse import quote
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("ultimate-assistant-canvas")
GUIDE = Path(__file__).with_name("canvas_guide.md")
MAX_ASK_WAIT = 600


def _call(method: str, path: str, **kwargs) -> httpx.Response:
    from .settings import settings
    base = (os.environ.get("HERALD_URL") or settings.herald_url).rstrip("/")
    key = os.environ.get("HERALD_API_KEY")
    kwargs.setdefault("timeout", 30)
    return httpx.request(method, f"{base}/nodes/main/http/api/canvas{path}",
                         headers={"Authorization": f"Bearer {key}"} if key else {}, **kwargs)


@mcp.tool()
def canvas_show(kind: str, title: str = "", body: str = "", devices: list[str] | None = None,
                position: str | dict = "top-right", monitor: str | int = "active", ttl_seconds: int = 120,
                id: str = "", image: str = "", shapes: list[dict] | None = None, color: str = "",
                template: str = "", data: dict | None = None, ui: str = "", bare: bool = False, draggable: bool = False,
                resizable: bool = False, monitor_locked: bool = False) -> str:
    """Put something on the canvas: your whiteboard and pinboard over the user's screens. Draw on it,
    pin photos, videos, notes and cards, or show small working tools, without taking their focus.

    kind: text | markdown | reminder | image | drawing | ui. Reuse an id to update a thing in place.
    draggable: allow the user to move this item by its top edge (phone and PC). Enable for floating
    references/notes when useful; keep false for annotations aligned to app content. Content updates
    preserve the user placement; changing position resets it. Use finite TTLs for demonstrations.
    resizable: allow the user to resize it from its bottom-right corner (PC), e.g. videos, images and
    long notes. A resized item keeps its size across content updates; changing position resets it.
    monitor_locked: a draggable item stays on its monitor (by default it can be dragged to another).
    To pin something in place, leave draggable false and use ttl_seconds=0 so it stays until cleared.
    ttl_seconds: how long it stays (0 = until cleared). position: top-left ... bottom-right, center, or
    {"x", "y", "w"} in 0-1 of the monitor; monitor: "active", "all", "assistant" or a number;
    devices: ["pc"], ["phone"], a device name or ["everywhere"]. image: https URL or a file on the main PC.
    drawing: shapes like {"type": "arrow", "x": .2, "y": .3, "x2": .5, "y2": .5} (also rect, ellipse,
    line, label, stroke) in 0-1 of the monitor.
    ui: interactive cards. Easiest: template = choice | checklist | timer | stats | message | chart |
    video | gallery with its data, e.g. template="timer", data={"seconds": 300, "label": "Tea"}. For anything
    else write markup in `ui` (Markdown + components, the language ChatGPT uses for its live widgets).
    Read canvas_guide("templates") or canvas_guide("markup") before your first card of a kind, and
    canvas_events for what the user did. Keep things small; clear them when done."""
    element = {"kind": kind, "devices": devices or ["pc"], "position": position, "monitor": monitor,
               "ttl_seconds": ttl_seconds, "draggable": draggable,
               "resizable": resizable, "monitor_locked": monitor_locked, "agent": os.environ.get("HERALD_GATEWAY_PART", "")[-40:]}
    for key, value in (("id", id), ("title", title), ("body", body), ("image", image), ("color", color),
                       ("template", template), ("ui", ui), ("data", data), ("bare", bare)):
        if value:
            element[key] = value
    if shapes:
        element["shapes"] = shapes
    response = _call("POST", "", json=element)
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    shown = response.json()["shown"]
    what = shown.get("template") or shown["kind"]
    text = f"Published {what} '{shown['id']}' on {', '.join(shown['devices'])}" + (
        f" for {shown['ttl_seconds']}s." if shown["ttl_seconds"] else " until cleared.")
    if {"phone", "everywhere"} & set(shown["devices"]):
        text += " " + _phone_delivery(shown["id"])
    return text


def _phone_delivery(element_id: str, wait: float = 6) -> str:
    """What the phone app got: published on the canvas is not the same as on the user's phone."""
    deadline = time.monotonic() + wait
    while True:
        try:
            response = _call("GET", "/phone-delivery", params={"id": element_id}, timeout=10)
        except httpx.HTTPError:
            response = None
        if response is None or response.status_code >= 400:
            return "Phone: delivery can't be checked from here; don't tell the user it's showing until they say so."
        status = response.json()
        if status["state"] in ("sent", "not_forwarded") or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if status["state"] == "sent":
        return "Phone: the app received it (the app doesn't confirm drawing it; if the user doesn't see it, believe them)."
    return f"Phone: NOT delivered yet: {status['detail']}. Tell the user that, don't say it's showing."


@mcp.tool()
def canvas_guide(topic: str = "") -> str:
    """How to use the canvas, one part at a time. No topic: the overview and the list of topics.
    Topics: kinds, drawing, templates, markup, components, talking back, example, keep in mind, feeds."""
    text = GUIDE.read_text(encoding="utf-8")
    sections = re.split(r"(?m)^## ", text)
    if not topic.strip():
        names = [s.split("\n", 1)[0].strip() for s in sections[1:]]
        return sections[0].strip() + "\n\nTopics: " + ", ".join(n.lower() for n in names)
    wanted = topic.strip().lower()
    for section in sections[1:]:
        if section.split("\n", 1)[0].strip().lower().startswith(wanted):
            return "## " + section.strip()
    return f"No topic '{topic}'. " + canvas_guide("")


@mcp.tool()
def canvas_ask(question: str, options: list[str | dict], multi: bool = False, header: str = "",
               allow_other: bool = True, wait_seconds: int = 120, position: str | dict = "top-right",
               monitor: str | int = "active", devices: list[str] | None = None) -> str:
    """Ask the user a quick question on the canvas and wait for their tap: a small choice card over
    their screen instead of interrupting with chat or voice. options: strings or {"label",
    "description"}; multi lets them pick several; allow_other adds a "something else" box.
    Returns their choices, or, if they haven't answered within wait_seconds (up to 600), the card's id:
    it stays up and canvas_events(id) gets the answer later."""
    card_id = "ask-" + uuid.uuid4().hex[:10]
    shown = canvas_show(kind="ui", template="choice", id=card_id, title=question[:200], ttl_seconds=0,
                        position=position, monitor=monitor, devices=devices,
                        data={"question": question, "options": options, "multi": multi,
                              "header": header, "allow_other": allow_other})
    if shown.startswith("Error"):
        return shown
    deadline = time.monotonic() + max(5, min(wait_seconds, MAX_ASK_WAIT))
    while (remaining := deadline - time.monotonic()) > 0:
        response = _call("GET", "/events", params={"id": card_id, "after": 0, "wait": min(remaining, 110)},
                         timeout=min(remaining, 110) + 30)
        if response.status_code >= 400:
            return "Error: " + response.text[:400]
        for event in response.json()["events"]:
            if event["type"] == "answer" or (event["type"] == "action" and event.get("name") == "answer"):
                _call("DELETE", "", params={"id": card_id})
                return json.dumps(event.get("data") or {}, ensure_ascii=False)
            if event["type"] == "close":
                return "The user closed the question without answering."
    return f"No answer yet; the question is still on the canvas as '{card_id}'. canvas_events(id='{card_id}') gets it later."


def _marks_result(record: dict) -> list:
    """The marks in words and numbers, then each screenshot with the marks drawn in."""
    out: list = [json.dumps({k: record[k] for k in ("id", "device", "note", "description", "marks")},
                            ensure_ascii=False)]
    for index, image in enumerate(record.get("images", [])):
        response = _call("GET", f"/marks/{quote(record['id'], safe='')}/images/{index}", timeout=60)
        if response.status_code < 400:
            out.append(Image(data=response.content, format="png" if image["type"] == "image/png" else "jpeg"))
    return out


@mcp.tool(structured_output=False)
def canvas_point(prompt: str, devices: list[str] | None = None, tools: list[str] | None = None,
                 wait_seconds: int = 120, monitor: str | int = "active"):
    """Ask the user to point something out on their screen, and wait: their screen enters pointing
    mode with your prompt, they mark what they mean and press Enter. Returns the marks (positions
    in 0-1 of their monitor, the window under each) and a screenshot with the marks drawn in.
    tools: any of point (a click), box (a drag), draw (freehand), window (a whole window); default all.
    Use it when "which one?" is easier shown than said. devices: as canvas_show (default the PC)."""
    request_id = "point-" + uuid.uuid4().hex[:10]
    element = {"kind": "point", "id": request_id, "title": prompt[:200], "devices": devices or ["pc"],
               "monitor": monitor, "ttl_seconds": max(30, min(wait_seconds, MAX_ASK_WAIT)) + 30,
               "agent": os.environ.get("HERALD_GATEWAY_PART", "")[-40:]}
    if tools:
        element["tools"] = tools
    response = _call("POST", "", json=element)
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    deadline = time.monotonic() + max(5, min(wait_seconds, MAX_ASK_WAIT))
    while (remaining := deadline - time.monotonic()) > 0:
        response = _call("GET", "/events", params={"id": request_id, "after": 0, "wait": min(remaining, 110)},
                         timeout=min(remaining, 110) + 30)
        if response.status_code >= 400:
            return "Error: " + response.text[:400]
        for event in response.json()["events"]:
            if event["type"] == "pointed":
                found = _call("GET", "/marks", params={"id": (event.get("data") or {}).get("marks_id", "")})
                records = found.json().get("marks", []) if found.status_code < 400 else []
                return _marks_result(records[0]) if records else "The user pointed, but the marks were not found."
            if event["type"] == "close":
                return "The user left pointing mode without marking anything."
    _call("DELETE", "", params={"id": request_id})
    return "The user did not point at anything in time; pointing mode was closed."


@mcp.tool(structured_output=False)
def canvas_marks(id: str = "", device: str = ""):
    """What the user pointed at on their own (pointing mode's hotkey, or while talking): the latest
    marks, or one set by id (a turn's context names it), with the screenshot they marked."""
    params = {k: v for k, v in (("id", id), ("device", device)) if v}
    response = _call("GET", "/marks", params=params)
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    records = response.json().get("marks", [])
    return _marks_result(records[0]) if records else "The user hasn't pointed at anything recently."


@mcp.tool()
def canvas_clear(id: str = "", device: str = "") -> str:
    """Remove one element by id, or everything on a device (device="pc"), or everything (no arguments)."""
    params = {k: v for k, v in (("id", id), ("device", device)) if v}
    response = _call("DELETE", "", params=params)
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    return f"Removed {response.json()['removed']} element(s)."


@mcp.tool()
def canvas_list(device: str = "") -> str:
    """What the canvas is showing now (optionally for one device), with each interactive card's state."""
    response = _call("GET", "", params={"device": device} if device else {})
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    elements = response.json()["elements"]
    return json.dumps([{k: e.get(k) for k in ("id", "kind", "template", "title", "devices", "monitor",
                                               "ttl_seconds", "draggable", "resizable", "monitor_locked", "state")
                        if e.get(k) is not None and (k != "state" or e.get("kind") == "ui")}
                       for e in elements], ensure_ascii=False) if elements else "The canvas is empty."


@mcp.tool()
def canvas_events(id: str = "", after: int = 0, wait_seconds: int = 0) -> str:
    """What the user did on interactive cards: messages (UA.send), actions (UA.action, and template
    events like a choice's answer or a timer's done), closes, link opens and render errors, each with
    the card's saved state. Pass the last seen `seq` as `after` to get only new ones; wait_seconds
    (up to 120) waits for the next one."""
    params = {"after": after, "wait": max(0, min(wait_seconds, 120))}
    if id:
        params["id"] = id
    response = _call("GET", "/events", params=params, timeout=params["wait"] + 30)
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    events = response.json()["events"]
    return json.dumps(events, ensure_ascii=False) if events else "No new card events."


@mcp.tool()
def canvas_feed_save(spec: dict) -> str:
    """Save a live data feed and its canvas card. Read canvas_guide('feeds') for sources,
    field mapping, refresh intervals and conditions that can wake an assistant."""
    response = _call("POST", "/feeds", json=spec)
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    return json.dumps(response.json(), ensure_ascii=False)


@mcp.tool()
def canvas_feed_list() -> str:
    """List saved canvas feeds, their current state and recent refresh errors."""
    response = _call("GET", "/feeds")
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    return json.dumps(response.json(), ensure_ascii=False)


@mcp.tool()
def canvas_feed_remove(id: str) -> str:
    """Remove a saved canvas feed so it stops refreshing."""
    response = _call("DELETE", "/feeds/" + quote(id, safe=""))
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    return json.dumps(response.json(), ensure_ascii=False)


@mcp.tool()
def canvas_feed_refresh(id: str) -> str:
    """Refresh one saved canvas feed now and return its observed result."""
    response = _call("POST", "/feeds/" + quote(id, safe="") + "/refresh", timeout=90)
    if response.status_code >= 400:
        return "Error: " + response.text[:400]
    return json.dumps(response.json(), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
