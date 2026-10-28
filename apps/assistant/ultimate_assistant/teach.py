"""Teach mode on a Linux desktop: the user shows the assistant a task once, and it learns it.

`ultimate-assistant teach` starts recording (the overlay shows "Recording" with Stop and Discard);
the user does the task as usual; `ultimate-assistant teach` again (or Stop) sends the recording to
the main node (teach_store.py), where an agent turns it into a skill with parameters. What is
recorded, per step:

- the window in use (Hyprland's event socket: class and title),
- the controls used, by name and role, through AT-SPI accessibility events (focus, presses,
  toggles, text typed into a field; password fields are recorded as hidden, never their text),
- a screenshot once the screen settles after a step (grim; the overlay steps aside first).

No raw key logging and no coordinates: the point is the procedure, not a replay. Recording is
bounded (MAX_SECONDS, MAX_STEPS, MAX_SHOTS) and stays on this device until it is sent; a
recording that couldn't be sent waits in <data dir>/teach-pending.json for the next try.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)
MAX_SECONDS = 15 * 60
MAX_STEPS = 300
MAX_SHOTS = 40
SETTLE = 0.8  # seconds without new steps before the screenshot of the latest one
EVENTS = ("object:state-changed:focused", "object:state-changed:checked", "object:state-changed:pressed",
          "object:text-changed:insert")


class Steps:
    """What the user did, as steps: the pure part of the recorder (no desktop needed)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.items: list[dict[str, Any]] = []
        self.want_shot: int | None = None  # the step waiting for its screenshot
        self.changed_at = 0.0

    def _add(self, step: dict[str, Any], *, shot: bool) -> None:
        if len(self.items) >= MAX_STEPS:
            return
        step["at"] = self.clock()
        self.items.append(step)
        self.changed_at = step["at"]
        if shot:
            self.want_shot = len(self.items) - 1

    def window(self, app: str, title: str) -> None:
        last = next((s for s in reversed(self.items) if s["kind"] == "window"), None)
        if not app or (last and last.get("app") == app and last.get("title") == title):
            return
        if self.items and self.items[-1]["kind"] == "window" and self.items[-1].get("app") == app:
            # Hyprland reports every title change (a terminal's, a page loading) as a window event:
            # the same app with nothing done in between is still the same step, now titled anew.
            self.items[-1]["title"] = title
            return
        self._add({"kind": "window", "app": app, "title": title}, shot=True)

    def _control(self, kind: str, element: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"kind": kind, **{k: element[k] for k in ("name", "role", "app") if element.get(k)}, **extra}

    def focus(self, element: dict[str, Any]) -> None:
        last = self.items[-1] if self.items else None
        if last and last["kind"] in ("focus", "text", "activate") and last.get("_node") == element.get("node"):
            return  # still on the same control
        self._add({**self._control("focus", element), "_node": element.get("node")}, shot=True)

    def activate(self, element: dict[str, Any]) -> None:
        self._add({**self._control("activate", element), "_node": element.get("node")}, shot=True)

    def toggle(self, element: dict[str, Any], on: bool) -> None:
        self._add({**self._control("toggle", element, text="on" if on else "off"), "_node": element.get("node")},
                  shot=True)

    def text(self, element: dict[str, Any], inserted: str) -> None:
        secret = (element.get("role") or "").lower() == "password text"
        last = self.items[-1] if self.items else None
        if last and last["kind"] == "text" and last.get("_node") == element.get("node"):
            if not secret:
                last["text"] = (last.get("text", "") + inserted)[:2000]
            self.changed_at = self.clock()
            self.want_shot = len(self.items) - 1
            return
        step = self._control("text", element, **({"secret": True} if secret else {"text": inserted[:2000]}))
        self._add({**step, "_node": element.get("node")}, shot=True)

    def note(self, text: str) -> None:
        if text.strip():
            self._add({"kind": "note", "text": text.strip()[:2000]}, shot=False)

    def ready_for_shot(self) -> int | None:
        """The step to photograph now (the screen has settled since it), or None."""
        if self.want_shot is not None and self.clock() - self.changed_at >= SETTLE:
            index, self.want_shot = self.want_shot, None
            return index
        return None

    def public(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.items]


# -- the desktop: Hyprland and AT-SPI ---------------------------------------------------------

def _hypr_socket(name: str) -> Path | None:
    root = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "hypr"
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    candidate = root / signature / name
    if signature and candidate.exists():
        return candidate
    live = list(root.glob(f"*/{name}")) if root.is_dir() else []
    return max(live, key=lambda p: p.stat().st_mtime, default=None)


def watch_windows(on_window: Callable[[str, str], None], stopped: threading.Event) -> None:
    """Hyprland's event socket: every focus change of a window (class, title)."""
    path = _hypr_socket(".socket2.sock")
    if path is None:
        LOG.info("Teach mode: no Hyprland event socket; windows aren't recorded")
        return
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(path))
        connection.settimeout(1)
        buffer = b""
        while not stopped.is_set():
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                event, _, data = line.decode(errors="replace").partition(">>")
                if event == "activewindow" and "," in data:
                    app, title = data.split(",", 1)
                    on_window(app, title)


class Accessibility:
    """Listens to AT-SPI on the accessibility bus with jeepney (pure-Python D-Bus)."""

    def __init__(self) -> None:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        self._address, self._call_new = DBusAddress, new_method_call
        session = open_dbus_connection(bus="SESSION")
        try:
            status = DBusAddress("/org/a11y/bus", bus_name="org.a11y.Bus", interface="org.freedesktop.DBus.Properties")
            if not session.send_and_get_reply(new_method_call(status, "Get", "ss", ("org.a11y.Status", "IsEnabled"))).body[0][1]:
                session.send_and_get_reply(new_method_call(status, "Set", "ssv", ("org.a11y.Status", "IsEnabled", ("b", True))))
            bus = DBusAddress("/org/a11y/bus", bus_name="org.a11y.Bus", interface="org.a11y.Bus")
            address = session.send_and_get_reply(new_method_call(bus, "GetAddress")).body[0]
        finally:
            session.close()
        self.connection = open_dbus_connection(bus=address)
        self._pids: dict[str, int] = {}

    def call(self, name: str, path: str, interface: str, method: str, signature: str = "", body: tuple = ()):
        reply = self.connection.send_and_get_reply(
            self._call_new(self._address(path, bus_name=name, interface=interface), method, signature, body), timeout=3)
        if reply.header.message_type.name == "error":
            raise RuntimeError(f"{interface}.{method} failed")
        return reply.body

    def register(self) -> None:
        registry = ("org.a11y.atspi.Registry", "/org/a11y/atspi/registry", "org.a11y.atspi.Registry")
        for event in EVENTS:
            try:  # newer AT-SPI also takes the properties to send with each event and an app filter
                self.call(*registry, "RegisterEvent", "sass", (event, [], ""))
            except RuntimeError:
                self.call(*registry, "RegisterEvent", "s", (event,))
        from jeepney import MatchRule
        from jeepney.bus_messages import message_bus
        self.connection.send_and_get_reply(message_bus.AddMatch(
            MatchRule(type="signal", interface="org.a11y.atspi.Event.Object")))

    def deregister(self) -> None:
        for event in EVENTS:
            try:
                self.call("org.a11y.atspi.Registry", "/org/a11y/atspi/registry", "org.a11y.atspi.Registry",
                          "DeregisterEvent", "s", (event,))
            except Exception:  # noqa: BLE001 - the bus may be gone already
                pass
        self.connection.close()

    def ours(self, sender: str) -> bool:
        """Events from this assistant's own windows (the overlay) aren't the user's task."""
        if sender not in self._pids:
            try:
                bus = self._address("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                                    interface="org.freedesktop.DBus")
                pid = self.connection.send_and_get_reply(self._call_new(bus, "GetConnectionUnixProcessID", "s",
                                                                        (sender,)), timeout=3).body[0]
            except Exception:  # noqa: BLE001
                pid = -1
            self._pids[sender] = pid
        pid = self._pids[sender]
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        return pid == os.getpid() or b"canvas-overlay" in command

    def describe(self, sender: str, path: str) -> dict[str, Any]:
        accessible = "org.a11y.atspi.Accessible"
        element: dict[str, Any] = {"node": f"{sender}{path}"}
        try:
            element["name"] = self.call(sender, path, "org.freedesktop.DBus.Properties", "Get", "ss",
                                        (accessible, "Name"))[0][1]
            element["role"] = self.call(sender, path, accessible, "GetRoleName")[0]
            app = self.call(sender, path, accessible, "GetApplication")[0]
            element["app"] = self.call(app[0], app[1], "org.freedesktop.DBus.Properties", "Get", "ss",
                                       (accessible, "Name"))[0][1]
        except Exception:  # noqa: BLE001 - a control that went away; keep what we have
            pass
        return element

    def watch(self, steps: Steps, lock: threading.Lock, stopped: threading.Event) -> None:
        from jeepney import HeaderFields
        while not stopped.is_set():
            try:
                message = self.connection.receive(timeout=1)
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 - the bus closed
                return
            fields = message.header.fields
            if fields.get(HeaderFields.interface) != "org.a11y.atspi.Event.Object":
                continue
            member, sender, path = fields.get(HeaderFields.member), fields.get(HeaderFields.sender), fields.get(HeaderFields.path)
            detail, detail1 = message.body[0], message.body[1]
            if not sender or self.ours(sender):
                continue
            if member == "StateChanged" and detail in ("focused", "pressed", "checked") and (detail1 or detail == "checked"):
                element = self.describe(sender, path)
                with lock:
                    if detail == "focused":
                        steps.focus(element)
                    elif detail == "pressed":
                        steps.activate(element)
                    else:
                        steps.toggle(element, bool(detail1))
            elif member == "TextChanged" and detail == "insert":
                data = message.body[3]
                inserted = data[1] if isinstance(data, tuple) and len(data) == 2 else str(data)
                if isinstance(inserted, str) and inserted:
                    element = self.describe(sender, path)
                    with lock:
                        steps.text(element, inserted)


def screenshot(scale: float = 0.7) -> tuple[bytes, str] | None:
    """The focused monitor as JPEG, with the overlay stepped aside (its capture socket)."""
    canvas = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "ultimate-assistant-canvas.sock"
    aside = None
    try:
        aside = socket.socket(socket.AF_UNIX)
        aside.settimeout(2)
        aside.connect(str(canvas))
        aside.sendall(b"hide\n")
        aside.recv(16)
    except OSError:
        aside = None
    try:
        output = []
        monitors = subprocess.run(["hyprctl", "-j", "monitors"], capture_output=True, text=True, timeout=5)
        focused = next((m["name"] for m in json.loads(monitors.stdout or "[]") if m.get("focused")), None)
        if focused:
            output = ["-o", focused]
        done = subprocess.run(["grim", "-t", "jpeg", "-q", "70", "-s", str(scale), *output, "-"],
                              capture_output=True, timeout=10)
        return (done.stdout, "image/jpeg") if done.returncode == 0 and done.stdout else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        if aside is not None:
            try:
                aside.sendall(b"show\n")
                aside.recv(16)
            except OSError:
                pass
            aside.close()


class Recorder:
    """One recording at a time on this device."""

    def __init__(self, *, overlay: Callable[[str], Any] = lambda command: None,
                 notify: Callable[[str], None] = lambda text: None,
                 send: Callable[[dict], None] | None = None,
                 shoot: Callable[[], tuple[bytes, str] | None] = screenshot,
                 pending: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.overlay, self.notify, self.shoot, self.clock = overlay, notify, shoot, clock
        self.send = send or _send_to_main
        self.pending = pending
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self.steps: Steps | None = None
        self.images: list[tuple[bytes, str]] = []
        self.started_at = 0.0
        self.note = ""
        self._accessibility: Accessibility | None = None

    @property
    def recording(self) -> bool:
        return self.steps is not None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"recording": self.recording, "steps": len(self.steps.items) if self.steps else 0,
                    "screenshots": len(self.images)}

    def command(self, text: str = "") -> dict[str, Any]:
        """`teach` toggles; `teach stop` / `teach discard` / `teach start [note]`; other text is a note."""
        word, _, rest = text.strip().partition(" ")
        word = word.lower()
        if word == "discard":
            self.discard()
        elif word == "stop" or (self.recording and word not in ("start",)):
            self.stop(note=rest if word == "stop" else text)
        else:
            self.start(note=rest if word == "start" else text)
        return self.status()

    def start(self, note: str = "", *, listen: bool = True) -> None:
        with self._lock:
            if self.recording:
                return
            self.steps, self.images, self.note = Steps(self.clock), [], note.strip()
            self.started_at = self.clock()
            self._stopped = threading.Event()
        if listen:
            self._listen()
        self.overlay("teach-on 0")
        self.notify("Recording: show me the task, then press the teach key again (or Stop).")
        self._retry_pending()

    def _listen(self) -> None:
        stopped = self._stopped
        threading.Thread(target=watch_windows, args=(self._window, stopped), name="teach-windows", daemon=True).start()
        try:
            self._accessibility = Accessibility()
            self._accessibility.register()
            threading.Thread(target=self._accessibility.watch, args=(self.steps, self._lock, stopped),
                             name="teach-accessibility", daemon=True).start()
        except Exception:  # noqa: BLE001 - windows and screenshots still record
            LOG.warning("Teach mode: AT-SPI unavailable; recording windows and screenshots only", exc_info=True)
            self._accessibility = None
        threading.Thread(target=self._shots, args=(stopped,), name="teach-shots", daemon=True).start()

    def _window(self, app: str, title: str) -> None:
        with self._lock:
            if self.steps is not None:
                self.steps.window(app, title)

    def tick(self) -> None:
        """Screenshot the settled step, show progress, and stop at the limits (the shots thread calls it)."""
        with self._lock:
            if self.steps is None:
                return
            index = self.steps.ready_for_shot() if len(self.images) < MAX_SHOTS else None
            count = len(self.steps.items)
            over = self.clock() - self.started_at >= MAX_SECONDS or count >= MAX_STEPS
        if index is not None:
            shot = self.shoot()
            with self._lock:
                if shot and self.steps is not None and index < len(self.steps.items):
                    self.steps.items[index]["screenshot"] = len(self.images)
                    self.images.append(shot)
        self.overlay(f"teach-on {count}")
        if over:
            self.notify("Teach mode reached its limit; sending what was recorded.")
            self.stop()

    def _shots(self, stopped: threading.Event) -> None:
        while not stopped.wait(0.25):
            self.tick()

    def _finish(self) -> tuple[Steps | None, list, float, str]:
        with self._lock:
            steps, images, started, note = self.steps, self.images, self.started_at, self.note
            self.steps, self.images = None, []
            self._stopped.set()
        if self._accessibility is not None:
            self._accessibility.deregister()
            self._accessibility = None
        self.overlay("teach-off")
        return steps, images, started, note

    def discard(self) -> None:
        if self._finish()[0] is not None:
            self.notify("Recording discarded.")

    def stop(self, note: str = "", *, wait: bool = False) -> dict | None:
        """End the recording and send it to the main node (in the background unless `wait`)."""
        steps, images, started, recorded_note = self._finish()
        if steps is None:
            return None
        if note.strip():
            steps.note(note)
        if not steps.items:
            self.notify("Nothing was recorded (no window or control changed).")
            return None
        from .node_link import node_name
        body = {"device": node_name(), "note": " ".join(filter(None, (recorded_note, note.strip()))),
                "started_at": started, "seconds": round(self.clock() - started, 1), "steps": steps.public(),
                "images": [{"type": media, "data": base64.b64encode(data).decode()} for data, media in images]}
        if not wait:  # screenshots take a while to upload; the command that stopped it shouldn't wait
            threading.Thread(target=self._deliver, args=(body,), name="teach-send", daemon=True).start()
            return None
        return self._deliver(body)

    def _deliver(self, body: dict) -> dict | None:
        try:
            recording = self.send(body)
        except Exception as exc:  # noqa: BLE001 - keep it for the next try
            LOG.warning("Teach recording not sent: %s", exc)
            self._keep(body)
            self.notify("Recorded, but the main assistant is unreachable; it is kept here and sent next time.")
            return None
        self.notify(f"Recorded {len(body['steps'])} steps. Tell the assistant to learn it, and what it's for.")
        return recording

    def _keep(self, body: dict) -> None:
        if self.pending is None:
            return
        try:
            kept = json.loads(self.pending.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kept = []
        self.pending.parent.mkdir(parents=True, exist_ok=True)
        self.pending.write_text(json.dumps((kept + [body])[-3:]), encoding="utf-8")

    def _retry_pending(self) -> None:
        if self.pending is None or not self.pending.is_file():
            return
        try:
            kept = json.loads(self.pending.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        left = []
        for body in kept:
            try:
                self.send(body)
            except Exception:  # noqa: BLE001
                left.append(body)
        if left:
            self.pending.write_text(json.dumps(left), encoding="utf-8")
        else:
            self.pending.unlink(missing_ok=True)


def _send_to_main(body: dict) -> dict:
    import httpx
    from .emergency import main_node_client
    base, headers = main_node_client()
    with httpx.Client(headers=headers, timeout=httpx.Timeout(10, read=60)) as client:
        page = client.get(base + "/")
        page.raise_for_status()
        token = re.search(r'<meta name="app-token" content="([^"]+)"', page.text)
        if not token:
            raise RuntimeError("The main assistant did not provide its session token.")
        response = client.post(base + "/api/teach/recordings", json=body,
                               headers={"x-ultimate-assistant-token": token.group(1)})
        response.raise_for_status()
        return response.json()["recording"]
