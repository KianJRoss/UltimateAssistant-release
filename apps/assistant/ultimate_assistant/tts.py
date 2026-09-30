from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import httpx


ELEVENLABS_API = "https://api.elevenlabs.io/v1"
ELEVENLABS_VOICE_API = "https://api.elevenlabs.io/v2/voices?page_size=100"


def elevenlabs_key() -> str | None:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    key_path = Path(local_app_data) / "UltimateAssistant" / "elevenlabs-api-key.txt"
    try:
        return key_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def kokoro_python() -> Path | None:
    configured = os.environ.get("ULTIMATE_ASSISTANT_KOKORO_PYTHON", "").strip()
    executable = Path(configured) if configured else None
    if executable and executable.is_file():
        return executable
    return None


def kokoro_assets() -> tuple[Path, Path]:
    asset_dir = Path(__file__).resolve().parents[1] / "models" / "kokoro"
    return asset_dir / "kokoro-v1.0.int8.onnx", asset_dir / "voices-v1.0.bin"


def providers() -> dict[str, bool]:
    model, voices = kokoro_assets()
    return {
        "elevenlabs": bool(elevenlabs_key()),
        "kokoro": kokoro_python() is not None and model.is_file() and voices.is_file(),
    }


def list_elevenlabs_voices() -> list[dict[str, str]]:
    key = elevenlabs_key()
    if not key:
        return []
    response = httpx.get(
        ELEVENLABS_VOICE_API,
        headers={"xi-api-key": key},
        timeout=20,
    )
    response.raise_for_status()
    return [
        {"id": voice["voice_id"], "name": voice.get("name", "Voice")}
        for voice in response.json().get("voices", [])
        if isinstance(voice, dict) and voice.get("voice_id")
    ]


def elevenlabs_speech(text: str, voice_id: str, speed: float) -> bytes:
    key = elevenlabs_key()
    if not key:
        raise RuntimeError("Add your ElevenLabs API key in apps/assistant/configure.ps1 first.")
    response = httpx.post(
        f"{ELEVENLABS_API}/text-to-speech/{voice_id}",
        params={"output_format": "mp3_22050_32"},
        headers={"xi-api-key": key, "Accept": "audio/mpeg"},
        json={
            "text": text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {"stability": 0.42, "similarity_boost": 0.72, "speed": speed},
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.content


def kokoro_speech(text: str, voice: str, speed: float) -> bytes:
    executable = kokoro_python()
    model_path, voices_path = kokoro_assets()
    worker = Path(__file__).with_name("kokoro_worker.py")
    if not executable or not model_path.is_file() or not voices_path.is_file():
        raise RuntimeError("Local Kokoro is not installed. Run setup-kokoro.ps1.")
    with tempfile.TemporaryDirectory(prefix="ultimate-assistant-tts-") as temp_dir:
        output_file = Path(temp_dir) / "reply.wav"
        subprocess.run(
            [
                str(executable), str(worker),
                "--model", str(model_path), "--voices", str(voices_path),
                "--output", str(output_file), "--voice", voice, "--speed", str(speed),
            ],
            input=text,
            text=True,
            check=True,
            capture_output=True,
            timeout=180,
        )
        return output_file.read_bytes()


def synthesize(
    text: str, provider: str, voice: str, speed: float
) -> tuple[bytes, str, str]:
    if provider == "elevenlabs":
        try:
            return elevenlabs_speech(text, voice, speed), "audio/mpeg", "elevenlabs"
        except Exception:
            if not kokoro_python():
                raise
            return kokoro_speech(text, "af_heart", speed), "audio/wav", "kokoro-fallback"
    if provider == "kokoro":
        return kokoro_speech(text, voice, speed), "audio/wav", "kokoro"
    raise ValueError("Unsupported speech provider.")
