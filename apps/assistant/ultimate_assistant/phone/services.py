"""The phone services, built once per process and shared (the routes, the canvas bridge and the
phone tools all use the same stores). `service(name)` matches what the canvas bridge expects."""
from __future__ import annotations

import json
import threading
from typing import Any

from . import DEFAULT_DEVICE, conversation_id, phone_root

_lock = threading.RLock()
_services: dict[str, Any] = {}
# A build on top may supply its own: one value (provide), or a runtime that builds them in its own
# folder (use_runtime: its service(name) is asked first, for the names it lists).
_provided: dict[str, Any] = {}
_runtime: tuple[Any, frozenset[str]] | None = None
NAMES = frozenset({"pairing", "control", "phone_commands", "mobile_conversation", "voice", "assistant"})

INTERFACE = ("The user is talking to you through the assistant's phone app ({surface}, {platform}). The phone "
             "shows the conversation and the canvas and can be controlled: use the assistant_phone tools "
             "(phone_status first, then phone_command) to act on it, and read their results; a permission "
             "being granted doesn't mean an action worked.")


class Responder:
    """The phone's (and phone voice's) turn, through the same chat pipeline as the desktop: one
    memory and one set of conversations on every surface."""

    def respond(self, prompt: str, *, activity_id=None, request_id=None, progress=None, origin_device=None) -> str:
        from .unified_chat import chat
        origin = dict(origin_device or {})
        surface = str(origin.get("interface") or "phone app")[:40]
        platform = str(origin.get("platform") or "android")[:40]
        reply = chat(prompt, surface=str(origin.get("device_id") or DEFAULT_DEVICE)[:100],
                     interface=INTERFACE.format(surface=surface, platform=platform), progress=progress)
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("The assistant returned no answer.")
        return reply


def provide(name: str, value: Any) -> None:
    with _lock:
        _provided[name] = value
        _services.pop(name, None)


def use_runtime(runtime: Any, names=NAMES) -> None:
    global _runtime
    with _lock:
        _runtime = (runtime, frozenset(names))
        _services.clear()


def service(name: str) -> Any:
    with _lock:
        if name in _provided:
            return _provided[name]
        if _runtime is not None and name in _runtime[1]:
            return _runtime[0].service(name)
        if name in _services:
            return _services[name]
        root = phone_root()
        if name == "pairing":
            from .phone_pairing import PairingStore
            value = PairingStore(root / "pairing")
        elif name == "control":
            from .control_store import ControlStore
            value = ControlStore(root / "control")
        elif name == "phone_commands":
            from .phone_command_service import PhoneCommandService
            value = PhoneCommandService(service("control"), service("pairing"))
        elif name == "mobile_conversation":
            from .handoff_context import HandoffContextStore
            from .mobile_conversation import MobileConversation
            value = MobileConversation(service("assistant"), HandoffContextStore(root / "handoff"), root / "mobile")
            _services[name] = value
            value.voice = lambda: service("voice")
            return value
        elif name == "voice":
            from .voice_interaction import VoiceInteraction
            value = VoiceInteraction(service("mobile_conversation"), conversation_id=conversation_id(), device_id=DEFAULT_DEVICE)
        elif name == "assistant":
            value = Responder()
        else:
            raise KeyError(f"no phone service named {name}")
        _services[name] = value
        return value


class _Lazy:
    """Resolve a service on first use, not at import (routes are registered before anything runs)."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attribute: str) -> Any:
        return getattr(service(self.name), attribute)


def register_routes(app, *, canvas_event=None) -> None:
    """The phone app's routes (device-credential authenticated, never the app token)."""
    if any(getattr(route, "path", "").startswith("/api/mobile/v1/") for route in app.routes):
        return
    from .mobile_api import register
    register(app, pairing_store=_Lazy("pairing"), conversation=_Lazy("mobile_conversation"),
             commands=_Lazy("phone_commands"), canvas_event=canvas_event)


_bridge = None


def start_canvas_bridge(base_url: str, token: str):
    """Forward the assistant's canvas to the phone's conversation (main node only, once)."""
    global _bridge
    if _bridge is None:
        from .canvas_bridge import PhoneCanvasBridge
        _bridge = PhoneCanvasBridge(lambda: _ServiceGraph, base_url, token)
        _bridge.start()
    return _bridge


class _ServiceGraph:
    service = staticmethod(service)


def status() -> dict[str, Any]:
    pairing = service("pairing").status(DEFAULT_DEVICE)
    return {"device": DEFAULT_DEVICE, "pairing": pairing,
            "canvas": _bridge.status if _bridge is not None else {"following": False}}


def catalogue() -> dict[str, Any]:
    """What the phone app can do when the user granted it (the app reports grants in phone_status)."""
    return {"commands": {
        "device_status": "Fresh grants, accessibility connection, default-assistant role and installed build. Run first.",
        "launch_app": {"package_name": "installed Android package"},
        "open_url": {"url": "https URL"},
        "accessibility_action": {"action_type": "inspect_window, find_nodes, click_node, set_text, scroll, global_action, capture_screen",
                                 "click_node": {"view_id": "or text"}, "set_text": {"text": "required", "view_id": "optional"},
                                 "scroll": {"direction": "forward/backward"},
                                 "global_action": {"action": "back/home/recents/notifications/quick_settings"},
                                 "capture_screen": {"max_edge": "optional 320..2048; returns an image source_id"}},
        "list_file_roots": {}, "list_files": {"uri": "root/folder URI from an earlier result", "limit": "max 200"},
        "read_file": {"uri": "granted file URI", "max_bytes": "max 262144"},
        "upload_file": {"uri": "granted file URI; returns a source_id"},
        "privileged_status": "Whether Shizuku (optional, user-installed) is ready for privileged_shell/privileged_files.",
        "privileged_shell": {"command": "shell string or argv list", "timeout_seconds": "1..30"},
        "privileged_files": {"action": "list/read/write", "path": "absolute Android path"}},
        "notes": "Commands queue for the phone and run when the app is connected. Folder access is granted on the phone "
                 "(Settings > Connect a folder). Uploaded files and screenshots are read with read_phone_source."}


def describe() -> str:
    return json.dumps({"status": status(), **catalogue()}, ensure_ascii=False, default=str)

