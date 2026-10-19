"""
activity_logger.py — CHANGE-GATED screen activity sampler for the companion.

Cheap sampler → heavy escalation: every run it takes a screenshot and compares a tiny
64x64 grayscale signature to the previous sample. If the screen hasn't meaningfully
changed, it does nothing (no vision model). Only when the screen changed does it spend
the GPU to have Qwen3-VL label the activity. Local, and light when you're idle.
"""

import base64
import datetime
import io
import json
import os

import mss
import numpy as np
import requests
from PIL import Image

LOG = r"C:\AI\perception-mcp\activity-log.jsonl"
SIG = r"C:\AI\perception-mcp\last-frame.npy"
MODE_STATE = r"C:\AI\satellite-bridge\assistant-mode-state.json"
LIGHT_MODES = {"gaming", "away", "private"}
OLLAMA = "http://localhost:11434"
MODEL = "qwen2.5vl:3b"   # frugal vision (~3GB); qwen3-vl:8b kept for on-demand high quality
CHANGE_THRESHOLD = 6.0   # mean abs grayscale diff (0-255); higher = less sensitive
_ocr = None


def active_mode_is_light() -> bool:
    """VRAM-light profile? Read the satellite-bridge mode (single source of truth);
    gaming/away/private → avoid the GPU vision model so the game keeps the VRAM."""
    try:
        d = json.load(open(MODE_STATE, encoding="utf-8-sig"))
        return str(d.get("mode", "")).lower() in LIGHT_MODES
    except Exception:
        return False


def ocr_label(img) -> str:
    """Zero-VRAM path: read the most prominent on-screen text with RapidOCR (CPU)."""
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    res, _ = _ocr(np.array(img))
    texts = [t for _b, t, s in (res or []) if s and s > 0.6]
    return "On-screen text: " + " | ".join(texts[:6]) if texts else "No readable text"
PROMPT = (
    "This is a screenshot of a Windows desktop. In ONE short line, label the user's "
    "primary current activity and app, like 'Gaming - Rocket League', 'Coding in VS Code', "
    "'Watching YouTube', 'Reading email', 'Idle desktop'. Reply with only the label."
)


def grab():
    with mss.mss() as s:
        shot = s.grab(s.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    lo, hi = img.convert("L").getextrema()
    return img, (hi - lo) < 8


def signature(img) -> np.ndarray:
    return np.asarray(img.convert("L").resize((64, 64)), dtype=np.float32)


def vision_label(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    r = requests.post(f"{OLLAMA}/api/chat", timeout=120,
                      json={"model": MODEL, "stream": False,
                            "messages": [{"role": "user", "content": PROMPT, "images": [b64]}]})
    r.raise_for_status()
    return r.json().get("message", {}).get("content", "").strip()


def main():
    entry = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
    try:
        img, blank = grab()
        sig = signature(img)
        prev = np.load(SIG) if os.path.exists(SIG) else None
        np.save(SIG, sig)
        changed = prev is None or float(np.mean(np.abs(sig - prev))) > CHANGE_THRESHOLD

        if blank:
            return  # no interactive desktop; log nothing
        if not changed:
            return  # cheap path: screen unchanged, skip heavy processing entirely
        if active_mode_is_light():
            entry["activity"] = ocr_label(img)   # gaming/light: zero-VRAM OCR
            entry["profile"] = "light-ocr"
        else:
            entry["activity"] = vision_label(img)  # full: GPU vision model
            entry["profile"] = "vision"
        entry["changed"] = True
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    print(json.dumps(entry))


if __name__ == "__main__":
    main()
