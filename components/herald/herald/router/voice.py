"""Multi-engine speech I/O for Herald's voice surfaces (CLI `herald talk` and
the mobile web app).

Speech-to-text runs every engine that's actually available on the current
machine and returns every candidate transcript rather than picking one --
a single ASR engine mishearing a word is common enough that the caller
should reconcile candidates with an LLM (see server.py's /voice/transcribe,
or cli/main.py's `herald talk`) instead of trusting whichever engine
happened to run first.

Engines:
  - faster-whisper: local, offline, GPU-accelerated where CUDA is present,
    falls back to CPU. Works on any OS. This is the primary engine.
  - Windows SAPI (System.Speech.Recognition): built into Windows, zero
    extra dependencies, weaker accuracy than Whisper but a genuinely
    independent second opinion since it uses a different acoustic model.
  - Browser Web Speech API: not implemented here -- it runs client-side in
    the mobile page and is passed in as a third candidate string.

Text-to-speech uses Windows SAPI (System.Speech.Synthesis) for the CLI.
The mobile page uses the browser's own speechSynthesis instead of a
server round-trip.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

_whisper_model = None
_whisper_lock = threading.Lock()

WHISPER_MODEL_SIZE = os.environ.get("HERALD_WHISPER_MODEL", "base")


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _get_whisper_model():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            # device="auto" picks CUDA when available (e.g. the RTX 4070 Ti
            # on a compatible device) and silently falls back to CPU everywhere else.
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="auto", compute_type="default")
        return _whisper_model


def transcribe_whisper(audio_path: str | Path) -> dict[str, Any]:
    """Transcribe an audio file with local Whisper. Returns {"engine",
    "text", "confidence"} or {"engine", "error"} if unavailable/failed."""
    if not whisper_available():
        return {"engine": "whisper", "error": "faster-whisper is not installed"}
    try:
        model = _get_whisper_model()
        segments, info = model.transcribe(str(audio_path), beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return {"engine": "whisper", "text": text, "confidence": info.language_probability}
    except Exception as exc:  # noqa: BLE001
        return {"engine": "whisper", "error": str(exc)}


def windows_sapi_available() -> bool:
    return os.name == "nt"


def transcribe_windows_sapi(audio_path: str | Path, *, timeout: float = 30.0) -> dict[str, Any]:
    """Transcribe a WAV file with Windows' built-in speech recognizer, a
    second, independently-trained engine to cross-check Whisper against."""
    if not windows_sapi_available():
        return {"engine": "windows_sapi", "error": "not running on Windows"}
    script = f"""
Add-Type -AssemblyName System.Speech
$rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine
$rec.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$rec.SetInputToWaveFile('{Path(audio_path).resolve()}')
$result = $rec.Recognize()
if ($result) {{ Write-Output $result.Text }}
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        text = proc.stdout.strip()
        if not text:
            return {"engine": "windows_sapi", "error": proc.stderr.strip() or "no speech recognized"}
        return {"engine": "windows_sapi", "text": text}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"engine": "windows_sapi", "error": str(exc)}


def transcribe_all(audio_path: str | Path, *, extra_candidates: list[str] | None = None) -> list[dict[str, Any]]:
    """Run every locally-available STT engine on the same audio file and
    return every result (successes and failures) so the caller can see what
    was tried, not just the winner."""
    results = [transcribe_whisper(audio_path)]
    if windows_sapi_available():
        results.append(transcribe_windows_sapi(audio_path))
    for candidate in extra_candidates or []:
        if candidate and candidate.strip():
            results.append({"engine": "browser", "text": candidate.strip()})
    return results


def speak_windows(text: str, *, rate: int = 0, timeout: float = 60.0) -> bool:
    """Speak text aloud synchronously via Windows SAPI. Returns False (and
    stays silent, no exception) on any non-Windows host or failure -- voice
    output is a nicety, never something that should crash a chat loop."""
    if not windows_sapi_available() or not text.strip():
        return False
    escaped = text.replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = {rate}
$synth.Speak('{escaped}')
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def record_microphone(*, samplerate: int = 16000, max_seconds: float = 30.0) -> str:
    """Record from the default microphone until Enter is pressed, or
    max_seconds elapses, and return the path to a temp WAV file. Requires
    the optional `sounddevice` + `numpy` dependencies."""
    import numpy as np
    import sounddevice as sd
    import wave

    frames: list[np.ndarray] = []
    stop_event = threading.Event()

    def callback(indata, frame_count, time_info, status) -> None:  # noqa: ANN001
        frames.append(indata.copy())

    def wait_for_enter() -> None:
        input()
        stop_event.set()

    listener = threading.Thread(target=wait_for_enter, daemon=True)
    listener.start()
    with sd.InputStream(samplerate=samplerate, channels=1, dtype="int16", callback=callback):
        stop_event.wait(timeout=max_seconds)

    audio = np.concatenate(frames, axis=0) if frames else np.zeros((0, 1), dtype="int16")
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="herald-talk-")
    os.close(fd)
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(samplerate)
        wav_file.writeframes(audio.tobytes())
    return path


def microphone_available() -> bool:
    try:
        import sounddevice  # noqa: F401
        return True
    except ImportError:
        return False
