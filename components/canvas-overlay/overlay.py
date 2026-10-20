"""The assistant's canvas on a Windows PC: draws canvas elements over the user's screens.

One transparent, click-through, always-on-top window per monitor that never
takes focus and (by default) is left out of screenshots and screen capture, so
the assistant's own screen reading still sees the real screen. It follows the
main node's /api/canvas/stream and redraws on every change.

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
import win32api
import win32con
import win32gui
from PySide6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAbstractTextDocumentLayout, QColor, QDesktopServices, QFont, QGuiApplication, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap, QPolygonF, QTextDocument)
from PySide6.QtWidgets import QApplication, QWidget

try:  # interactive cards need Qt WebEngine (PySide6-Addons); everything else works without it
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover - depends on the installed Qt
    QWebEnginePage = QWebEngineView = None

BASE = os.environ.get("ULTIMATE_ASSISTANT_URL", "http://127.0.0.1:8841").rstrip("/")
TOKEN = os.environ.get("ULTIMATE_ASSISTANT_APP_TOKEN", "")
DEVICE = os.environ.get("ULTIMATE_ASSISTANT_CANVAS_DEVICE", "pc")
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
CARD_FILL = QColor("#16181f")  # an interactive card's window: genui.css --ua-card-fill


class Feed(QObject):
    changed = Signal(list)

    def run(self) -> None:
        headers = _router_auth({"x-ultimate-assistant-token": TOKEN, "Accept": "text/event-stream"})
        while True:
            try:
                with httpx.stream("GET", f"{BASE}/api/canvas/stream", params={"device": DEVICE},
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


def _foreground_device() -> str:
    """The monitor (device name) the user is working on: where the foreground window is."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return ""
    try:
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        return win32api.GetMonitorInfo(monitor)["Device"]
    except Exception:  # noqa: BLE001
        return ""


def _adapter(device: str) -> str:
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
    for folder in ("UltimateAssistantPersonal", "UltimateAssistant"):
        try:
            value = json.load(open(Path(os.environ.get("LOCALAPPDATA", "")) / folder / "settings.json",
                                   encoding="utf-8")).get("operator_display")
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
        self.device = screen_device(screen)  # "\\.\DISPLAYn", the name Windows and the other tools use
        self.elements: list[dict] = []
        self.heights: dict[str, int] = {}  # reported content height of each interactive card
        self.placed: list[tuple] = []  # (element, rect, document, image) for cards; drawings have no rect
        self.ui_rects: dict[str, QRect] = {}  # where each interactive card's window goes, in screen coordinates
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
                width = position.get("w") * self.width() if isinstance(position, dict) and position.get("w") else UI_WIDTH
                width = max(260.0, min(width, self.width() - 2 * MARGIN))
                height = min(float(self.heights.get(element["id"], 160)), self.height() - 2 * MARGIN)
                x, y = self._place(position, width, height, stacks)
                origin = self.geometry().topLeft()
                self.ui_rects[element["id"]] = QRect(int(origin.x() + x), int(origin.y() + y), int(width), int(height))
            else:
                self.placed.append(self._measure(element, stacks))
        self.update()

    # -- painting -----------------------------------------------------------------

    def paintEvent(self, _event) -> None:
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
        if ROUTER_KEY and info.requestUrl().toString().startswith(BASE):
            info.setHttpHeader(b"Authorization", f"Bearer {ROUTER_KEY}".encode())


class CardWindow(QWebEngineView if QWebEngineView else object):
    """One interactive card: a frameless always-on-top web view running /api/canvas/ui/card."""

    def __init__(self, on_height) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # appearing never steals focus; a click does
        # Opaque on purpose: a translucent top-level web view often paints nothing on Windows.
        # The card fills the window and Windows 11 rounds its corners.
        self.setPage(CardPage(self))
        self.page().setBackgroundColor(CARD_FILL)
        self.on_height, self.ready, self.pending, self.shown_at = on_height, False, None, None
        self.element_id = ""
        self.closed = ""
        self.loadFinished.connect(self._loaded)
        self.titleChanged.connect(self._title)
        self.load(QUrl(f"{BASE}/api/canvas/ui/card?embedded=1#token={TOKEN}"))

    def show_element(self, element: dict) -> None:
        self.element_id = element["id"]
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

    def rebuild(self) -> None:
        for layer in self.layers:
            layer.close()
        self.layers = [Layer(screen, i + 1, self.pixmaps) for i, screen in enumerate(_ordered_screens())]
        for layer in self.layers:
            layer.heights = self.heights
        self.route()

    def receive(self, elements: list) -> None:
        if elements != self.elements:
            print(f"{time.strftime('%H:%M:%S')} canvas: {', '.join(e['kind'] + ':' + e['id'] for e in elements) or 'empty'}")
        self.elements = elements
        missing = [e["image"] for e in elements if e["kind"] == "image" and e.get("image") not in self.pixmaps]
        if missing:
            threading.Thread(target=self._load, args=(missing,), daemon=True).start()
        self.route()

    def _load(self, sources: list[str]) -> None:
        for source in sources:
            pixmap = QPixmap()
            try:
                data = httpx.get(source, timeout=20).content if source.startswith("http") else Path(source).read_bytes()
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
        live = [e for e in self.elements if not e.get("expires_at") or e["expires_at"] > now]
        for layer in self.layers:
            mine = [e for e in live if layer.wants(e, active, assistant)]
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

    def place_cards(self) -> None:
        if QWebEngineView is None:
            return
        wanted = {}
        for layer in self.layers:
            for element in layer.elements:
                if element["id"] in layer.ui_rects:
                    wanted[(layer.index, element["id"])] = (element, layer.ui_rects[element["id"]])
        for key in [k for k in self.cards if k not in wanted]:
            self.cards.pop(key).deleteLater()
        for key, (element, rect) in wanted.items():
            card = self.cards.get(key)
            if card is None:
                print(f"{time.strftime('%H:%M:%S')} card window for {element['id']} on monitor {key[0]} at {rect.getRect()}")
                card = self.cards[key] = CardWindow(self.card_height)
            card.show_element(element)
            card.place(rect)


def _monitors() -> list[tuple[str, tuple[int, int, int, int]]]:
    """Windows' monitors in its order: (device name, (left, top, right, bottom) in physical pixels)."""
    out = []
    for handle, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        out.append((info["Device"], tuple(info["Monitor"])))
    return out


def screen_device(screen) -> str:
    """The Windows device name of a Qt screen. Qt 6 names screens by monitor model ("LF27T35"),
    so match by position: Qt keeps each screen's native top-left; only its size is DPI-scaled."""
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
    """Qt screens in Windows' monitor order (the numbering the other desktop tools use)."""
    order = [device for device, _ in _monitors()]
    devices = {screen: screen_device(screen) for screen in QGuiApplication.screens()}
    return sorted(devices, key=lambda s: order.index(devices[s]) if devices[s] in order else len(order))


def _log_to_file() -> None:
    """pythonw has no console: keep errors (Python and Qt) in a small log next to the user's settings."""
    folder = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "UltimateAssistant"
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
    if QWebEngineView and ROUTER_KEY:
        app.router_auth = RouterAuth()  # kept alive with the app
        QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(app.router_auth)
    app.setQuitOnLastWindowClosed(False)
    canvas = Canvas()  # noqa: F841 - lives for the app
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
