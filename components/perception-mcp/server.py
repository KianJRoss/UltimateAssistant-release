from __future__ import annotations

"""
perception-mcp — screen perception tools for agent PC control on the local Windows device.

Capture the live desktop, read it with OCR (RapidOCR, precise text + coordinates),
and reason about it with a vision model (local Ollama Qwen3-VL by default, OpenRouter
fallback). Pairs with win-ui MCP (which supplies the "hands": focus/type/keys/click).

IMPORTANT: screen capture only works from an INTERACTIVE desktop session (local login
or VNC). A pure SSH/service session cannot see the live desktop and will capture black.
"""

import base64
import io
import json
import logging
import os
import sys
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP, Image

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("perception")

MCP_HOST = os.environ.get("PERCEPTION_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("PERCEPTION_MCP_PORT", "8765"))
mcp = FastMCP("perception", host=MCP_HOST, port=MCP_PORT)

CAPTURE_DIR = os.environ.get("PERCEPTION_CAPTURE_DIR", r"C:\AI\perception-mcp\captures")
VISION_MODEL = os.environ.get("PERCEPTION_VISION_MODEL", "qwen2.5vl:3b")
VISION_FALLBACK = os.environ.get("PERCEPTION_VISION_FALLBACK", "qwen3-vl:8b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_VISION_MODEL = os.environ.get("PERCEPTION_OPENROUTER_MODEL",
                                         "qwen/qwen3-vl-8b-instruct")

VISION_MAX_WIDTH = int(os.environ.get("PERCEPTION_VISION_MAX_WIDTH", "3072"))

os.makedirs(CAPTURE_DIR, exist_ok=True)

# Lazy singletons — heavy imports only when first used.
_ocr = None
_mss = None


def _grab(region: Optional[dict] = None, monitor: Optional[int] = None):
    """Return a PIL.Image of every monitor (default), one monitor (1..N), or a
    region {left,top,width,height} in virtual-screen coordinates."""
    global _mss
    import mss
    from PIL import Image
    if _mss is None:
        _mss = mss.mss()
    if region:
        mon = {"left": int(region["left"]), "top": int(region["top"]),
               "width": int(region["width"]), "height": int(region["height"])}
    else:
        # mss.monitors[0] is the whole virtual screen; 1..N are single monitors.
        # Default to all of them: a primary-only capture missed every window on
        # the other screens of a multi-monitor desk.
        index = int(monitor or 0)
        if not 0 <= index < len(_mss.monitors):
            raise ValueError(f"monitor must be 1..{len(_mss.monitors) - 1}, or omitted for all")
        mon = _mss.monitors[index]
    shot = _mss.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, mon


def _save(img) -> str:
    path = os.path.join(CAPTURE_DIR, f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png")
    img.save(path)
    return path


def _get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr


def _looks_blank(img) -> bool:
    """Heuristic: an all-black/near-uniform image likely means a non-interactive session."""
    ex = img.convert("L").getextrema()  # (min, max)
    return ex[1] - ex[0] < 8


@mcp.tool(structured_output=False)
def capture_screen(region: Optional[dict] = None, monitor: Optional[int] = None,
                   include_image: bool = True):
    """Capture the screen (or a region {left,top,width,height}). Saves a PNG on this
    device and returns its path + dimensions, plus the image itself (downscaled) so a
    model on any device can look at it. Requires an interactive desktop session.
    `monitor`: omit for all monitors, or 1..N for one (layout in perception_status)."""
    try:
        img, mon = _grab(region, monitor)
        path = _save(img)
        info = {"ok": True, "path": path, "width": img.width, "height": img.height,
                "region": mon, "blank_warning": _looks_blank(img)}
        if not include_image:
            return info
        if img.width > VISION_MAX_WIDTH:
            img = img.resize((VISION_MAX_WIDTH, round(img.height * VISION_MAX_WIDTH / img.width)))
        buf = io.BytesIO(); img.convert("RGB").save(buf, format="JPEG", quality=80)
        info["image_scale"] = round(img.width / info["width"], 4)
        return [json.dumps(info), Image(data=buf.getvalue(), format="jpeg")]
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def ocr_screen(region: Optional[dict] = None, monitor: Optional[int] = None) -> dict[str, Any]:
    """Read all on-screen text with RapidOCR. Returns items with text, confidence, and
    center pixel coordinates {x,y} — feed those coords to win-ui to click the element.
    `monitor`: omit for all monitors, or 1..N for one (layout in perception_status)."""
    try:
        import numpy as np
        img, mon = _grab(region, monitor)
        result, _ = _get_ocr()(np.array(img))
        items = []
        for box, text, score in (result or []):
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            cx = int(sum(xs) / len(xs)); cy = int(sum(ys) / len(ys))
            ox = int(mon.get("left", 0)); oy = int(mon.get("top", 0))
            items.append({"text": text, "score": round(float(score), 3),
                          "center": {"x": cx + ox, "y": cy + oy}})
        return {"ok": True, "count": len(items), "items": items,
                "blank_warning": _looks_blank(img)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def find_on_screen(query: str, region: Optional[dict] = None,
                   monitor: Optional[int] = None) -> dict[str, Any]:
    """Find on-screen text matching `query` (case-insensitive substring). Returns matches
    ranked by score with click coordinates — use to locate a button/label to click."""
    res = ocr_screen(region, monitor)
    if not res.get("ok"):
        return res
    q = query.lower().strip()
    matches = [it for it in res["items"] if q in it["text"].lower()]
    matches.sort(key=lambda it: it["score"], reverse=True)
    return {"ok": True, "query": query, "match_count": len(matches), "matches": matches}


def _ollama_vision(prompt: str, img_b64: str, model: str) -> dict[str, Any]:
    import requests
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=180, json={
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
    })
    r.raise_for_status()
    return r.json()


@mcp.tool()
def describe_screen(question: Optional[str] = None, region: Optional[dict] = None,
                    model: Optional[str] = None, monitor: Optional[int] = None) -> dict[str, Any]:
    """Ask a vision model about the current screen. `question` defaults to a general
    description. Uses local Ollama Qwen3-VL by default; set model to override.
    `monitor`: omit for all monitors, or 1..N for one (layout in perception_status)."""
    try:
        img, mon = _grab(region, monitor)
        blank = _looks_blank(img)
        if img.width > VISION_MAX_WIDTH:  # several monitors side by side
            img = img.resize((VISION_MAX_WIDTH, round(img.height * VISION_MAX_WIDTH / img.width)))
        buf = io.BytesIO(); img.save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()
        prompt = question or ("Describe what is on this screen: the active app, key UI "
                              "elements, and any notable state or text.")
        use = model or VISION_MODEL
        for candidate in [use, VISION_FALLBACK]:
            try:
                data = _ollama_vision(prompt, img_b64, candidate)
                text = data.get("message", {}).get("content", "")
                return {"ok": True, "model": candidate, "answer": text,
                        "blank_warning": blank}
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                logger.warning("vision model %s failed: %s", candidate, last)
        # OpenRouter fallback
        if OPENROUTER_KEY:
            import requests
            r = requests.post("https://openrouter.ai/api/v1/chat/completions", timeout=120,
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                json={"model": OPENROUTER_VISION_MODEL, "messages": [{"role": "user",
                    "content": [{"type": "text", "text": prompt},
                                {"type": "image_url", "image_url":
                                 {"url": f"data:image/png;base64,{img_b64}"}}]}]})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            return {"ok": True, "model": OPENROUTER_VISION_MODEL, "answer": text}
        return {"ok": False, "error": f"all vision models failed; last={last}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def perception_status() -> dict[str, Any]:
    """Report perception stack health: capture works, OCR loads, vision models reachable."""
    out: dict[str, Any] = {}
    try:
        img, _ = _grab(); out["capture"] = {"ok": True, "size": [img.width, img.height],
                                            "blank_warning": _looks_blank(img)}
        out["monitors"] = [{"monitor": i, **m} for i, m in enumerate(_mss.monitors) if i]
    except Exception as exc:
        out["capture"] = {"ok": False, "error": str(exc)}
    try:
        import requests
        tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10).json()
        out["ollama_models"] = [m["name"] for m in tags.get("models", [])]
    except Exception as exc:
        out["ollama_models"] = f"error: {exc}"
    out["config"] = {"vision_model": VISION_MODEL, "fallback": VISION_FALLBACK,
                     "capture_dir": CAPTURE_DIR}
    return out


if __name__ == "__main__":
    transport = os.environ.get("PERCEPTION_MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("PERCEPTION_MCP_TRANSPORT must be stdio or streamable-http")
    # Import the heavy modules before the stdio reader thread starts. On Windows,
    # first importing numpy while another thread has a pending read on the stdin
    # pipe blocks until that read returns -- every OCR call hung until the client
    # timed out (verified on a Windows desktop: capture_screen answered in 0.1s, ocr_screen
    # exactly at the 90s client timeout).
    import numpy  # noqa: F401
    import PIL.Image  # noqa: F401
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        pass
    mcp.run(transport=transport)
