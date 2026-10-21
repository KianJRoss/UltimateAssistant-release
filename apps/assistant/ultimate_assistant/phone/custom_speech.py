"""Custom speech shared by phone and desktop voice clients."""
import os
import json
from pathlib import Path
import httpx
from .. import tts
from ..settings import settings

FORMATS = {"audio/webm": ".webm", "audio/mp4": ".mp4", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav"}
MAX_AUDIO = 25 * 1024 * 1024

def options():
    path = settings.user_settings_file.parent / "voice-preferences.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}
    provider = os.environ.get("UA_SPEECH_PROVIDER") or saved.get("assistant-tts-provider", "kokoro")
    # Device speech is an old browser preference, not a custom speech provider.
    if provider not in {"kokoro", "elevenlabs"}:
        provider = "kokoro"
    default_voice = "af_heart" if provider == "kokoro" else ""
    voice = os.environ.get("UA_SPEECH_VOICE") or saved.get("assistant-voice-" + provider) or default_voice
    try:
        speed = float(os.environ.get("UA_SPEECH_SPEED") or saved.get("assistant-voice-rate", "1"))
        if not __import__("math").isfinite(speed):
            speed = 1.0
    except (TypeError, ValueError):
        speed = 1.0
    return {"provider": provider, "voice": voice, "speed": max(0.5, min(2.0, speed)),
            "available": tts.providers()}

def synthesize(text):
    text = str(text).strip()
    if not text or len(text) > 12000:
        raise ValueError("Speech text must contain 1 to 12000 characters")
    selected = options()
    return tts.synthesize(text, selected["provider"], selected["voice"], selected["speed"])

def transcribe(audio, content_type):
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind not in FORMATS:
        raise ValueError("Unsupported microphone recording format")
    if not audio or len(audio) > MAX_AUDIO:
        raise ValueError("Recording must contain audio and be smaller than 25 MB")
    headers = {}
    key = os.environ.get("HERALD_API_KEY", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    response = httpx.post(settings.herald_url.rstrip("/") + "/voice/transcribe",
        headers=headers, files={"audio": ("recording" + FORMATS[kind], audio, kind)},
        timeout=httpx.Timeout(connect=10, read=240, write=60, pool=10))
    response.raise_for_status()
    text = str(response.json().get("text", "")).strip()
    if not text:
        raise ValueError("No speech recognized")
    return {"text": text}
