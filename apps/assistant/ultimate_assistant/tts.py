from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import json
import queue
import threading
import atexit
from pathlib import Path

import httpx

from .settings import settings, setup_hint


ELEVENLABS_API = "https://api.elevenlabs.io/v1"
ELEVENLABS_VOICE_API = "https://api.elevenlabs.io/v2/voices?page_size=100"


class _KokoroProcess:
    """Serialize speech through one reusable worker in its installed Python environment."""

    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.command = None
        self.responses = None

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def request(self, command: list[str], payload: dict, *, timeout: float = 180):
        with self.lock:
            if self.process is None or self.process.poll() is not None or command != self.command:
                self.close()
                self.command = list(command)
                self.responses = queue.Queue()
                process = self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                          text=True, encoding="utf-8", bufsize=1,
                                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                responses = self.responses

                def read():
                    try:
                        for line in process.stdout:
                            responses.put(line)
                    except (OSError, ValueError):
                        pass  # The owning request closed the process after failure/shutdown.
                    finally:
                        responses.put(None)

                threading.Thread(target=read, name="kokoro-worker-output", daemon=True).start()
            try:
                self.process.stdin.write(json.dumps(payload) + "\n")
                self.process.stdin.flush()
                line = self.responses.get(timeout=timeout)
                if line is None:
                    raise RuntimeError("Kokoro speech worker exited before completing the request.")
                result = json.loads(line)
                if not result.get("ok"):
                    raise RuntimeError("Kokoro speech failed: " + str(result.get("error") or "unknown error"))
            except queue.Empty as exc:
                self.close()
                raise RuntimeError("Kokoro speech worker timed out.") from exc
            except Exception:
                self.close()  # A later request starts cleanly; never accept a late response.
                raise


_kokoro_process = _KokoroProcess()
atexit.register(_kokoro_process.close)


def elevenlabs_key() -> str | None:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    key_path = settings.user_settings_file.parent / "elevenlabs-api-key.txt"
    try:
        return key_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def kokoro_python() -> Path | None:
    configured = os.environ.get("ULTIMATE_ASSISTANT_KOKORO_PYTHON", "").strip()
    executable = Path(configured) if configured else None
    if executable and executable.is_file():
        return executable
    bundled = Path(__file__).resolve().parents[3] / '.venvs' / 'kokoro' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if bundled.is_file():
        return bundled
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
        raise RuntimeError("Add your ElevenLabs API key first: " + ("apps/assistant/configure.ps1" if sys.platform == "win32"
                           else f"ELEVENLABS_API_KEY, or the file {settings.user_settings_file.parent / 'elevenlabs-api-key.txt'}"))
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
        raise RuntimeError(f"Local Kokoro is not installed. Run {setup_hint('setup-kokoro.ps1')}.")
    with tempfile.TemporaryDirectory(prefix="ultimate-assistant-tts-") as temp_dir:
        output_file = Path(temp_dir) / "reply.wav"
        _kokoro_process.request(
            [str(executable), str(worker), "--serve", "--model", str(model_path), "--voices", str(voices_path)],
            {"text": text, "voice": voice, "speed": speed, "output": str(output_file)},
        )
        return output_file.read_bytes()


def warm_kokoro() -> None:
    """Load the speech runtime without synthesizing or playing an utterance."""
    executable = kokoro_python()
    model, voices = kokoro_assets()
    if not executable or not model.is_file() or not voices.is_file():
        return
    worker = Path(__file__).with_name("kokoro_worker.py")
    _kokoro_process.request([str(executable), str(worker), "--serve", "--model", str(model), "--voices", str(voices)],
                            {"warm": True})


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
