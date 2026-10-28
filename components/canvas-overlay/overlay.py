"""The assistant's canvas on a PC: draws canvas elements over the user's screens.

One transparent, click-through, always-on-top window per monitor that never
takes focus and (by default, on Windows) is left out of screenshots and screen capture, so
the assistant's own screen reading still sees the real screen. It follows the
main node's /api/canvas/stream and redraws on every change.

Windows uses window styles (pywin32). Hyprland (and other wlroots-style Wayland
compositors) uses layer-shell surfaces through KDE's layer-shell-qt, which must
match the Qt that PySide6 runs on, so on Linux run it with the system packages:
pyside6, qt6-webengine, layer-shell-qt and python-httpx.

Interactive ("ui") cards are the exception to click-through: each one is a
small frameless always-on-top web view (Qt WebEngine, from PySide6-Addons)
placed where the card belongs, running the node's /api/canvas/ui/card page. It appears
without taking focus; clicking it lets the user type. Without WebEngine a ui
card is drawn as a plain card saying where to open it.

Env: ULTIMATE_ASSISTANT_URL (the node's HTTP), ULTIMATE_ASSISTANT_APP_TOKEN,
ULTIMATE_ASSISTANT_CANVAS_DEVICE (default "pc"), ULTIMATE_ASSISTANT_ROUTER_KEY (a device that
follows the main node's canvas through Herald, /nodes/main/http, sends Herald's key with every
request, including the cards' pages),
ULTIMATE_ASSISTANT_CANVAS_CAPTURE=1 to let screenshots include the overlay.
Works over normal and borderless-windowed apps and games, not over exclusive
fullscreen.

Wayland has no per-surface capture exclusion (Hyprland blacks out the whole layer), so on
Linux the overlay steps aside instead: a screen capture sends "hide" to the socket
$XDG_RUNTIME_DIR/ultimate-assistant-canvas.sock, waits for "ok", captures, then sends "show".

Pointing mode is the way back: the user marks things for the assistant (a point, a box, a
freehand stroke, a whole window) on an input layer over every monitor, and the marks go to the
main node with a screenshot that has them drawn in (POST /api/canvas/marks). It starts when an
agent asks (a "point" element), from the socket ("point-toggle" for the hotkey; "point-voice-on",
"point-voice-done", "point-voice-cancel" while the user talks), and ends with Enter or Esc.

Teach mode (the device's teach.py) records a task the user shows the assistant; while it runs the
overlay shows a small "Recording" badge with the step count and Stop / Discard ("teach-on <steps>",
"teach-off" on the socket). The buttons go back to the device's controller (desktop.py's socket).
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
from PySide6.QtCore import QEvent, QMargins, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAbstractTextDocumentLayout, QColor, QDesktopServices, QFont, QGuiApplication, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QTextDocument)
from PySide6.QtWidgets import QApplication, QWidget

if sys.platform == "win32":
    import win32api
    import win32con
    import win32gui
else:
    import shiboken6

try:  # interactive cards need Qt WebEngine (PySide6-Addons); everything else works without it
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover - depends on the installed Qt
    QWebEnginePage = QWebEngineView = None

BASE = os.environ.get("ULTIMATE_ASSISTANT_URL", "http://127.0.0.1:8841").rstrip("/")
TOKEN = os.environ.get("ULTIMATE_ASSISTANT_APP_TOKEN", "")
DEVICE = os.environ.get("ULTIMATE_ASSISTANT_CANVAS_DEVICE", "pc")
MAIN_NODE = os.environ.get("ULTIMATE_ASSISTANT_CANVAS_MAIN", "1") != "0"
CAPTURE = os.environ.get("ULTIMATE_ASSISTANT_CANVAS_CAPTURE") == "1"
ROUTER_KEY = os.environ.get("ULTIMATE_ASSISTANT_ROUTER_KEY", "")


def _router_auth(headers: dict) -> dict:
    if ROUTER_KEY:
        headers["Authorization"] = f"Bearer {ROUTER_KEY}"
    return headers
ACCENT = QColor("#7aa2f7")
CARD = QColor(22, 24, 32, 225)
INK = QColor("#e6e8ef")
MARGIN = 28
UI_WIDTH = 400  # default width of an interactive card, in logical pixels
VIDEO_WIDTH = 560  # a video card's default width: big enough to watch
MIN_SIZE = (220, 110)  # the smallest a user can resize a card to
GRIP = 26  # the top strip that moves a draggable card (genui card page: .ua-grip)
CORNER = 18  # the bottom-right square that resizes a resizable card (.ua-resize)
CARD_FILL = QColor("#16181f")  # an interactive card's window: genui.css --ua-card-fill


def _load_omarchy_theme() -> bool:
    """Sync overlay colors with the active Omarchy theme (colors.toml) when running on Omarchy."""
    global ACCENT, CARD, INK, CARD_FILL
    state_home = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    colors_file = state_home / "omarchy" / "current" / "theme" / "colors.toml"
    if not colors_file.is_file():
        return False
    try:
        try:
            import tomllib
            palette = tomllib.loads(colors_file.read_text(encoding="utf-8"))
        except Exception:
            palette = {}
            for line in colors_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    palette[k.strip()] = v.strip().strip('"').strip("'")
        accent = palette.get("accent") or palette.get("blue")
        bg = palette.get("dark_background") or palette.get("background")
        fg = palette.get("bright_foreground") or palette.get("foreground")
        if accent:
            ACCENT = QColor(str(accent))
        if bg:
            base_bg = QColor(str(bg))
            CARD_FILL = QColor(base_bg)
            CARD = QColor(base_bg.red(), base_bg.green(), base_bg.blue(), 228)
        if fg:
            INK = QColor(str(fg))
        return True
    except Exception:
        return False


_load_omarchy_theme()


class Feed(QObject):
    changed = Signal(list)

    def run(self) -> None:
        headers = _router_auth({"x-ultimate-assistant-token": TOKEN, "Accept": "text/event-stream"})
        while True:
            try:
                with httpx.stream("GET", f"{BASE}/api/canvas/stream", params={"device": DEVICE, "main": str(MAIN_NODE).lower()},
                                  headers=headers, timeout=httpx.Timeout(10, read=60)) as response:
                    response.raise_for_status()
                    event = None
                    for line in response.iter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event == "canvas":
                            self.changed.emit(json.loads(line[5:])["elements"])
            except Exception:  # noqa: BLE001 - the runtime restarts or the network blips: retry
                self.changed.emit([])
                time.sleep(3)


def _settings_dirs() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".ultimate-assistant"
    # every UltimateAssistant* state folder (a named variant before the standard one)
    folders = [path for path in base.glob("UltimateAssistant*") if path.is_dir()] if base.is_dir() else []
    return sorted(folders, key=lambda path: (path.name == "UltimateAssistant", path.name)) or [base / "UltimateAssistant"]


def _foreground_device() -> str:
    """The monitor (device name) the user is working on: where the foreground window is."""
    if sys.platform != "win32":
        return _hyprland_foreground_monitor()
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return ""
    try:
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        return win32api.GetMonitorInfo(monitor)["Device"]
    except Exception:  # noqa: BLE001
        return ""


def _adapter(device: str) -> str:
    if sys.platform != "win32":
        return next((m.get("description", "") for m in _hypr("j/monitors") or [] if m.get("name") == device), "")
    index = 0
    while True:
        try:
            d = win32api.EnumDisplayDevices(None, index)
        except Exception:  # noqa: BLE001
            return ""
        if d.DeviceName == device:
            return d.DeviceString
        index += 1


def _assistant_display() -> str:
    for folder in _settings_dirs():
        try:
            value = json.load(open(folder / "settings.json", encoding="utf-8")).get("operator_display")
        except (OSError, ValueError):
            continue
        if value:
            return str(value)
    return "Virtual Display Driver"


class Layer(QWidget):
    """The canvas on one monitor."""

    def __init__(self, screen, index: int, pixmaps: dict) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.screen_ref, self.index, self.pixmaps = screen, index, pixmaps
        self.device = screen_device(screen)  # "\\.\DISPLAYn" on Windows, the output name ("eDP-1") on Wayland
        self.elements: list[dict] = []
        self.heights: dict[str, int] = {}  # reported content height of each interactive card
        self.moves: dict[str, dict] = {}  # where the user moved or sized cards (Canvas.moves)
        self.placed: list[tuple] = []  # (element, rect, document, image) for cards; drawings have no rect
        self.ui_rects: dict[str, QRect] = {}  # where each interactive card's window goes, in screen coordinates
        self.paused = False
        if sys.platform != "win32":
            # the whole output on the overlay layer; input passes through, never keyboard focus
            _layer_shell(self, screen, layer=3, anchors=_ALL_EDGES, keyboard=0, exclusive_zone=-1)
            self.resize(screen.geometry().size())
            self.show()
            return
        self.setGeometry(screen.geometry())
        self.show()
        hwnd = int(self.winId())
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style | win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
                               | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW)
        if not CAPTURE:
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE

    def wants(self, element: dict, active: str, assistant: str) -> bool:
        monitor = element.get("monitor", "active")
        if monitor == "all":
            return True
        if monitor == "active":
            return self.device == active or (not active and self.screen_ref == QGuiApplication.primaryScreen())
        if monitor == "assistant":
            return assistant in (self.device, str(self.index)) or assistant.lower() in _adapter(self.device).lower()
        return monitor == self.index

    def show_elements(self, elements: list[dict]) -> None:
        self.elements = elements
        self.arrange()

    def arrange(self) -> None:
        """Place every card (stacking cards that share a position) and the interactive cards' windows."""
        stacks: dict[str, float] = {}
        self.placed, self.ui_rects = [], {}
        now = time.time()
        for element in self.elements:
            if element.get("expires_at") and element["expires_at"] <= now:
                continue
            if element["kind"] == "drawing":
                self.placed.append((element, None, None, None))
            elif element["kind"] == "ui" and QWebEngineView is not None:
                position = element.get("position", "top-right")
                default = VIDEO_WIDTH if element.get("template") == "video" else UI_WIDTH
                width = position.get("w") * self.width() if isinstance(position, dict) and position.get("w") else default
                width = max(260.0, min(width, self.width() - 2 * MARGIN))
                height = min(float(self.heights.get(element["id"], 160)), self.height() - 2 * MARGIN)
                moved = self.moves.get(element["id"])
                if moved and moved["key"] == _placement_key(element):
                    # where the user dragged or sized it, kept inside this monitor
                    width = min(moved.get("w") or width, self.width())
                    height = min(moved.get("h") or height, self.height())
                    x = max(0.0, min(moved["x"], self.width() - width))
                    y = max(0.0, min(moved["y"], self.height() - height))
                else:
                    x, y = self._place(position, width, height, stacks)
                origin = self.screen_ref.geometry().topLeft()
                self.ui_rects[element["id"]] = QRect(int(origin.x() + x), int(origin.y() + y), int(width), int(height))
            else:
                self.placed.append(self._measure(element, stacks))
        self.update()

    # -- painting -----------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        if self.paused:  # a screen capture is running: draw nothing
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        for element, rect, doc, image in self.placed:
            if rect is None:
                self._drawing(painter, element)
            else:
                self._card(painter, element, rect, doc, image)
        painter.end()

    def _point(self, x: float, y: float) -> QPointF:
        return QPointF(x * self.width(), y * self.height())

    def _pen(self, shape: dict, element: dict) -> QPen:
        pen = QPen(QColor(shape.get("color") or element.get("color") or "#ff5d5d"))
        pen.setWidthF(float(shape.get("width", 4)))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def _drawing(self, painter: QPainter, element: dict) -> None:
        for shape in element.get("shapes", []):
            pen = self._pen(shape, element)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            kind = shape["type"]
            if kind == "stroke":
                path = QPainterPath(self._point(*shape["points"][0]))
                for x, y in shape["points"][1:]:
                    path.lineTo(self._point(x, y))
                painter.drawPath(path)
            elif kind in ("rect", "ellipse"):
                rect = QRectF(self._point(shape["x"], shape["y"]),
                              self._point(shape["x"] + shape["w"], shape["y"] + shape["h"]))
                painter.drawRoundedRect(rect, 10, 10) if kind == "rect" else painter.drawEllipse(rect)
            elif kind in ("line", "arrow"):
                start, end = self._point(shape["x"], shape["y"]), self._point(shape["x2"], shape["y2"])
                painter.drawLine(start, end)
                if kind == "arrow":
                    direction = end - start
                    length = max((direction.x() ** 2 + direction.y() ** 2) ** 0.5, 1)
                    unit = QPointF(direction.x() / length, direction.y() / length)
                    normal = QPointF(-unit.y(), unit.x())
                    size = 10 + pen.widthF() * 3
                    head = QPolygonF([end, end - unit * size + normal * size * 0.6, end - unit * size - normal * size * 0.6])
                    painter.setBrush(pen.color())
                    painter.drawPolygon(head)
            elif kind == "label":
                font = QFont("Segoe UI", 15, QFont.DemiBold)
                painter.setFont(font)
                text = shape.get("text", "")
                at = self._point(shape["x"], shape["y"])
                bounds = painter.fontMetrics().boundingRect(text).adjusted(-10, -6, 10, 6)
                bounds.moveTopLeft(at.toPoint())
                painter.setPen(Qt.NoPen)
                painter.setBrush(CARD)
                painter.drawRoundedRect(bounds, 8, 8)
                painter.setPen(pen.color())
                painter.drawText(bounds, Qt.AlignCenter, text)

    def _measure(self, element: dict, stacks: dict[str, float]) -> tuple:
        position = element.get("position", "top-right")
        width = (position.get("w") if isinstance(position, dict) and position.get("w") else 0.26) * self.width()
        width = max(260.0, min(width, self.width() - 2 * MARGIN))
        doc = QTextDocument()
        doc.setDefaultFont(QFont("Segoe UI", 11))
        doc.setDefaultStyleSheet(f"* {{ color: {INK.name()}; }} a {{ color: {ACCENT.name()}; }}")
        title = element.get("title", "")
        body = element.get("body", "")
        if element["kind"] == "ui":  # no WebEngine here: say where the card can be used
            body = "Interactive card: open it in the assistant's browser view (/api/canvas/ui/card?id=" + element["id"] + ")."
        if element["kind"] == "markdown":
            doc.setMarkdown((f"**{title}**\n\n" if title else "") + body)
        else:
            html = (f"<b>{_escape(title)}</b><br>" if title else "") + _escape(body).replace("\n", "<br>")
            doc.setHtml(html)
        inner = width - 36
        doc.setTextWidth(inner)
        image = self.pixmaps.get(element.get("image", "")) if element["kind"] == "image" else None
        image_height = 0.0
        if image is not None and not image.isNull():
            image_height = image.height() * inner / max(image.width(), 1)
            image_height = min(image_height, self.height() * 0.6)
        height = doc.size().height() + image_height + (12 if image_height and (title or body) else 0) + 28
        x, y = self._place(position, width, height, stacks)
        return element, QRectF(x, y, width, height), doc, (image if image_height else None)

    def _card(self, painter: QPainter, element: dict, rect: QRectF, doc: QTextDocument, image) -> None:
        x, y, width, height = rect.x(), rect.y(), rect.width(), rect.height()
        accent = QColor(element.get("color") or ("#e0af68" if element["kind"] == "reminder" else ACCENT.name()))
        inner = width - 36
        image_height = image.height() * inner / max(image.width(), 1) if image is not None else 0.0
        image_height = min(image_height, self.height() * 0.6)
        painter.setPen(Qt.NoPen)
        painter.setBrush(CARD)
        painter.drawRoundedRect(rect, 14, 14)
        painter.setBrush(accent)
        painter.drawRoundedRect(QRectF(x, y + 14, 4, height - 28), 2, 2)
        top = y + 14
        if image_height:
            painter.drawPixmap(QRectF(x + 18, top, inner, image_height), image, QRectF(image.rect()))
            top += image_height + 12
        painter.save()
        painter.translate(x + 18, top)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.Text, INK)  # list bullets and plain text use the palette
        doc.documentLayout().draw(painter, context)
        painter.restore()

    def _place(self, position, width: float, height: float, stacks: dict[str, float]) -> tuple[float, float]:
        if isinstance(position, dict):
            return position["x"] * (self.width() - width), position["y"] * (self.height() - height)
        horizontal = "left" if "left" in position else "right" if "right" in position else "center"
        vertical = "top" if position.startswith("top") else "bottom" if position.startswith("bottom") else "middle"
        x = {"left": MARGIN, "right": self.width() - width - MARGIN, "center": (self.width() - width) / 2}[horizontal]
        offset = stacks.get(position, 0.0)
        if vertical == "top":
            y = MARGIN + offset
        elif vertical == "bottom":
            y = self.height() - height - MARGIN - offset
        else:
            y = (self.height() - height) / 2 + offset
        stacks[position] = offset + height + 12  # cards at the same spot stack instead of overlapping
        return x, y


class CardPage(QWebEnginePage if QWebEnginePage else object):
    """Keeps the card page in place: links and window.open go to the user's browser."""

    def acceptNavigationRequest(self, url, _kind, main_frame) -> bool:  # noqa: N802 - Qt override
        if main_frame and url.scheme() in ("http", "https") and not url.toString().startswith(BASE):
            QDesktopServices.openUrl(url)
            return False
        return True

    def createWindow(self, _kind):  # noqa: N802 - Qt override
        return ExternalPage(self.profile(), self)

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802 - Qt override
        print(f"card js: {message} ({source}:{line})")


class ExternalPage(QWebEnginePage if QWebEnginePage else object):
    def acceptNavigationRequest(self, url, _kind, _main_frame) -> bool:  # noqa: N802 - Qt override
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
        self.deleteLater()
        return False


class RouterAuth(QWebEngineUrlRequestInterceptor if QWebEngineView else object):
    """Adds Herald's key to the cards' own requests when the canvas comes through Herald."""

    def interceptRequest(self, info) -> None:
        if info.requestUrl().toString().startswith(BASE + "/api/canvas/media/"):
            info.setHttpHeader(b"x-ultimate-assistant-token", TOKEN.encode())
        if ROUTER_KEY and info.requestUrl().toString().startswith(BASE):
            info.setHttpHeader(b"Authorization", f"Bearer {ROUTER_KEY}".encode())


class CardWindow(QWebEngineView if QWebEngineView else object):
    """One interactive card: a frameless always-on-top web view running /api/canvas/ui/card."""

    def __init__(self, on_height, mouse_filter) -> None:
        super().__init__(None)
        self.mouse_filter = mouse_filter
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # appearing never steals focus; a click does
        # Opaque on purpose: a translucent top-level web view often paints nothing on Windows.
        # The card fills the window and Windows 11 rounds its corners.
        self.setPage(CardPage(self))
        self.page().setBackgroundColor(CARD_FILL)
        self.on_height, self.ready, self.pending, self.shown_at = on_height, False, None, None
        self.element_id = ""
        self.draggable = self.resizable = self.sized = False
        self.closed = ""
        self.shell = None  # its layer-shell settings on Wayland
        self.loadFinished.connect(self._loaded)
        self.titleChanged.connect(self._title)
        self.load(QUrl(f"{BASE}/api/canvas/ui/card?embedded=1#token={TOKEN}"))

    def show_element(self, element: dict) -> None:
        self.element_id = element["id"]
        self.draggable, self.resizable = bool(element.get("draggable")), bool(element.get("resizable"))
        if element.get("updated_at") == self.shown_at:
            return
        self.shown_at = element.get("updated_at")
        self.pending = element
        if self.ready:
            self._push()

    def _push(self) -> None:
        if self.pending is not None:
            self.page().runJavaScript(f"window.uaShow({json.dumps(json.dumps(self.pending))})")
            self.pending = None
        if self.ready:
            self.page().runJavaScript(f"window.uaSized && window.uaSized({json.dumps(self.sized)})")

    def set_sized(self, sized: bool) -> None:
        if sized != self.sized:
            self.sized = sized
            self._push()

    def handle_at(self, point) -> str:
        """What a press at `point` (window coordinates) grabs: "move", "resize" or ""."""
        if self.resizable and point.x() >= self.width() - CORNER and point.y() >= self.height() - CORNER:
            return "resize"
        if self.draggable and point.y() < GRIP and point.x() < self.width() - 40:  # not the close button
            return "move"
        return ""

    def childEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().childEvent(event)
        # once the web view's inner widget is built, its mouse goes through the canvas (moving, sizing)
        if event.polished() and isinstance(event.child(), QWidget):
            event.child().installEventFilter(self.mouse_filter)

    def _loaded(self, ok: bool) -> None:
        if not ok:
            print(f"{time.strftime('%H:%M:%S')} card page failed to load for {self.element_id}")
        self.ready = ok
        if ok:
            self._push()

    def _title(self, title: str) -> None:
        if title == "ua-close":  # closed on the card: hide now, don't wait for the canvas to drop it
            self.closed = self.element_id
            self.hide()
            return
        if title.startswith("ua-h:") and title[5:].isdigit():
            self.on_height(self.element_id, int(title[5:]) + 2)

    def place(self, rect: QRect) -> None:
        if self.closed and self.closed == self.element_id:
            return
        if sys.platform != "win32":
            self._place_layer(rect)
            return
        if self.geometry() != rect:
            self.setGeometry(rect)
        if not self.isVisible():
            self.show()
            hwnd = int(self.winId())
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style | win32con.WS_EX_TOOLWINDOW)
            corner = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
            if not CAPTURE:
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE

    def _place_layer(self, rect: QRect) -> None:
        """Wayland: a layer surface pinned at the card's spot on its output. Keyboard focus only
        once the user clicks into it, so appearing never takes the keyboard."""
        if self.shell is None:
            self.screen_ref = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
            self.shell = _layer_shell(self, self.screen_ref, layer=3, anchors=_TOP | _LEFT, keyboard=2)
        origin = self.screen_ref.geometry().topLeft()
        self.shell.setProperty("margins", QMargins(rect.x() - origin.x(), rect.y() - origin.y(), 0, 0))
        if self.size() != rect.size():
            self.resize(rect.size())
        if not self.isVisible():
            self.show()


MARK_COLOR = QColor("#ff4d6d")
TOOLS = (("point", "Point", "1"), ("box", "Box", "2"), ("draw", "Draw", "3"), ("window", "Window", "4"))


class PointLayer(QWidget):
    """Pointing mode on one monitor: takes the mouse and keyboard and draws the user's marks."""

    def __init__(self, pointing: "Pointing", screen, index: int) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.pointing, self.screen_ref, self.index = pointing, screen, index
        self.device = screen_device(screen)
        self.press = self.hover = None
        self.stroke: list[QPointF] = []
        self.buttons: dict[str, QRectF] = {}
        if sys.platform != "win32":
            _layer_shell(self, screen, layer=3, anchors=_ALL_EDGES, keyboard=1, exclusive_zone=-1)  # 1: exclusive
            self.resize(screen.geometry().size())
        else:
            self.setGeometry(screen.geometry())
        self.show()
        self.activateWindow()
        self.setFocus()

    # -- input ----------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.pointing.finish(submit=True)
        elif key == Qt.Key_Escape:
            self.pointing.finish(submit=False)
        elif key == Qt.Key_Backspace or (key == Qt.Key_Z and event.modifiers() & Qt.ControlModifier):
            self.pointing.undo()
        else:
            for tool, _, shortcut in TOOLS:
                if event.text() == shortcut and tool in self.pointing.tools:
                    self.pointing.tool = tool
                    self.pointing.repaint()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        at = event.position()
        for name, rect in self.buttons.items():
            if rect.contains(at):
                if name == "send":
                    self.pointing.finish(submit=True)
                elif name == "cancel":
                    self.pointing.finish(submit=False)
                else:
                    self.pointing.tool = name
                    self.pointing.repaint()
                return
        if event.button() == Qt.RightButton:
            self.pointing.undo()
            return
        if event.button() == Qt.LeftButton:
            self.press, self.stroke = at, [at]

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        at = event.position()
        if self.press is not None:
            self.stroke.append(at)
        if self.pointing.tool == "window":
            self.hover = self.pointing.window_at(self, at)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.press is None or event.button() != Qt.LeftButton:
            return
        start, end, tool = self.press, event.position(), self.pointing.tool
        self.press = None
        moved = abs(end.x() - start.x()) + abs(end.y() - start.y()) > 8
        w, h = self.width(), self.height()
        if tool == "window":
            window = self.pointing.window_at(self, end)
            if window:
                rect = window["rect"]
                self.pointing.add(self, {"type": "window", "x": rect.x() / w, "y": rect.y() / h,
                                         "w": rect.width() / w, "h": rect.height() / h}, window)
        elif tool == "draw" and moved and len(self.stroke) > 1:
            step = max(1, len(self.stroke) // 256)  # at most ~256 points
            points = [[min(max(p.x() / w, 0), 1), min(max(p.y() / h, 0), 1)] for p in self.stroke[::step]]
            self.pointing.add(self, {"type": "draw", "points": points})
        elif tool == "box" and moved:
            left, top = min(start.x(), end.x()), min(start.y(), end.y())
            self.pointing.add(self, {"type": "box", "x": max(left / w, 0), "y": max(top / h, 0),
                                     "w": min(abs(end.x() - start.x()) / w, 1), "h": min(abs(end.y() - start.y()) / h, 1)})
        else:  # a click is a point with any tool
            self.pointing.add(self, {"type": "point", "x": min(max(end.x() / w, 0), 1), "y": min(max(end.y() / h, 0), 1)})
        self.stroke = []
        self.update()

    # -- painting -------------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self.pointing.capturing:
            painter.end()
            return
        voice = self.pointing.source == "voice"
        painter.fillRect(self.rect(), QColor(0, 0, 0, 25 if voice else 70))
        pen = QPen(MARK_COLOR, 3)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(1.5, 1.5, self.width() - 3, self.height() - 3))  # "the screen is listening"
        number = 0
        for screen_index, mark, _ in self.pointing.marks:
            number += 1
            if screen_index == self.index:
                draw_mark(painter, mark, number, self.width(), self.height())
        if self.press is not None and self.stroke:  # the mark being made
            painter.setPen(QPen(MARK_COLOR, 3, Qt.DashLine))
            if self.pointing.tool == "draw":
                path = QPainterPath(self.stroke[0])
                for point in self.stroke[1:]:
                    path.lineTo(point)
                painter.drawPath(path)
            elif self.pointing.tool == "box":
                painter.drawRect(QRectF(self.press, self.stroke[-1]).normalized())
        if self.hover and self.pointing.tool == "window":
            painter.setPen(QPen(MARK_COLOR, 2, Qt.DashLine))
            painter.setBrush(QColor(255, 77, 109, 40))
            painter.drawRect(QRectF(self.hover["rect"]))
        self._toolbar(painter)
        painter.end()

    def _toolbar(self, painter: QPainter) -> None:
        pointing = self.pointing
        painter.setFont(QFont("Segoe UI", 11))
        metrics = painter.fontMetrics()
        items = [(tool, f"{label} {key}") for tool, label, key in TOOLS if tool in pointing.tools]
        items += [("send", "Send ⏎"), ("cancel", "Cancel Esc")]
        widths = [metrics.horizontalAdvance(text) + 24 for _, text in items]
        prompt = pointing.prompt or {"voice": "Listening: point at what you mean",
                                     "user": "Point at things, then ask"}.get(pointing.source, "")
        prompt_width = metrics.horizontalAdvance(prompt) + 28 if prompt else 0
        total = sum(widths) + 6 * (len(items) - 1) + prompt_width + 20
        x, y, height = (self.width() - total) / 2, 14.0, 34.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(CARD)
        painter.drawRoundedRect(QRectF(x, y, total, height + 12), 12, 12)
        x += 10
        if prompt:
            painter.setPen(INK)
            painter.drawText(QRectF(x, y + 6, prompt_width, height), Qt.AlignVCenter | Qt.AlignLeft, prompt)
            x += prompt_width
        self.buttons = {}
        for (name, text), width in zip(items, widths):
            rect = QRectF(x, y + 6, width, height)
            selected = name == pointing.tool
            painter.setPen(Qt.NoPen)
            painter.setBrush(MARK_COLOR if selected else QColor(255, 255, 255, 18))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QColor("#ffffff") if selected else INK)
            painter.drawText(rect, Qt.AlignCenter, text)
            self.buttons[name] = rect
            x += width + 6


def draw_mark(painter: QPainter, mark: dict, number: int, width: float, height: float) -> None:
    """One mark, numbered, on a surface of the given size (the screen, or a screenshot of it)."""
    scale = max(width / 1280, 1.0)
    pen = QPen(MARK_COLOR, 4 * scale)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    kind = mark["type"]
    if kind == "draw":
        points = [QPointF(x * width, y * height) for x, y in mark["points"]]
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        painter.drawPath(path)
        anchor = points[0]
    elif kind == "point":
        anchor = QPointF(mark["x"] * width, mark["y"] * height)
        painter.setBrush(QColor(255, 77, 109, 90))
        painter.drawEllipse(anchor, 14 * scale, 14 * scale)
        painter.setBrush(MARK_COLOR)
        painter.drawEllipse(anchor, 4 * scale, 4 * scale)
    else:
        rect = QRectF(mark["x"] * width, mark["y"] * height, mark["w"] * width, mark["h"] * height)
        painter.drawRoundedRect(rect, 6 * scale, 6 * scale)
        anchor = rect.topLeft()
    radius = 12 * scale  # the number the assistant and the user both refer to
    badge = QPointF(anchor.x() + radius + 4 * scale, anchor.y() - radius - 4 * scale) if kind == "point" else anchor
    painter.setPen(Qt.NoPen)
    painter.setBrush(MARK_COLOR)
    painter.drawEllipse(badge, radius, radius)
    painter.setPen(QColor("#ffffff"))
    painter.setFont(QFont("Segoe UI", max(8, int(10 * scale)), QFont.Bold))
    painter.drawText(QRectF(badge.x() - radius, badge.y() - radius, 2 * radius, 2 * radius), Qt.AlignCenter, str(number))


class Pointing(QObject):
    """Pointing mode across the monitors: who started it, the marks so far, and sending them."""

    submitted = Signal(object)  # the callbacks to answer once the marks are sent

    def __init__(self, canvas: "Canvas") -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.layers: list[PointLayer] = []
        self.marks: list[tuple[int, dict, dict | None]] = []  # (monitor index, mark, window under it)
        self.source = ""  # "user" (hotkey), "voice", or the id of an agent's point request
        self.prompt, self.tool, self.tools = "", "box", [t for t, _, _ in TOOLS]
        self.capturing = False
        self.answered: set[str] = set()  # point requests already handled here
        self.waiting: list = []  # socket replies owed once the marks are sent
        self.submitted.connect(self._answer)

    @property
    def active(self) -> bool:
        return bool(self.layers)

    def start(self, source: str, prompt: str = "", tools: list[str] | None = None) -> None:
        self.source, self.prompt = source, prompt
        self.tools = [t for t, _, _ in TOOLS if not tools or t in tools]
        if sys.platform != "win32" and _hypr("j/monitors") is None:
            self.tools = [t for t in self.tools if t != "window"] or ["point"]  # windows come from Hyprland here
        self.tool = "box" if "box" in self.tools else self.tools[0]
        if not self.layers:
            self.marks = []
            self.layers = [PointLayer(self, screen, i + 1) for i, screen in enumerate(_ordered_screens())]
        self.repaint()

    def command(self, command: str, reply) -> None:
        if command == "toggle":
            if self.active and self.source != "voice":
                self.finish(submit=True)
            elif not self.active:
                self.start("user")
            reply()
        elif command == "voice-on":
            if not self.active:
                self.start("voice")
            reply()
        elif command == "voice-done":
            if self.active and self.source in ("voice", "user"):
                self.waiting.append(reply)
                self.finish(submit=True)
            else:
                reply()
        elif command == "voice-cancel":
            if self.active and self.source == "voice":
                self.finish(submit=False)
            reply()

    def follow(self, requests: list[dict]) -> None:
        """An agent's point requests: start for a new one; stop if ours went away (it timed out)."""
        if self.active and self.source.startswith("point-") and self.source not in {r["id"] for r in requests}:
            self.close()
            return
        for request in requests:
            if request["id"] not in self.answered and not self.capturing:
                self.answered.add(request["id"])
                self.start(request["id"], request.get("title", ""), request.get("tools"))
                return

    def add(self, layer: PointLayer, mark: dict, window: dict | None = None) -> None:
        if window is None:  # the window under the mark, for the assistant's sake
            if mark["type"] == "draw":
                xs, ys = [p[0] for p in mark["points"]], [p[1] for p in mark["points"]]
                cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            else:
                cx, cy = mark["x"] + mark.get("w", 0) / 2, mark["y"] + mark.get("h", 0) / 2
            window = self.window_at(layer, QPointF(cx * layer.width(), cy * layer.height()))
        mark["monitor"] = layer.device
        if window:
            mark["window"] = {k: window[k] for k in ("class", "title", "address") if window.get(k)}
        self.marks.append((layer.index, mark, window))
        self.repaint()

    def undo(self) -> None:
        if self.marks:
            self.marks.pop()
            self.repaint()

    def repaint(self) -> None:
        for layer in self.layers:
            layer.update()

    def window_at(self, layer: PointLayer, at: QPointF) -> dict | None:
        """The topmost window under `at` on this layer's monitor, in layer coordinates."""
        if sys.platform == "win32":
            return _win32_window_at(layer, at)
        monitors = _hypr("j/monitors") or []
        monitor = next((m for m in monitors if m.get("name") == layer.device), None)
        if monitor is None:
            return None
        visible = {(monitor.get("activeWorkspace") or {}).get("id"), (monitor.get("specialWorkspace") or {}).get("id")}
        best = None
        for client in _hypr("j/clients") or []:
            if not client.get("mapped") or client.get("hidden") or (client.get("workspace") or {}).get("id") not in visible:
                continue
            (x, y), (w, h) = client.get("at", (0, 0)), client.get("size", (0, 0))
            rect = QRect(int(x - monitor.get("x", 0)), int(y - monitor.get("y", 0)), int(w), int(h))
            if rect.contains(at.toPoint()) and (best is None or client.get("focusHistoryID", 99) < best[0]):
                best = (client.get("focusHistoryID", 99), {"class": client.get("class", ""), "title": client.get("title", ""),
                                                           "address": client.get("address", ""), "rect": rect})
        return best[1] if best else None

    def finish(self, submit: bool) -> None:
        if not self.active or self.capturing:
            return
        if not submit or not self.marks:
            if self.source.startswith("point-"):
                request = self.source
                threading.Thread(target=_post, args=(f"/api/canvas/{request}/events", {"type": "close"}), daemon=True).start()
            self.close()
            return
        # Capture without the pointing layers and the canvas, then draw the marks onto the shots.
        self.capturing = True
        self.repaint()
        for layer in self.layers:
            layer.hide()
        self.canvas.pause(True)
        QTimer.singleShot(150, self._capture)

    def _capture(self) -> None:
        marks, source = list(self.marks), self.source
        screens = {layer.index: layer for layer in self.layers}
        shots = []
        for index in sorted({i for i, _, _ in marks}):
            layer = screens[index]
            shot = _screenshot(layer.screen_ref, layer.device)
            if shot is not None:
                shots.append((index, layer.device, shot, layer.width(), layer.height()))
        self.canvas.pause(False)
        self.close()
        images = []
        for index, device, shot, width, height in shots:
            painter = QPainter(shot)
            painter.setRenderHint(QPainter.Antialiasing)
            number = 0
            for screen_index, mark, _ in marks:
                number += 1
                if screen_index == index:
                    draw_mark(painter, mark, number, shot.width(), shot.height())
            painter.end()
            images.append({"monitor": device, "type": "image/jpeg", "data": _jpeg_base64(shot)})
        body = {"device": DEVICE, "marks": [m for _, m, _ in marks], "images": images,
                "request_id": source if source.startswith("point-") else ""}

        def send() -> None:
            ok = _post("/api/canvas/marks", body)
            if ok and source == "user":
                _notify(f"Marked {len(marks)} thing{'s' if len(marks) != 1 else ''}. Ask about {'them' if len(marks) != 1 else 'it'} now.")
            self.submitted.emit(None)
        threading.Thread(target=send, daemon=True).start()

    def _answer(self, _value) -> None:
        waiting, self.waiting = self.waiting, []
        for reply in waiting:
            reply()

    def close(self) -> None:
        for layer in self.layers:
            layer.close()
            layer.deleteLater()
        sending = self.capturing  # marks on their way: whoever waits is answered once they are sent
        self.layers, self.capturing, self.source, self.prompt, self.marks = [], False, "", "", []
        if not sending:
            self._answer(None)


class TeachBadge(QWidget):
    """Teach mode is recording: a small badge at the top of a monitor with Stop and Discard."""

    def __init__(self, screen) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # recording must not change where the user is
        self.steps, self.buttons = 0, {}
        size = QRect(0, 0, 470, 46)
        if sys.platform != "win32":
            shell = _layer_shell(self, screen, layer=3, anchors=_TOP, keyboard=0)
            shell.setProperty("margins", QMargins(0, 12, 0, 0))
            self.resize(size.size())
        else:
            geometry = screen.geometry()
            self.setGeometry(geometry.x() + (geometry.width() - size.width()) // 2, geometry.y() + 12,
                             size.width(), size.height())
        self.show()

    def set_steps(self, steps: int) -> None:
        if steps != self.steps:
            self.steps = steps
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(CARD)
        painter.drawRoundedRect(QRectF(self.rect()), 14, 14)
        painter.setBrush(MARK_COLOR)
        painter.drawEllipse(QPointF(20, self.height() / 2), 6, 6)
        painter.setFont(QFont("Segoe UI", 11))
        painter.setPen(INK)
        label = f"Recording · show me the task · {self.steps} step{'s' if self.steps != 1 else ''}"
        painter.drawText(QRectF(34, 0, 270, self.height()), Qt.AlignVCenter | Qt.AlignLeft, label)
        self.buttons = {}
        x = self.width() - 10
        for name, text, fill in (("discard", "Discard", QColor(255, 255, 255, 22)), ("stop", "Stop", MARK_COLOR)):
            width = painter.fontMetrics().horizontalAdvance(text) + 22
            rect = QRectF(x - width, 8, width, self.height() - 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(rect, Qt.AlignCenter, text)
            self.buttons[name] = rect
            x -= width + 6
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        for name, rect in self.buttons.items():
            if rect.contains(event.position()):
                threading.Thread(target=_desktop_command, args=("teach", name), daemon=True).start()


def _desktop_command(action: str, text: str = "") -> None:
    """Tell this device's controller (desktop.py's socket) to do something, e.g. stop teach mode."""
    import socket
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "ultimate-assistant-desktop.sock"
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(15)
            connection.connect(str(path))
            connection.sendall(json.dumps({"action": action, "text": text}).encode() + b"\n")
            connection.recv(65536)
    except OSError as exc:
        print(f"{time.strftime('%H:%M:%S')} {action} {text} not delivered: {exc}")


def _win32_window_at(layer: QWidget, at: QPointF) -> dict | None:
    """Windows: the frontmost real window under a point (Z order, skipping hidden, minimized,
    cloaked and this overlay's own windows). Qt's screen geometry keeps the native top-left and
    scales the size, so a point converts to physical pixels with the screen's ratio."""
    import win32process
    geometry, ratio = layer.screen_ref.geometry(), layer.screen_ref.devicePixelRatio()
    x, y = geometry.x() + at.x() * ratio, geometry.y() + at.y() * ratio
    dwm = ctypes.windll.dwmapi
    found = []

    def visit(hwnd, _):
        if found or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
            return True
        cloaked = ctypes.c_int(0)
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))  # DWMWA_CLOAKED
        if cloaked.value or win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
            return True
        frame = (ctypes.c_long * 4)()  # the visible frame, without the invisible resize borders
        if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(frame), ctypes.sizeof(frame)) != 0:  # EXTENDED_FRAME_BOUNDS
            frame[:] = win32gui.GetWindowRect(hwnd)
        left, top, right, bottom = frame
        if left <= x < right and top <= y < bottom and right - left > 1 and bottom - top > 1 and win32gui.GetWindowText(hwnd):
            found.append((hwnd, (left, top, right, bottom)))
        return True

    win32gui.EnumWindows(visit, None)  # front to back
    if not found:
        return None
    hwnd, (left, top, right, bottom) = found[0]
    rect = QRect(int((left - geometry.x()) / ratio), int((top - geometry.y()) / ratio),
                 int((right - left) / ratio), int((bottom - top) / ratio))
    return {"class": _win32_process_name(hwnd), "title": win32gui.GetWindowText(hwnd),
            "address": hex(hwnd), "rect": rect}


def _win32_process_name(hwnd) -> str:
    """The window's program ("chrome.exe"), standing in for Hyprland's window class."""
    import win32process
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _screenshot(screen, device: str):
    """The monitor as a QImage at its full resolution (grim on Wayland), or None."""
    from PySide6.QtGui import QImage
    if sys.platform != "win32":
        import subprocess
        try:
            data = subprocess.run(["grim", "-o", device, "-t", "png", "-"], capture_output=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        image = QImage()
        return image if image.loadFromData(data) else None
    return screen.grabWindow(0).toImage()


def _jpeg_base64(image) -> str:
    import base64
    from PySide6.QtCore import QBuffer, QIODevice
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPEG", 85)
    return base64.b64encode(bytes(buffer.data())).decode()


def _post(path: str, body: dict) -> bool:
    try:
        response = httpx.post(BASE + path, json=body, headers=_media_headers(), timeout=30)
        if response.status_code >= 400:
            print(f"{time.strftime('%H:%M:%S')} {path} failed: {response.status_code} {response.text[:300]}")
        return response.status_code < 400
    except httpx.HTTPError as exc:
        print(f"{time.strftime('%H:%M:%S')} {path} failed: {exc}")
        return False


def _notify(text: str) -> None:
    import shutil
    import subprocess
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Ultimate Assistant", "-t", "4000", "Ultimate Assistant", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Canvas(QObject):
    images_ready = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.active = ""
        self.pixmaps: dict[str, QPixmap] = {}
        self.layers: list[Layer] = []
        self.elements: list[dict] = []
        self.cards: dict[tuple[int, str], CardWindow] = {}  # (monitor index, element id) -> its window
        self.heights: dict[str, int] = {}
        self.moves: dict[str, dict] = {}  # id -> {key, x, y, w?, h?}: the user's placement, per device
        self.grab: dict | None = None  # the move or resize under way
        self.paused = False
        self.pointing = Pointing(self)
        self.teach_badge: TeachBadge | None = None
        self.rebuild()
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda _s: self.rebuild())
        app.screenRemoved.connect(lambda _s: self.rebuild())
        self.feed = Feed()
        self.feed.changed.connect(self.receive)
        threading.Thread(target=self.feed.run, daemon=True).start()
        self.images_ready.connect(self.relayout)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.route)  # follow the user's active monitor, drop expired items
        self.timer.start(1000)
        self.capture_pause = CapturePause(self)  # capture pauses (Wayland) and pointing commands

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        """Moving and resizing cards. The web view's inner widget gets the mouse, so each card
        window puts this filter on its children (never on the whole application: PySide would wrap
        objects still being built and crash)."""
        kind = event.type()
        if kind not in (QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease) or not isinstance(obj, QWidget):
            return False
        if self.grab is not None:
            if kind == QEvent.MouseMove:
                self._drag()
            elif kind == QEvent.MouseButtonRelease:
                self.grab = None
            return True
        card = obj.window()
        if not isinstance(card, CardWindow) or not card.element_id:
            return False
        point = obj.mapTo(card, event.position().toPoint()) if obj is not card else event.position().toPoint()
        handle = card.handle_at(point)
        if kind == QEvent.MouseMove:
            obj.setCursor({"move": Qt.SizeAllCursor, "resize": Qt.SizeFDiagCursor}.get(handle, Qt.ArrowCursor))
            return False
        if kind != QEvent.MouseButtonPress or event.button() != Qt.LeftButton or not handle:
            return False
        layer = next((l for l in self.layers if card.element_id in l.ui_rects
                      and self.cards.get((l.index, card.element_id)) is card), None)
        if layer is None:
            return False
        rect = layer.ui_rects[card.element_id]
        origin = layer.screen_ref.geometry().topLeft()
        element = next(e for e in layer.elements if e["id"] == card.element_id)
        self.grab = {"id": card.element_id, "handle": handle, "cursor": _cursor(), "key": _placement_key(element),
                     "rect": QRect(rect.x() - origin.x(), rect.y() - origin.y(), rect.width(), rect.height()),
                     "origin": QPoint(origin), "screen": layer.index, "locked": bool(element.get("monitor_locked")),
                     "sized": self.moves.get(card.element_id, {}).get("h") is not None}
        return True

    def _drag(self) -> None:
        grab = self.grab
        now = _cursor()
        dx, dy = now.x() - grab["cursor"].x(), now.y() - grab["cursor"].y()
        rect = grab["rect"]
        move = {"key": grab["key"], "x": float(rect.x()), "y": float(rect.y()), "w": float(rect.width()),
                "screen": grab["screen"]}
        if grab["handle"] == "move":
            move["x"], move["y"] = float(rect.x() + dx), float(rect.y() + dy)
            # The card follows the cursor onto another monitor, and stays there.
            target = None if grab["locked"] else next((l for l in self.layers if l.screen_ref.geometry().contains(now)), None)
            if target is not None and target.index != grab["screen"]:
                origin = target.screen_ref.geometry().topLeft()
                move["x"] = float(grab["origin"].x() + rect.x() + dx - origin.x())
                move["y"] = float(grab["origin"].y() + rect.y() + dy - origin.y())
                move["screen"] = target.index
            if grab["sized"]:
                move["h"] = float(rect.height())
        else:
            move["w"] = float(max(MIN_SIZE[0], rect.width() + dx))
            move["h"] = float(max(MIN_SIZE[1], rect.height() + dy))
        moved_screen = (self.moves.get(grab["id"]) or {}).get("screen", grab["screen"]) != move["screen"]
        self.moves[grab["id"]] = move
        if moved_screen:
            self.route()
        else:
            self.relayout()

    def pause(self, paused: bool) -> None:
        """Hide everything while a screen capture runs (Wayland), then put it back."""
        self.paused = paused
        for layer in self.layers:
            layer.paused = paused
            layer.update()
        if paused:
            for card in self.cards.values():
                card.hide()
        else:
            self.place_cards()
        if self.teach_badge is not None:  # a teach screenshot must not show its own badge
            self.teach_badge.setVisible(not paused)

    def teach(self, recording: bool, steps: int = 0) -> None:
        """Show or update teach mode's badge (on the monitor in use when it starts), or remove it."""
        if not recording:
            if self.teach_badge is not None:
                self.teach_badge.close()
                self.teach_badge.deleteLater()
                self.teach_badge = None
            return
        if self.teach_badge is None:
            active = _foreground_device()
            screen = next((l.screen_ref for l in self.layers if l.device == active), QGuiApplication.primaryScreen())
            self.teach_badge = TeachBadge(screen)
        self.teach_badge.set_steps(steps)

    def rebuild(self) -> None:
        for layer in self.layers:
            layer.close()
        self.layers = [Layer(screen, i + 1, self.pixmaps) for i, screen in enumerate(_ordered_screens())]
        for layer in self.layers:
            layer.heights, layer.paused, layer.moves = self.heights, self.paused, self.moves
        self.route()

    def receive(self, elements: list) -> None:
        if elements != self.elements:
            print(f"{time.strftime('%H:%M:%S')} canvas: {', '.join(e['kind'] + ':' + e['id'] for e in elements) or 'empty'}")
        elements = [_promote(e) for e in elements]
        live = {e["id"]: _placement_key(e) for e in elements}
        for element_id in [i for i, m in self.moves.items() if live.get(i) != m["key"]]:
            del self.moves[element_id]  # gone, or the assistant placed it somewhere new
        self.elements = elements
        missing = [(e["image"], e["id"]) for e in elements if e["kind"] == "image" and e.get("image") not in self.pixmaps]
        if missing:
            threading.Thread(target=self._load, args=(missing,), daemon=True).start()
        self.route()

    def _load(self, sources: list[tuple[str, str]]) -> None:
        for source, element_id in sources:
            pixmap = QPixmap()
            try:
                if source.startswith("http"):
                    data = httpx.get(source, timeout=20).content
                elif Path(source).is_file():
                    data = Path(source).read_bytes()
                else:  # a file on the main node: it serves it
                    data = httpx.get(_media_url(element_id), headers=_media_headers(), timeout=20).content
                pixmap.loadFromData(data)
            except Exception:  # noqa: BLE001 - a broken image just doesn't show
                pass
            self.pixmaps[source] = pixmap
        self.images_ready.emit()

    def route(self) -> None:
        active = _foreground_device()
        if self.cards and any(card.isActiveWindow() for card in self.cards.values()):
            active = self.active  # the user is using a card: it stays on its monitor
        self.active = active
        assistant = _assistant_display()
        now = time.time()
        live = [e for e in self.elements if (not e.get("expires_at") or e["expires_at"] > now) and e["kind"] != "point"]
        self.pointing.follow([e for e in self.elements if e["kind"] == "point"
                              and (not e.get("expires_at") or e["expires_at"] > now)])
        for layer in self.layers:
            mine = [e for e in live if self._shown_on(layer, e, active, assistant)]
            if mine != layer.elements:
                layer.show_elements(mine)
        self.place_cards()

    def relayout(self) -> None:
        for layer in self.layers:
            layer.arrange()
        self.place_cards()

    def card_height(self, element_id: str, height: int) -> None:
        if self.heights.get(element_id) != height:
            self.heights[element_id] = height
            for layer in self.layers:
                if element_id in layer.ui_rects:
                    layer.arrange()
            self.place_cards()

    def _shown_on(self, layer: "Layer", element: dict, active: str, assistant: str) -> bool:
        """Where the user dragged a card wins over the monitor the assistant asked for."""
        moved = self.moves.get(element["id"])
        if moved and moved.get("screen") and moved["key"] == _placement_key(element):
            return layer.index == moved["screen"]
        return layer.wants(element, active, assistant)

    def place_cards(self) -> None:
        if QWebEngineView is None or self.paused:
            return
        wanted = {}
        for layer in self.layers:
            for element in layer.elements:
                if element["id"] in layer.ui_rects:
                    wanted[(layer.index, element["id"])] = (element, layer.ui_rects[element["id"]])
        if sys.platform == "win32":
            # A card that moved to another monitor keeps its window (and what plays in it). On
            # Wayland a layer surface belongs to one output, so there it is made anew.
            for key in [k for k in self.cards if k not in wanted]:
                moved_to = next((w for w in wanted if w[1] == key[1] and w not in self.cards), None)
                if moved_to is not None:
                    self.cards[moved_to] = self.cards.pop(key)
        for key in [k for k in self.cards if k not in wanted]:
            self.cards.pop(key).deleteLater()
        for key, (element, rect) in wanted.items():
            card = self.cards.get(key)
            if card is None:
                print(f"{time.strftime('%H:%M:%S')} card window for {element['id']} on monitor {key[0]} at {rect.getRect()}")
                card = self.cards[key] = CardWindow(self.card_height, self)
            card.show_element(element)
            card.set_sized(self.moves.get(element["id"], {}).get("h") is not None)
            card.place(rect)


def _placement_key(element: dict) -> str:
    """What the assistant asked for: the user's own placement holds until this changes."""
    return json.dumps([element.get("position"), element.get("monitor")], sort_keys=True)


def _cursor():
    """The pointer in the same logical desktop coordinates as Qt's screens. On Wayland a
    surface doesn't know where it is, so Hyprland says where the pointer is."""
    from PySide6.QtGui import QCursor
    if sys.platform != "win32":
        at = _hypr("j/cursorpos") or {}
        if "x" in at:
            return QPoint(int(at["x"]), int(at["y"]))
    return QCursor.pos()


def _media_url(element_id: str) -> str:
    return f"{BASE}/api/canvas/media/{element_id}"


def _media_headers() -> dict:
    return _router_auth({"x-ultimate-assistant-token": TOKEN})


def _promote(element: dict) -> dict:
    """A note or picture the user can move or resize becomes an interactive card, which has
    its own window to grab (the painted layer lets every click through), as on the phone."""
    if element["kind"] not in ("text", "markdown", "reminder", "image") or QWebEngineView is None:
        return element
    if not (element.get("draggable") or element.get("resizable")):
        return element
    image = str(element.get("image") or "")
    if image and not image.startswith(("http://", "https://")):
        image = _media_url(element["id"])
    body = '<markdown source={data.body}/>' if element["kind"] == "markdown" else '<text>{data.body}</text>'
    markup = ('{#if data.title}<title size="md">{data.title}</title>{/if}\n'
              '{#if data.image}<image src={data.image} radius="md"/>{/if}\n'
              '{#if data.body}' + body + '{/if}\n')
    data = {"title": element.get("title", ""), "body": element.get("body", ""), "image": image}
    return {**element, "kind": "ui", "ui": markup, "data": data, "promoted_from": element["kind"]}


def _hypr(request: str):
    """One Hyprland IPC request (j/... answers JSON); None without a running Hyprland."""
    import socket
    root = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "hypr"
    instance = root / os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "") / ".socket.sock"
    if not instance.exists():
        live = [p for p in root.glob("*/.socket.sock")] if root.is_dir() else []
        instance = max(live, key=lambda p: p.stat().st_mtime, default=None)
        if instance is None:
            return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(str(instance))
            sock.sendall(request.encode())
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return json.loads(data) if request.startswith("j/") else data.decode()
    except (OSError, ValueError):
        return None


def _hyprland_foreground_monitor() -> str:
    monitors = _hypr("j/monitors") or []
    window = _hypr("j/activewindow") or {}
    for monitor in monitors:
        if window.get("monitor") == monitor.get("id"):
            return monitor.get("name", "")
    return next((m.get("name", "") for m in monitors if m.get("focused")), "")


# LayerShellQt::Window: its settings are Qt properties; only get(QWindow*) needs a native call.
_TOP, _BOTTOM, _LEFT, _RIGHT = 1, 2, 4, 8
_ALL_EDGES = _TOP | _BOTTOM | _LEFT | _RIGHT
_layer_shell_get = None


def _layer_shell(widget: QWidget, screen, *, layer: int, anchors: int, keyboard: int, exclusive_zone: int = 0):
    """Make `widget` a layer-shell surface on `screen` (call before show); returns its settings object."""
    global _layer_shell_get
    if _layer_shell_get is None:
        library = ctypes.CDLL("libLayerShellQtInterface.so.6")
        _layer_shell_get = library._ZN12LayerShellQt6Window3getEP7QWindow
        _layer_shell_get.restype, _layer_shell_get.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
    widget.create()
    handle = widget.windowHandle()
    handle.setScreen(screen)
    shell = shiboken6.wrapInstance(_layer_shell_get(shiboken6.getCppPointer(handle)[0]), QObject)
    for name, value in (("scope", "ultimate-assistant-canvas"), ("layer", layer), ("anchors", anchors),
                        ("keyboardInteractivity", keyboard), ("exclusionZone", exclusive_zone)):
        shell.setProperty(name, value)
    return shell


class CapturePause(QObject):
    """The socket screen captures use to have the overlay step aside (see the module docstring)."""

    HIDDEN_AT_MOST_MS = 5000  # a capture that never says "show" can't hide the canvas for good
    FRAME_MS = 80  # long enough for the compositor to present the cleared frame

    def __init__(self, canvas: Canvas) -> None:
        super().__init__(canvas)
        from PySide6.QtNetwork import QLocalServer
        self.canvas = canvas
        # a Unix socket on Linux; on Windows Qt makes the name a pipe: \\.\pipe\ultimate-assistant-canvas
        path = ("ultimate-assistant-canvas" if sys.platform == "win32" else
                str(Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "ultimate-assistant-canvas.sock"))
        QLocalServer.removeServer(path)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept)
        if not self.server.listen(path):
            print(f"capture pause socket unavailable: {self.server.errorString()}")
        self.restore = QTimer(self)
        self.restore.setSingleShot(True)
        self.restore.timeout.connect(lambda: self.canvas.pause(False))

    def _accept(self) -> None:
        while (connection := self.server.nextPendingConnection()) is not None:
            connection.readyRead.connect(lambda c=connection: self._read(c))
            connection.disconnected.connect(connection.deleteLater)

    def _read(self, connection) -> None:
        while connection.canReadLine():
            command = bytes(connection.readLine()).decode().strip()
            if command == "hide":
                self.canvas.pause(True)
                self.restore.start(self.HIDDEN_AT_MOST_MS)
                QTimer.singleShot(self.FRAME_MS, lambda c=connection: c.write(b"ok\n"))
            elif command == "show":
                self.restore.stop()
                self.canvas.pause(False)
                connection.write(b"ok\n")
            elif command.startswith("teach-"):
                word, _, steps = command[6:].partition(" ")
                self.canvas.teach(word == "on", int(steps) if steps.isdigit() else 0)
                connection.write(b"ok\n")
            elif command.startswith("point-"):
                # answered once the marks are with the main node, so the turn that takes them finds them
                self.canvas.pointing.command(command[6:], lambda c=connection: c.write(b"ok\n"))
            elif command == "theme-reload":
                _load_omarchy_theme()
                for layer in getattr(self.canvas, "layers", []):
                    layer.arrange()
                connection.write(b"ok\n")


def _monitors() -> list[tuple[str, tuple[int, int, int, int]]]:
    """Windows' monitors in its order: (device name, (left, top, right, bottom) in physical pixels)."""
    out = []
    for handle, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        out.append((info["Device"], tuple(info["Monitor"])))
    return out


def screen_device(screen) -> str:
    """The Windows device name of a Qt screen. Qt 6 names screens by monitor model ("LF27T35"),
    so match by position: Qt keeps each screen's native top-left; only its size is DPI-scaled.
    On Wayland Qt already names screens by output ("eDP-1"), as Hyprland does."""
    if sys.platform != "win32":
        return screen.name()
    geometry, ratio = screen.geometry(), screen.devicePixelRatio()
    monitors = _monitors()
    for device, (left, top, _, _) in monitors:
        if (left, top) == (geometry.x(), geometry.y()):
            return device
    for device, (left, top, right, bottom) in monitors:
        if abs(right - left - geometry.width() * ratio) < 2 and abs(bottom - top - geometry.height() * ratio) < 2:
            return device
    return screen.name()


def _ordered_screens():
    """Qt screens in Windows' monitor order (the numbering the other desktop tools use);
    on Hyprland, its monitor ids."""
    if sys.platform != "win32":
        ids = {m.get("name"): m.get("id", 0) for m in _hypr("j/monitors") or []}
        return sorted(QGuiApplication.screens(), key=lambda s: ids.get(s.name(), len(ids)))
    order = [device for device, _ in _monitors()]
    devices = {screen: screen_device(screen) for screen in QGuiApplication.screens()}
    return sorted(devices, key=lambda s: order.index(devices[s]) if devices[s] in order else len(order))


def _log_to_file() -> None:
    """pythonw has no console: keep errors (Python and Qt) in a small log next to the user's settings."""
    folder = _settings_dirs()[1]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "canvas-overlay.log"
    if path.is_file() and path.stat().st_size > 1_000_000:
        path.replace(path.with_suffix(".log.1"))
    log = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    log.write(f"\n--- overlay started {time.strftime('%Y-%m-%d %H:%M:%S')} (WebEngine: {QWebEngineView is not None})\n")

    def qt_message(_kind, _context, message):
        log.write(f"qt: {message}\n")
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(qt_message)


def main() -> int:
    if sys.stderr is None or os.environ.get("ULTIMATE_ASSISTANT_CANVAS_LOG") == "1":
        _log_to_file()
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    if QWebEngineView:
        app.router_auth = RouterAuth()  # kept alive with the app
        QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(app.router_auth)
    app.setQuitOnLastWindowClosed(False)
    canvas = Canvas()  # noqa: F841 - lives for the app
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
