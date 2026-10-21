"""Transport-neutral geometry for task-anchored canvas drawings.

A drawing is a list of revisioned primitives (stroke, shape, arrow, label)
expressed in normalized coordinates (0.0-1.0) relative to an anchor.  The
anchor names the task, element, asset or screen the marks refer to.  Drawings
are pure projection data: they carry no domain-transition authority and no
executable markup.
"""
from __future__ import annotations

import math
import re

PRIMITIVE_TYPES = frozenset({"stroke", "shape", "arrow", "label"})
SHAPES = frozenset({"rect", "ellipse", "line"})
OPERATIONS = frozenset({"create", "update", "remove"})
ANCHOR_KINDS = frozenset({"task", "element", "asset", "screen"})
COORDINATE_SPACES = frozenset({"normalized"})
MAX_PRIMITIVES = 256
MAX_STROKE_POINTS = 512
_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_MARKUP = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>|(?:javascript|vbscript)\s*:", re.I)


def _id(value, name):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 128 or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty string of at most 128 bytes")
    return value


def _revision(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _coord(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be within the normalized range 0.0 to 1.0")
    return float(value)


def _point(value, name):
    if isinstance(value, dict):
        value = [value.get("x"), value.get("y")]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be an [x, y] pair")
    return [_coord(value[0], f"{name}.x"), _coord(value[1], f"{name}.y")]


def validate_anchor(anchor, name="anchor"):
    if not isinstance(anchor, dict):
        raise ValueError(f"{name} must be an object")
    kind = anchor.get("kind")
    if kind not in ANCHOR_KINDS:
        raise ValueError(f"{name}.kind must be one of {sorted(ANCHOR_KINDS)}")
    result = {"kind": kind, "ref": _id(anchor.get("ref"), f"{name}.ref")}
    if anchor.get("revision") is not None:
        result["revision"] = _revision(anchor["revision"], f"{name}.revision")
    return result


def validate_style(style, name="style"):
    if style is None:
        return None
    if not isinstance(style, dict):
        raise ValueError(f"{name} must be an object")
    result = {}
    if style.get("color") is not None:
        if not isinstance(style["color"], str) or not _COLOR.fullmatch(style["color"]):
            raise ValueError(f"{name}.color must be #RRGGBB or #RRGGBBAA")
        result["color"] = style["color"]
    if style.get("width") is not None:
        w = style["width"]
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or not 0 < w <= 64:
            raise ValueError(f"{name}.width must be within (0, 64]")
        result["width"] = float(w)
    if style.get("opacity") is not None:
        result["opacity"] = _coord(style["opacity"], f"{name}.opacity")
    if style.get("filled") is not None:
        if not isinstance(style["filled"], bool):
            raise ValueError(f"{name}.filled must be boolean")
        result["filled"] = style["filled"]
    return result


def validate_primitive(primitive, index=0):
    name = f"primitives[{index}]"
    if not isinstance(primitive, dict):
        raise ValueError(f"{name} must be an object")
    operation = primitive.get("operation", "create")
    if operation not in OPERATIONS:
        raise ValueError(f"{name}.operation must be one of {sorted(OPERATIONS)}")
    result = {"id": _id(primitive.get("id"), f"{name}.id"), "operation": operation,
              "revision": _revision(primitive.get("revision", 1), f"{name}.revision")}
    if operation == "remove":
        return result
    kind = primitive.get("type")
    if kind not in PRIMITIVE_TYPES:
        raise ValueError(f"{name}.type must be one of {sorted(PRIMITIVE_TYPES)}")
    result["type"] = kind
    if kind == "stroke":
        points = primitive.get("points")
        if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= MAX_STROKE_POINTS:
            raise ValueError(f"{name}.points must contain 2 to {MAX_STROKE_POINTS} points")
        result["points"] = [_point(p, f"{name}.points[{i}]") for i, p in enumerate(points)]
    elif kind == "shape":
        shape = primitive.get("shape")
        if shape not in SHAPES:
            raise ValueError(f"{name}.shape must be one of {sorted(SHAPES)}")
        result["shape"] = shape
        if shape == "line":
            result["from"] = _point(primitive.get("from"), f"{name}.from")
            result["to"] = _point(primitive.get("to"), f"{name}.to")
        else:
            x, y = _coord(primitive.get("x"), f"{name}.x"), _coord(primitive.get("y"), f"{name}.y")
            w, h = _coord(primitive.get("width"), f"{name}.width"), _coord(primitive.get("height"), f"{name}.height")
            if x + w > 1.0 + 1e-9 or y + h > 1.0 + 1e-9:
                raise ValueError(f"{name} extends outside the normalized canvas")
            result.update(x=x, y=y, width=w, height=h)
    elif kind == "arrow":
        result["from"] = _point(primitive.get("from"), f"{name}.from")
        result["to"] = _point(primitive.get("to"), f"{name}.to")
    else:
        text = primitive.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 1024 or "\x00" in text:
            raise ValueError(f"{name}.text must be a nonempty string of at most 1024 bytes")
        if _MARKUP.search(text):
            raise ValueError(f"{name}.text must not contain HTML or executable URLs")
        result["text"] = text
        result["at"] = _point(primitive.get("at"), f"{name}.at")
    style = validate_style(primitive.get("style"), f"{name}.style")
    if style:
        result["style"] = style
    return result


def validate_drawing(drawing, name="drawing"):
    """Validate and normalize a drawing; returns a new dict."""
    if not isinstance(drawing, dict):
        raise ValueError(f"{name} must be an object")
    space = drawing.get("coordinate_space", "normalized")
    if space not in COORDINATE_SPACES:
        raise ValueError(f"{name}.coordinate_space must be one of {sorted(COORDINATE_SPACES)}")
    primitives = drawing.get("primitives")
    if not isinstance(primitives, (list, tuple)) or not 1 <= len(primitives) <= MAX_PRIMITIVES:
        raise ValueError(f"{name}.primitives must contain 1 to {MAX_PRIMITIVES} items")
    normalized = [validate_primitive(p, i) for i, p in enumerate(primitives)]
    seen = set()
    for p in normalized:
        if p["id"] in seen:
            raise ValueError(f"{name}.primitives contains duplicate id {p['id']}")
        seen.add(p["id"])
    return {"coordinate_space": space, "anchor": validate_anchor(drawing.get("anchor"), f"{name}.anchor"),
            "primitives": normalized}


def apply_drawing(current, update):
    """Merge a validated drawing update into current primitives by revision.

    Returns the new primitive list; stale updates (revision older than the
    retained primitive) are ignored and removals delete the primitive.
    """
    state = {p["id"]: p for p in (current or [])}
    for p in update["primitives"]:
        old = state.get(p["id"])
        if old is not None and p["revision"] < old["revision"]:
            continue
        if p["operation"] == "remove":
            state.pop(p["id"], None)
        else:
            state[p["id"]] = p
    return list(state.values())
