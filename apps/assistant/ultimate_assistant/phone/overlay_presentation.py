"""Versioned, transport-neutral conversational presentation contract.

Presentations are projections of authoritative source, conversation, and learning
records.  They deliberately contain references rather than filesystem paths or
executable markup.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .canvas_geometry import validate_anchor, validate_drawing


SCHEMA = "assistant.presentation"
VERSION = 1
# "card" is a reference to an interactive canvas card on the main node; the phone fetches and
# renders it through its own device credential, so no markup travels in a presentation.
KINDS = frozenset({"text", "markdown", "math", "image", "artifact", "question", "flashcard", "feedback", "annotation", "reminder", "drawing", "card"})
ACTION_KINDS = frozenset({"next", "previous", "dismiss", "answer", "reveal", "retry", "open_artifact", "acknowledge", "snooze"})
ELEMENT_OPERATIONS = frozenset({"create", "update", "remove"})
INTERACTION_POLICIES = frozenset({"touch_through", "interactive", "dismissible", "none"})
_HTML = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_DANGEROUS = re.compile(r"(?:javascript\s*:|vbscript\s*:|data\s*:\s*(?:text/html|text/javascript|application/javascript))", re.I)


def _text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be a{' possibly empty' if empty else ' nonempty'} string")
    if len(value.encode("utf-8")) > maximum or "\x00" in value:
        raise ValueError(f"{name} exceeds its {maximum}-byte bound or contains NUL")
    return value


def _instant(value, name):
    if value is None:
        return None
    value = _text(value, name, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return value


def _safe_markup(value, name, maximum=32768):
    value = _text(value, name, maximum, empty=True)
    if _HTML.search(value) or _DANGEROUS.search(value):
        raise ValueError(f"{name} must not contain HTML or executable URLs")
    return value


def _validate_bounds(bounds, name="bounds"):
    if bounds is None:
        return None
    if not isinstance(bounds, dict):
        raise ValueError(f"{name} must be an object")
    # Bounds can be normalized (0.0 to 1.0) or anchored with x, y, width, height, or anchor: top_left, etc.
    res = {}
    if "anchor" in bounds:
        res["anchor"] = _text(bounds["anchor"], f"{name}.anchor", 64)
    for coord in ("x", "y", "width", "height", "left", "top", "right", "bottom"):
        if coord in bounds:
            val = bounds[coord]
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"{name}.{coord} must be a number")
            # If coordinates are normalized, negative dimensions are invalid
            if coord in ("width", "height") and val < 0:
                raise ValueError(f"{name}.{coord} must be non-negative")
            res[coord] = float(val) if isinstance(val, float) else val
    return res


def _validate_scope(scope, name="scope"):
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise ValueError(f"{name} must be an object")
    res = {}
    for key in ("device_id", "screen_id", "activity_id", "window_id", "package_name"):
        if scope.get(key) is not None:
            res[key] = _text(scope[key], f"{name}.{key}", 256)
    return res


def _validate_element(element, index):
    if not isinstance(element, dict):
        raise ValueError(f"elements[{index}] must be an object")
    element_id = _text(element.get("id"), f"elements[{index}].id", 128)
    operation = element.get("operation", "create")
    if operation not in ELEMENT_OPERATIONS:
        raise ValueError(f"elements[{index}] operation must be one of {sorted(ELEMENT_OPERATIONS)}")
    
    # Stable revision for element
    elem_rev = element.get("revision", 1)
    if isinstance(elem_rev, bool) or not isinstance(elem_rev, int) or elem_rev < 1:
        raise ValueError(f"elements[{index}] revision must be a positive integer")

    norm_elem = {
        "id": element_id,
        "operation": operation,
        "revision": elem_rev,
    }

    if operation == "remove":
        # Remove operation only strictly requires id, operation, and revision; can have reason
        if element.get("reason") is not None:
            norm_elem["reason"] = _text(element["reason"], f"elements[{index}].reason", 256)
        return norm_elem

    kind = element.get("kind")
    if kind not in KINDS:
        raise ValueError(f"elements[{index}] has an unsupported kind: {kind}")
    norm_elem["kind"] = kind

    # Z-order
    if "z_order" in element:
        z = element["z_order"]
        if isinstance(z, bool) or not isinstance(z, int):
            raise ValueError(f"elements[{index}].z_order must be an integer")
        norm_elem["z_order"] = z
    else:
        norm_elem["z_order"] = index

    # Bounds
    if "bounds" in element:
        norm_elem["bounds"] = _validate_bounds(element["bounds"], f"elements[{index}].bounds")

    # Scope
    if "scope" in element:
        norm_elem["scope"] = _validate_scope(element["scope"], f"elements[{index}].scope")

    # Expiry
    if "expires_at" in element:
        norm_elem["expires_at"] = _instant(element["expires_at"], f"elements[{index}].expires_at")

    # Task anchor (assistant-positioned imagery and drawings refer to a task/element/asset/screen)
    if element.get("anchor") is not None:
        norm_elem["anchor"] = validate_anchor(element["anchor"], f"elements[{index}].anchor")
    elif kind == "drawing" and isinstance(element.get("drawing"), dict) and element["drawing"].get("anchor") is not None:
        norm_elem["anchor"] = validate_anchor(element["drawing"]["anchor"], f"elements[{index}].drawing.anchor")

    # Interaction policy
    policy = element.get("interaction_policy", "touch_through")
    if policy not in INTERACTION_POLICIES:
        raise ValueError(f"elements[{index}].interaction_policy must be one of {sorted(INTERACTION_POLICIES)}")
    norm_elem["interaction_policy"] = policy
    draggable = element.get("draggable", False)
    if type(draggable) is not bool:
        raise ValueError("draggable must be a boolean")
    norm_elem["draggable"] = draggable

    # Content / payload according to kind
    if kind in {"text", "markdown", "math", "feedback", "annotation", "reminder"}:
        norm_elem["content"] = _safe_markup(element.get("content", ""), f"elements[{index}].content")
        if element.get("title") is not None:
            norm_elem["title"] = _safe_markup(element["title"], f"elements[{index}].title", 512)
    elif kind in {"image", "artifact"}:
        asset = element.get("asset")
        if not isinstance(asset, dict):
            raise ValueError(f"elements[{index}] asset reference is required")
        asset_kind = asset.get("kind")
        key = "source_id" if asset_kind == "source" else "artifact_id"
        if asset_kind not in {"source", "artifact"}:
            raise ValueError(f"elements[{index}] asset kind must be source or artifact")
        norm_elem["asset"] = {
            "kind": asset_kind,
            key: _text(asset.get(key), f"elements[{index}].asset.{key}", 1024),
            "revision": asset.get("revision"),
        }
        if isinstance(norm_elem["asset"]["revision"], bool) or not isinstance(norm_elem["asset"]["revision"], int) or norm_elem["asset"]["revision"] < 1:
            raise ValueError(f"elements[{index}] asset revision must be a positive integer")
        if asset.get("auth_revision") is not None:
            auth_revision = asset["auth_revision"]
            if isinstance(auth_revision, bool) or not isinstance(auth_revision, int) or auth_revision < 1:
                raise ValueError(f"elements[{index}] asset auth_revision must be a positive integer")
            norm_elem["asset"]["auth_revision"] = auth_revision
        if element.get("alt") is not None:
            norm_elem["alt"] = _safe_markup(element["alt"], f"elements[{index}].alt", 2048)
    elif kind == "drawing":
        norm_elem["drawing"] = validate_drawing(element.get("drawing"), f"elements[{index}].drawing")
        if element.get("alt") is not None:
            norm_elem["alt"] = _safe_markup(element["alt"], f"elements[{index}].alt", 2048)
    elif kind == "question":
        norm_elem["prompt"] = _safe_markup(element.get("prompt", ""), f"elements[{index}].prompt", 8192)
        choices = element.get("choices")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 20:
            raise ValueError(f"elements[{index}] question choices must contain 2 to 20 items")
        norm_elem["choices"] = [{"id": _text(c.get("id"), "choice.id", 128),
                                "text": _safe_markup(c.get("text", ""), "choice.text", 4096)}
                               for c in choices if isinstance(c, dict)]
        if len(norm_elem["choices"]) != len(choices):
            raise ValueError(f"elements[{index}] each choice must be an object")
    elif kind == "flashcard":
        norm_elem["front"] = _safe_markup(element.get("front", ""), f"elements[{index}].front", 8192)
        norm_elem["back"] = _safe_markup(element.get("back", ""), f"elements[{index}].back", 16384)
    elif kind == "card":
        card = element.get("card")
        if not isinstance(card, dict):
            raise ValueError(f"elements[{index}] card reference is required")
        card_revision = card.get("revision")
        if isinstance(card_revision, bool) or not isinstance(card_revision, int) or card_revision < 1:
            raise ValueError(f"elements[{index}] card revision must be a positive integer")
        norm_elem["card"] = {"id": _text(card.get("id"), f"elements[{index}].card.id", 128), "revision": card_revision}
        if element.get("title") is not None:
            norm_elem["title"] = _safe_markup(element["title"], f"elements[{index}].title", 512)

    # Actions on elements
    if "actions" in element:
        actions = element["actions"]
        if not isinstance(actions, list) or len(actions) > 16:
            raise ValueError(f"elements[{index}].actions must be a list of at most 16 items")
        norm_elem["actions"] = []
        for a_idx, action in enumerate(actions):
            if not isinstance(action, dict) or action.get("kind") not in ACTION_KINDS:
                raise ValueError(f"elements[{index}].actions[{a_idx}] unsupported action kind")
            norm_elem["actions"].append({
                "id": _text(action.get("id"), "action.id", 128),
                "kind": action["kind"],
                "label": _text(action.get("label"), "action.label", 256),
            })

    return norm_elem


def validate_presentation(value, *, supported_versions=(VERSION,)):
    """Validate and normalize a presentation, ignoring unknown extension fields."""
    if not isinstance(value, dict):
        raise ValueError("presentation must be an object")
    if value.get("schema") != SCHEMA:
        raise ValueError("unsupported presentation schema")
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in supported_versions:
        raise ValueError("unsupported presentation version")
    identity = _text(value.get("id"), "id", 256)
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("revision must be a positive integer")
    provenance = value.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("provenance must be an object")
    normalized_provenance = {}
    for key in ("source_id", "activity_id", "conversation_id", "correlation_id", "device_id", "screen_id"):
        if provenance.get(key) is not None:
            normalized_provenance[key] = _text(provenance[key], f"provenance.{key}", 1024)
    if not normalized_provenance.get("conversation_id") or not normalized_provenance.get("correlation_id"):
        raise ValueError("conversation_id and correlation_id provenance are required")
    normalized_provenance["revision"] = revision

    empty_canvas = bool(value.get("empty_canvas", False))

    blocks = value.get("blocks")
    elements = value.get("elements")

    # Either blocks, elements, or empty_canvas must be validly specified
    if empty_canvas:
        if blocks is not None and len(blocks) > 0:
            raise ValueError("empty_canvas presentation must not specify non-empty blocks")
        if elements is not None and any(e.get("operation") != "remove" for e in elements if isinstance(e, dict)):
            raise ValueError("empty_canvas presentation must not contain non-removal elements")
        normalized_blocks = []
        normalized_elements = [_validate_element(e, idx) for idx, e in enumerate(elements)] if elements else []
    else:
        if elements is not None:
            if not isinstance(elements, list) or len(elements) > 128:
                raise ValueError("elements must be a list of at most 128 items")
            if not elements and (blocks is None or not blocks):
                raise ValueError("presentation must contain elements or blocks, or set empty_canvas=True")
            normalized_elements = [_validate_element(e, idx) for idx, e in enumerate(elements)]
            # If blocks is also provided, validate it; otherwise synthesize blocks if empty
            if blocks is not None:
                if not isinstance(blocks, list) or len(blocks) > 128:
                    raise ValueError("blocks must contain at most 128 items")
                normalized_blocks = []
                for index, block in enumerate(blocks):
                    if not isinstance(block, dict) or block.get("kind") not in KINDS:
                        raise ValueError(f"blocks[{index}] has an unsupported kind")
                    kind = block["kind"]
                    item = {"kind": kind, "id": _text(block.get("id", f"block-{index + 1}"), "block.id", 128)}
                    if kind in {"text", "markdown", "math", "feedback", "annotation", "reminder"}:
                        item["content"] = _safe_markup(block.get("content", ""), "block.content")
                    elif kind in {"image", "artifact"}:
                        asset = block.get("asset")
                        if not isinstance(asset, dict):
                            raise ValueError("asset block requires an asset reference")
                        asset_kind = asset.get("kind")
                        key = "source_id" if asset_kind == "source" else "artifact_id"
                        if asset_kind not in {"source", "artifact"}:
                            raise ValueError("asset kind must be source or artifact")
                        item["asset"] = {"kind": asset_kind, key: _text(asset.get(key), key, 1024),
                                         "revision": asset.get("revision")}
                        if isinstance(item["asset"]["revision"], bool) or not isinstance(item["asset"]["revision"], int) or item["asset"]["revision"] < 1:
                            raise ValueError("asset revision must be a positive integer")
                        if block.get("alt") is not None:
                            item["alt"] = _safe_markup(block["alt"], "alt", 2048)
                    elif kind == "question":
                        item["prompt"] = _safe_markup(block.get("prompt", ""), "question.prompt", 8192)
                        choices = block.get("choices")
                        if not isinstance(choices, list) or not 2 <= len(choices) <= 20:
                            raise ValueError("question choices must contain 2 to 20 items")
                        item["choices"] = [{"id": _text(c.get("id"), "choice.id", 128),
                                            "text": _safe_markup(c.get("text", ""), "choice.text", 4096)}
                                           for c in choices if isinstance(c, dict)]
                        if len(item["choices"]) != len(choices):
                            raise ValueError("each choice must be an object")
                    elif kind == "flashcard":
                        item["front"] = _safe_markup(block.get("front", ""), "flashcard.front", 8192)
                        item["back"] = _safe_markup(block.get("back", ""), "flashcard.back", 16384)
                    normalized_blocks.append(item)
            else:
                # Synthesize blocks for backward compatibility with v1 consumers that read blocks
                normalized_blocks = []
                for elem in normalized_elements:
                    if elem.get("operation") != "remove":
                        b = {"kind": elem["kind"], "id": elem["id"]}
                        if "content" in elem:
                            b["content"] = elem["content"]
                        if "asset" in elem:
                            b["asset"] = elem["asset"]
                        if "alt" in elem:
                            b["alt"] = elem["alt"]
                        if "drawing" in elem:
                            b["drawing"] = elem["drawing"]
                        if "anchor" in elem:
                            b["anchor"] = elem["anchor"]
                        if "prompt" in elem:
                            b["prompt"] = elem["prompt"]
                            b["choices"] = elem.get("choices", [])
                        if "front" in elem:
                            b["front"] = elem["front"]
                            b["back"] = elem.get("back", "")
                        normalized_blocks.append(b)
        else:
            # Traditional blocks-only format
            if not isinstance(blocks, list) or not 1 <= len(blocks) <= 128:
                raise ValueError("blocks must contain 1 to 128 items")
            normalized_blocks = []
            for index, block in enumerate(blocks):
                if not isinstance(block, dict) or block.get("kind") not in KINDS:
                    raise ValueError(f"blocks[{index}] has an unsupported kind")
                kind = block["kind"]
                item = {"kind": kind, "id": _text(block.get("id", f"block-{index + 1}"), "block.id", 128)}
                if kind in {"text", "markdown", "math", "feedback", "annotation", "reminder"}:
                    item["content"] = _safe_markup(block.get("content", ""), "block.content")
                elif kind in {"image", "artifact"}:
                    asset = block.get("asset")
                    if not isinstance(asset, dict):
                        raise ValueError("asset block requires an asset reference")
                    asset_kind = asset.get("kind")
                    key = "source_id" if asset_kind == "source" else "artifact_id"
                    if asset_kind not in {"source", "artifact"}:
                        raise ValueError("asset kind must be source or artifact")
                    item["asset"] = {"kind": asset_kind, key: _text(asset.get(key), key, 1024),
                                     "revision": asset.get("revision")}
                    if isinstance(item["asset"]["revision"], bool) or not isinstance(item["asset"]["revision"], int) or item["asset"]["revision"] < 1:
                        raise ValueError("asset revision must be a positive integer")
                    if asset.get("auth_revision") is not None:
                        auth_revision = asset["auth_revision"]
                        if isinstance(auth_revision, bool) or not isinstance(auth_revision, int) or auth_revision < 1:
                            raise ValueError("asset auth_revision must be a positive integer")
                        item["asset"]["auth_revision"] = auth_revision
                    if block.get("alt") is not None:
                        item["alt"] = _safe_markup(block["alt"], "alt", 2048)
                elif kind == "question":
                    item["prompt"] = _safe_markup(block.get("prompt", ""), "question.prompt", 8192)
                    choices = block.get("choices")
                    if not isinstance(choices, list) or not 2 <= len(choices) <= 20:
                        raise ValueError("question choices must contain 2 to 20 items")
                    item["choices"] = [{"id": _text(c.get("id"), "choice.id", 128),
                                        "text": _safe_markup(c.get("text", ""), "choice.text", 4096)}
                                       for c in choices if isinstance(c, dict)]
                    if len(item["choices"]) != len(choices):
                        raise ValueError("each choice must be an object")
                elif kind == "flashcard":
                    item["front"] = _safe_markup(block.get("front", ""), "flashcard.front", 8192)
                    item["back"] = _safe_markup(block.get("back", ""), "flashcard.back", 16384)
                normalized_blocks.append(item)
            normalized_elements = []

    actions = value.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 32:
        raise ValueError("actions must be a list of at most 32 items")
    normalized_actions = []
    for action in actions:
        if not isinstance(action, dict) or action.get("kind") not in ACTION_KINDS:
            raise ValueError("unsupported presentation action")
        normalized_actions.append({"id": _text(action.get("id"), "action.id", 128),
                                   "kind": action["kind"],
                                   "label": _text(action.get("label"), "action.label", 256)})

    result = {
        "schema": SCHEMA,
        "version": version,
        "id": identity,
        "revision": revision,
        "title": _safe_markup(value.get("title", ""), "title", 1024),
        "empty_canvas": empty_canvas,
        "blocks": normalized_blocks,
        "elements": normalized_elements,
        "actions": normalized_actions,
        "expires_at": _instant(value.get("expires_at"), "expires_at"),
        "provenance": normalized_provenance,
        "authority": {"presentation_is_projection": True, "completion_inferred": False,
                      "mastery_inferred": False, "retention_inferred": False},
    }
    if "scope" in value:
        result["scope"] = _validate_scope(value["scope"], "scope")
    return result


def empty_canvas(identity, *, revision, conversation_id, correlation_id,
                 source_id=None, activity_id=None, elements=None):
    """Helper to produce an authoritative explicit empty-canvas presentation."""
    return validate_presentation({
        "schema": SCHEMA,
        "version": VERSION,
        "id": identity,
        "revision": revision,
        "empty_canvas": True,
        "elements": elements or [],
        "provenance": {
            "conversation_id": conversation_id,
            "correlation_id": correlation_id,
            "source_id": source_id,
            "activity_id": activity_id,
        },
    })


def serialize_presentation(value):
    return json.dumps(validate_presentation(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class PresentationStore:
    """Durable projection revisions; never a learning or completion authority."""

    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "presentations.sqlite"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS presentations(
                    id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    superseded INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(id,revision));
                CREATE INDEX IF NOT EXISTS current_presentation
                    ON presentations(id,superseded,revision);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, value):
        normalized = validate_presentation(value)
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload,superseded FROM presentations WHERE id=? AND revision=?",
                             (normalized["id"], normalized["revision"])).fetchone()
            if old:
                if old["payload"] != payload:
                    raise ValueError("presentation id and revision belong to a different payload")
                return {"state": "duplicate", "presentation": normalized,
                        "superseded": bool(old["superseded"])}
            latest = db.execute("SELECT MAX(revision) AS revision FROM presentations WHERE id=?",
                                (normalized["id"],)).fetchone()["revision"]
            if latest is not None and normalized["revision"] < latest:
                raise ValueError("presentation revision is older than the retained revision")
            db.execute("UPDATE presentations SET superseded=1 WHERE id=?",
                       (normalized["id"],))
            db.execute("INSERT INTO presentations(id,revision,payload,superseded) VALUES(?,?,?,0)",
                       (normalized["id"], normalized["revision"], payload))
        return {"state": "ready", "presentation": normalized,
                "supersedes_revision": latest}

    def current(self, identity):
        identity = _text(identity, "id", 256)
        with self._db() as db:
            row = db.execute("SELECT payload FROM presentations WHERE id=? AND superseded=0 ORDER BY revision DESC LIMIT 1",
                             (identity,)).fetchone()
        return None if row is None else json.loads(row["payload"])
