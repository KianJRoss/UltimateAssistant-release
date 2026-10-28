"""Linux voice and text entry points for the existing server-owned assistant.

The node owns one bounded microphone capture and speaker playback. Transcription,
chat/agents, memory and conversation routing stay on the main node. Kokoro speech
uses the installed local model when available, with the main node as fallback. The local
Unix socket is private to this login; it carries commands, never Router keys.
"""
from __future__ import annotations

import argparse
import array
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import uuid
import wave
import time

import httpx

from . import emergency
from .settings import settings

LOG = logging.getLogger(__name__)
MAX_RECORD_SECONDS = 120
VAD_POLL_SECONDS = 0.1
VAD_WARMUP_SECONDS = 0.28
VAD_SPEECH_RMS = 260.0
VAD_SILENCE_RMS = 160.0
VAD_SILENCE_STOP_SECONDS = 1.3
FOLLOWUP_TIMEOUT_SECONDS = 10.0

from .voice_decisions import (
    FAREWELL_MAX_WORDS,
    is_farewell as _is_farewell,
    wants_to_stop as _wants_to_stop,  # compatibility for callers using the backend directly
)


def _main_wants_to_stop(client, base: str, text: str) -> bool:
    """Ask the main backend, whose route setting is shared by the frontends.

    An older/unavailable main node retains the local farewell phrase rule.
    """
    text = str(text or "").strip()
    if not text or len(text.split()) > FAREWELL_MAX_WORDS:
        return False
    try:
        response = client.post(base + "/api/voice/turn-end", json={"text": text}, timeout=8.0)
        response.raise_for_status()
        data = response.json()
        value = data.get("stop") if isinstance(data, dict) else None
        if isinstance(value, bool):
            return value
    except (httpx.HTTPError, ValueError):
        pass
    return _is_farewell(text)


def _pcm_ac_rms(pcm: bytes) -> float:
    """Compute AC RMS (DC-offset removed) on 16-bit signed little-endian mono PCM."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable < 4:
        return 0.0
    samples = array.array("h", pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    n = len(samples)
    mean = sum(samples) / n
    return math.sqrt(sum((s - mean) * (s - mean) for s in samples) / n)


def _wav_has_speech(path: Path) -> bool:
    """Check whether a recorded 16kHz s16le WAV has speech energy after the DMIC warmup."""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    warmup_bytes = 44 + int(VAD_WARMUP_SECONDS * 16000) * 2
    if len(raw) <= warmup_bytes + 1600:
        return False
    body = raw[warmup_bytes:]
    window = 3200  # 100ms
    hits = 0
    for offset in range(0, len(body) - window + 1, window):
        if _pcm_ac_rms(body[offset : offset + window]) >= VAD_SPEECH_RMS:
            hits += 1
            if hits >= 2:
                return True
    return False


def socket_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "ultimate-assistant-desktop.sock"


def state_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "ultimate-assistant-state.json"


def _speech_chunks(text):
    """Preserve every character; start with a short phrase and prefer sentence boundaries."""
    # Similar-sized clips let preparation keep pace with playback; a much
    # larger second clip would introduce a long gap after the first phrase.
    while text:
        limit = 100
        if len(text) <= limit:
            yield text
            return
        boundaries = list(re.finditer(r"[.!?](?:\s+|$)|\n", text[:limit]))
        end = boundaries[-1].end() if boundaries else text.rfind(" ", 0, limit) + 1
        if end <= 0:
            end = limit
        yield text[:end]
        text = text[end:]


def _events(lines):
    """Read complete SSE frames, including multi-line data."""
    event, data = "", []
    for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            yield event, json.loads("\n".join(data))
            event, data = "", []


def _recording_bytes(path: Path) -> bytes:
    """Require usable PCM audio, rather than inferring success from a file header."""
    try:
        with wave.open(str(path), "rb") as recording:
            frames = recording.getnframes()
            if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) != (1, 2, 16000):
                raise ValueError("Unexpected microphone recording format.")
            if not 0 < frames <= (MAX_RECORD_SECONDS + 1) * 16000:
                raise ValueError("Microphone recording is empty or exceeds its duration limit.")
            if len(recording.readframes(frames)) != frames * 2:
                raise ValueError("Microphone recording is incomplete.")
        return path.read_bytes()
    except (OSError, wave.Error, EOFError) as exc:
        raise ValueError("No valid microphone audio was captured. Check the PipeWire input device.") from exc


def _unread_marks(client, base, images=None) -> str:
    """What the user pointed at or showed (teach mode) on this device since its last turn, as context
    for this one."""
    from .canvas import marks_context
    from .node_link import node_name
    from .teach_store import context as teaching_context
    parts = []
    params = {"device": node_name(), "unread": "true", "take": "true"}
    for path, key, describe in (("/api/canvas/marks", "marks", marks_context),
                                ("/api/teach/recordings", "recordings", teaching_context)):
        try:
            response = client.get(base + path, params=params, timeout=10)
            response.raise_for_status()
            records = response.json().get(key, [])
            parts.append(describe(records))
            if images is not None:
                from .visual_input import references
                references(records, "marks" if key == "marks" else "teach", images)
        except (httpx.HTTPError, ValueError):
            LOG.warning("Could not read this device's %s", key, exc_info=True)
    return "\n".join(part for part in parts if part)


def _overlay(command: str, wait: float = 0.0) -> bool:
    """Tell the canvas overlay something on its socket (pointing mode); wait for its "ok" when asked."""
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "ultimate-assistant-canvas.sock"
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(wait or 2)
            connection.connect(str(path))
            connection.sendall(command.encode() + b"\n")
            if wait:
                return connection.makefile("r").readline().strip() == "ok"
            return True
    except OSError:
        return False


class Desktop:
    def __init__(self):
        self._lock = threading.RLock()
        self._state_value, self._error = "idle", ""
        self._capture = self._player = self._timer = self._temp = None
        self._busy = False
        self._cancelled = threading.Event()
        self._conversation_active = False
        self._speech_detected = False
        self._followup_turn = False
        self._barge_in_requested = False
        self._voxtype_recording = False
        self._timings = {}
        self._preferences = {}  # the last voice preferences from the main node
        from .teach import Recorder
        self._teach = Recorder(overlay=_overlay, notify=self._notice,
                               pending=settings.user_data_dir / "teach-pending.json")
        self._conversation_file = settings.user_data_dir / "desktop-conversation.json"
        try:
            self._conversation = str(uuid.UUID(json.loads(self._conversation_file.read_text())["id"]))
        except (OSError, ValueError, KeyError, TypeError):
            self._conversation = str(uuid.uuid4())
        self._publish_state()

    @property
    def _state(self) -> str:
        return self._state_value

    @_state.setter
    def _state(self, value: str) -> None:
        self._state_value = value
        self._publish_state()

    def _publish_state(self) -> None:
        """Write live desktop state for Omarchy's Quickshell bar widget and browser UI."""
        try:
            payload = {
                "state": getattr(self, "_state_value", "idle"),
                "error": getattr(self, "_error", ""),
                "conversation_id": getattr(self, "_conversation", ""),
                "conversation_active": bool(getattr(self, "_conversation_active", False)),
                "voxtype_active": self._voxtype_ready(),
                "timings": dict(getattr(self, "_timings", {}) or {}),
                "updated_at": round(time.time(), 3),
            }
            target = state_path()
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(target)
        except Exception:
            pass

    @staticmethod
    def _voxtype_ready() -> bool:
        """True when Omarchy's voxtype daemon is running on this desktop session."""
        if getattr(subprocess.Popen, "__module__", "") != "subprocess":
            return False
        state = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "voxtype" / "state"
        return bool(shutil.which("voxtype")) and state.is_file()

    def status(self):
        from .node_link import node_name
        with self._lock:
            return {"state": self._state, "error": self._error, "device": node_name(),
                    "conversation_id": self._conversation,
                    "conversation_active": self._conversation_active,
                    "voxtype_active": self._voxtype_ready(),
                    "timings": dict(self._timings),
                    "microphone_available": bool(shutil.which("pw-record")),
                    "speaker_available": bool(shutil.which("pw-play"))}

    def _notice(self, text):
        glyphs = {
            "recording": "󰍬",
            "transcribing": "󰔟",
            "thinking": "󰧑",
            "speaking": "󰕾",
            "error": "󰀦",
        }
        glyph = glyphs.get(self._state, "󰚩")
        urgency = "critical" if self._state == "error" else "low"
        if shutil.which("omarchy-notification-send") and getattr(subprocess.Popen, "__module__", "") == "subprocess":
            launcher = shutil.which("ultimate-assistant") or str(Path.home() / ".local" / "bin" / "ultimate-assistant")
            cmd = [
                "omarchy-notification-send",
                "-r", "8841",
                "-g", glyph,
                "-u", urgency,
                "-t", "4000",
                "Ultimate Assistant",
                text[:300],
            ]
            if Path(launcher).exists():
                cmd.extend(["--exec", launcher, "text"])
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "Ultimate Assistant", "-t", "4000",
                              "Ultimate Assistant", text[:300]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _discard_capture_locked(self):
        if self._timer is not None:
            self._timer.cancel()
        if self._voxtype_recording:
            self._voxtype_recording = False
            try:
                subprocess.run(["voxtype", "record", "cancel"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except Exception:
                pass
        if self._capture is not None:
            if self._capture.poll() is None:
                self._capture.send_signal(signal.SIGINT)
            try:
                self._capture.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._capture.kill()
                self._capture.wait()
            self._capture = None
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None
        self._speech_detected = False
        self._followup_turn = False
        self._state = "idle"
        _overlay("point-voice-cancel")

    def _end_idle_capture(self, expected):
        with self._lock:
            if self._capture is not expected:
                return
            self._conversation_active = False
            self._discard_capture_locked()

    def _start_capture_locked(self, *, vad: bool = False, followup: bool = False):
        if not shutil.which("pw-record"):
            raise ValueError("PipeWire's pw-record is not installed.")
        self._temp = tempfile.TemporaryDirectory(prefix="ultimate-assistant-voice-")
        wav_path = Path(self._temp.name) / "request.wav"
        vox_path = Path(self._temp.name) / "voxtype.txt"
        try:
            self._capture = subprocess.Popen(
                ["pw-record", "--rate=16000", "--channels=1", "--format=s16", str(wav_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self._temp.cleanup()
            self._temp = None
            raise
        self._voxtype_recording = False
        if self._voxtype_ready():
            try:
                res = subprocess.run(
                    ["voxtype", "record", "start", f"--file={vox_path}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
                self._voxtype_recording = (res.returncode == 0)
            except Exception:
                self._voxtype_recording = False
        self._state = "recording"
        self._speech_detected = False
        self._followup_turn = followup
        _overlay("point-voice-on")  # point at things while talking
        self._timer = threading.Timer(MAX_RECORD_SECONDS, self._finish, args=(self._capture,))
        self._timer.daemon = True
        self._timer.start()
        if vad:
            threading.Thread(
                target=self._vad_monitor,
                args=(self._capture, wav_path, followup),
                name="assistant-vad",
                daemon=True,
            ).start()
            if followup:
                self._notice("Listening for your reply… (pause to send, or stay silent to finish)")
            else:
                self._notice("Listening — speak naturally (pauses send automatically; press Super+A while silent to end).")
        else:
            self._notice("Listening. Press the assistant key again to send; maximum two minutes.")

    def _vad_monitor(self, capture, wav_path: Path, followup: bool):
        from .voice_turns import configured_detector
        detector = configured_detector()
        try:
            self._vad_monitor_loop(capture, wav_path, followup, detector)
        finally:
            detector.close()

    def _vad_monitor_loop(self, capture, wav_path: Path, followup: bool, detector):
        started = time.monotonic()
        warmup_bytes = 44 + int(VAD_WARMUP_SECONDS * 16000) * 2
        cursor = warmup_bytes
        noise_floor = 70.0
        speech_Count = 0
        silence_since = None
        last_prediction = 0.0
        while True:
            time.sleep(VAD_POLL_SECONDS)
            with self._lock:
                if self._capture is not capture or capture.poll() is not None:
                    return
            now = time.monotonic()
            try:
                raw = wav_path.read_bytes()
            except OSError:
                # Under mock tests or before pw-record creates the file, wait cleanly.
                continue
            if len(raw) <= cursor + 640:
                if not self._speech_detected and (now - started) >= FOLLOWUP_TIMEOUT_SECONDS:
                    self._end_idle_capture(capture)
                    return
                continue
            chunk = raw[cursor:]
            cursor = len(raw)
            rms = _pcm_ac_rms(chunk)
            speech_threshold = max(VAD_SPEECH_RMS, noise_floor * 3.0)
            silence_threshold = max(VAD_SILENCE_RMS, noise_floor * 1.8)
            detector.append(chunk, rms >= speech_threshold)
            if rms >= speech_threshold:
                speech_Count += 1
                silence_since = None
                if speech_Count >= 2:
                    with self._lock:
                        self._speech_detected = True
            else:
                speech_Count = 0
                if not self._speech_detected:
                    noise_floor = max(40.0, min(220.0, noise_floor * 0.85 + rms * 0.15))
                    if (now - started) >= FOLLOWUP_TIMEOUT_SECONDS:
                        self._end_idle_capture(capture)
                        return
                elif rms < silence_threshold:
                    if silence_since is None:
                        silence_since = now
                    elif (now - silence_since) >= VAD_SILENCE_STOP_SECONDS:
                        # A semantic detector may extend a pause, but never hold the
                        # microphone indefinitely. Retry no more than twice a second.
                        if now - last_prediction < 0.5:
                            continue
                        last_prediction = now
                        complete = detector.complete()
                        # Audio continues arriving during model inference. A new
                        # phrase must not be submitted as part of an old silence.
                        try:
                            newer = wav_path.read_bytes()[cursor:]
                        except OSError:
                            newer = b""
                        if _pcm_ac_rms(newer) >= speech_threshold:
                            silence_since = None
                            continue
                        if complete is False and now - silence_since < 3.0:
                            continue
                        self._finish(capture)
                        return
                else:
                    silence_since = None

    def command(self, action: str, text: str = ""):
        if action == "status":
            return self.status()
        if action == "conversation":
            with self._lock:
                self._conversation = str(uuid.UUID(text.strip()))
                self._conversation_file.parent.mkdir(parents=True, exist_ok=True)
                self._conversation_file.write_text(json.dumps({"id": self._conversation}) + "\n")
                self._publish_state()
                return self.status()
        if action == "teach":
            # Teach mode (teach.py): toggle, or "stop" / "discard" / "start <what it is>".
            return {**self.status(), "teach": self._teach.command(text)}
        if action == "point":
            # Pointing mode on its own: marks wait for this device's next turn (or an agent asking).
            if not _overlay("point-toggle"):
                raise ValueError("The canvas overlay is not running, so there is nothing to point on.")
            return self.status()
        if action == "cancel":
            with self._lock:
                self._conversation_active = False
                self._barge_in_requested = False
                if self._capture is not None:
                    self._discard_capture_locked()
                elif self._state == "speaking":
                    self._cancelled.set()
                    if self._player is not None:
                        self._player.terminate()
                elif self._busy:
                    raise ValueError("The request is already with the assistant; it is still running.")
            return self.status()
        with self._lock:
            if action in {"toggle", "converse", "stop"} and self._capture is not None:
                wav_path = Path(self._temp.name) / "request.wav" if self._temp is not None else None
                if action in {"toggle", "converse"} and self._conversation_active and not self._speech_detected:
                    if wav_path is not None and wav_path.exists() and not _wav_has_speech(wav_path):
                        self._conversation_active = False
                        self._discard_capture_locked()
                        self._notice("Voice conversation ended.")
                        return self.status()
                self._finish(self._capture)
                return self.status()
            if action == "stop":
                raise ValueError("The microphone is not recording.")
            if action not in {"toggle", "converse", "start", "send"}:
                raise ValueError("Expected toggle, converse, start, stop, cancel, send, point, or status.")
            if action in {"toggle", "converse"} and self._state == "speaking":
                # Barge-in during verbal conversation: interrupt current reply and immediately listen.
                self._conversation_active = True
                self._barge_in_requested = True
                self._cancelled.set()
                if self._player is not None:
                    self._player.terminate()
                return self.status()
            if self._busy or self._capture is not None:
                raise ValueError("The assistant is busy. Wait for the reply, or cancel speaker playback.")
            self._error = ""
            self._timings = {}
            self._cancelled.clear()
            self._barge_in_requested = False
            if action == "send":
                if not text.strip() or len(text) > 20000:
                    raise ValueError("Expected a message of 1–20000 characters.")
                self._busy, self._state = True, "thinking"
                threading.Thread(target=self._run, args=(text, None), daemon=True).start()
            else:
                conversational = action in {"toggle", "converse"}
                self._conversation_active = conversational
                self._start_capture_locked(vad=conversational, followup=False)
            return self.status()

    def _finish(self, expected):
        with self._lock:
            if self._capture is not expected:
                return
            self._timer.cancel()
            capture, temp = self._capture, self._temp
            followup = self._followup_turn
            used_voxtype = self._voxtype_recording
            self._voxtype_recording = False
            self._capture = self._temp = None
            self._speech_detected = False
            self._followup_turn = False
            self._busy, self._state = True, "transcribing"
            threading.Thread(target=self._recorded, args=(capture, temp, followup, used_voxtype), daemon=True).start()

    def _recorded(self, capture, temp, followup: bool = False, used_voxtype: bool = False):
        # Marks made while talking reach the main node before the turn that takes them.
        _overlay("point-voice-done", wait=8)
        try:
            interrupted = capture.poll() is None
            if interrupted:
                capture.send_signal(signal.SIGINT)
            try:
                capture.wait(timeout=10)
            except subprocess.TimeoutExpired:
                capture.kill()
                capture.wait()
                raise RuntimeError("Microphone capture did not stop.")
            recording = Path(temp.name) / "request.wav"
            # pw-record can return 1 after an intentional SIGINT while finalizing
            # a perfectly valid WAV. An unsolicited failure still fails closed.
            accepted = (0, -signal.SIGINT, 1) if interrupted else (0,)
            if capture.returncode not in accepted:
                raise RuntimeError(f"Microphone capture exited unexpectedly ({capture.returncode}). Check the PipeWire input device.")
            if (self._conversation_active or followup) and recording.is_file() and recording.stat().st_size > 16044 and not _wav_has_speech(recording):
                if used_voxtype:
                    try:
                        subprocess.run(["voxtype", "record", "cancel"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                    except Exception:
                        pass
                with self._lock:
                    self._conversation_active = False
                    self._state, self._busy = "idle", False
                return
            if used_voxtype:
                stage = time.monotonic()
                try:
                    vox_res = subprocess.run(
                        ["voxtype", "record", "stop", "--wait", "--json", "--timeout", "30"],
                        capture_output=True, text=True, timeout=35,
                    )
                    vox_json = json.loads(vox_res.stdout.strip() or "{}")
                    vox_text = str(vox_json.get("text") or "").strip()
                    if not vox_text:
                        vox_file = Path(temp.name) / "voxtype.txt"
                        if vox_file.is_file():
                            vox_text = vox_file.read_text(encoding="utf-8", errors="ignore").strip()
                    if vox_text and vox_text.upper() not in {"[BLANK_AUDIO]", "(BLANK_AUDIO)", "[SILENCE]"}:
                        self._timing("transcription_seconds", time.monotonic() - stage)
                        if followup:
                            self._run(vox_text, None, followup=True)
                        else:
                            self._run(vox_text, None)
                        return
                    if (vox_res.returncode == 3 or not vox_text) and (self._conversation_active or followup):
                        with self._lock:
                            self._conversation_active = False
                            self._state, self._busy = "idle", False
                        return
                except Exception:
                    LOG.warning("Voxtype local GPU transcription failed; falling back to WAV upload", exc_info=True)
            if followup:
                self._run(None, _recording_bytes(recording), followup=True)
            else:
                self._run(None, _recording_bytes(recording))
        except Exception as exc:
            self._failed(exc)
        finally:
            temp.cleanup()

    def _failed(self, exc):
        LOG.exception("Desktop interaction failed")
        with self._lock:
            self._conversation_active = False
            self._barge_in_requested = False
            self._state, self._error, self._busy = "error", str(exc)[:500], False
        self._notice(self._error)

    def _run(self, text, audio, *, followup: bool = False):
        from .ui_proxy import device_interface
        base = settings.herald_url.rstrip("/") + "/nodes/main/http"
        key = os.environ.get("HERALD_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        started = time.monotonic()
        try:
            with httpx.Client(headers=headers, timeout=httpx.Timeout(30, read=900)) as client:
                try:
                    page = client.get(base + "/", timeout=httpx.Timeout(10, connect=2))
                    page.raise_for_status()
                except httpx.HTTPError as exc:
                    # This is a read-only probe, before submitting any work. A
                    # stalled probe can fall back without risking a duplicate action.
                    if not (emergency.unreachable(exc) or isinstance(exc, httpx.ReadTimeout)):
                        raise
                    self._emergency(client, base, text, audio, started)
                    return
                match = re.search(r'<meta name="app-token" content="([^"]+)"', page.text)
                if not match:
                    raise RuntimeError("The main assistant did not provide its session token.")
                client.headers["x-ultimate-assistant-token"] = match.group(1)
                self._hand_over(client, base)  # what was said while it was away comes before this turn
                if audio is not None:
                    stage = time.monotonic()
                    response = client.post(base + "/api/transcribe", content=audio, headers={"content-type": "audio/wav"})
                    if response.status_code == 422 and (followup or self._conversation_active):
                        with self._lock:
                            self._conversation_active = False
                            self._state, self._busy = "idle", False
                        return
                    response.raise_for_status()
                    text = response.json().get("text", "").strip()
                    if not text:
                        if followup or self._conversation_active:
                            with self._lock:
                                self._conversation_active = False
                                self._state, self._busy = "idle", False
                            return
                        raise RuntimeError("No speech was recognized. Try again or use the text interface.")
                    self._timing("transcription_seconds", time.monotonic() - stage)
                if _main_wants_to_stop(client, base, text):
                    with self._lock:
                        self._conversation_active = False
                with self._lock:
                    self._state = "thinking"
                result = None
                acknowledgement = None
                images = []
                pointed = _unread_marks(client, base, images)
                stage = time.monotonic()
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="assistant-ack") as speech, client.stream("POST", base + "/api/chat", json={
                    "message": text, "conversation_id": self._conversation, "images": images,
                    "interface": device_interface() + "\nVoice invocation from the Linux desktop (back-and-forth verbal conversation); reply for speech. "
                                 "Use the canvas for useful visuals, without opening a chat window."
                                 + ("\n" + pointed if pointed else ""),
                }) as response:
                    response.raise_for_status()
                    for event, data in _events(response.iter_lines()):
                        if event == "conversation":
                            self._conversation = str(uuid.UUID(data["conversation_id"]))
                            self._publish_state()
                        elif event == "ack" and acknowledgement is None and str(data.get("text") or "").strip():
                            self._timing("acknowledgement_seconds", time.monotonic() - stage)
                            acknowledgement = speech.submit(self._speak_ack, client, base, str(data["text"]), started)
                        elif event == "progress" and data.get("label"):
                            label = str(data["label"])
                            if not label.startswith(("Primary model unavailable", "Companion is on it", "Completed")):
                                self._notice(label)
                        elif event == "complete":
                            result = data
                        elif event == "error":
                            raise RuntimeError(data if isinstance(data, str) else str(data))
                if not result or not str(result.get("reply") or "").strip():
                    raise RuntimeError("The connection ended without an assistant reply.")
                self._timing("reply_seconds", time.monotonic() - stage)
                self._conversation = str(uuid.UUID(result.get("conversation_id") or self._conversation))
                self._conversation_file.parent.mkdir(parents=True, exist_ok=True)
                self._conversation_file.write_text(json.dumps({"id": self._conversation}) + "\n")
                self._publish_state()
                emergency.remember(self._conversation, text, str(result["reply"]))
                self._speak(client, base, str(result["reply"]), turn_started=started)
            with self._lock:
                barge_in = self._barge_in_requested
                self._barge_in_requested = False
                speak_enabled = str(self._preferences.get("assistant-speak", "true")).lower() != "false"
                keep_listening = barge_in or (self._conversation_active and speak_enabled and not self._cancelled.is_set())
                self._state, self._busy = "idle", False
                if keep_listening and shutil.which("pw-record"):
                    self._cancelled.clear()
                    try:
                        self._start_capture_locked(vad=True, followup=not barge_in)
                    except Exception:
                        LOG.warning("Could not reopen microphone for follow-up turn", exc_info=True)
                        self._conversation_active = False
                else:
                    self._conversation_active = False
        except Exception as exc:
            self._failed(exc)

    def _emergency(self, client, base, text, audio, started):
        """The main node is unreachable: a CLI on this device answers (emergency.py), spoken here."""
        if audio is not None and not text:
            if shutil.which("voxtype") and getattr(subprocess.Popen, "__module__", "") == "subprocess":
                try:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as wav_tmp:
                        wav_tmp.write(audio)
                        wav_tmp.flush()
                        res = subprocess.run(["voxtype", "-q", "transcribe", wav_tmp.name],
                                             capture_output=True, text=True, timeout=30)
                        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip() and not ln.startswith(("Loading audio", "Audio format", "Processing "))]
                        text = " ".join(lines).strip()
                except Exception:
                    LOG.warning("Offline voxtype transcription failed", exc_info=True)
            if not text:
                raise RuntimeError("The main assistant is unreachable, and speech is transcribed there. Type the "
                                   "request instead (the assistant's text key, or ultimate-assistant send).")
        self._notice("The main assistant is unreachable: answering on this device (emergency mode).")
        exchange = emergency.answer(text, self._conversation, spoken=True)
        self._timing("reply_seconds", time.monotonic() - started)
        self._speak(client, base, exchange["reply"], turn_started=started)
        with self._lock:
            self._state, self._busy = "idle", False

    @staticmethod
    def _hand_over(client, base):
        """Exchanges answered here while the main node was away go to it now that it answers."""
        if emergency.queued():
            def post(conversation_id, messages):
                client.post(f"{base}/api/conversations/{conversation_id}/import",
                            json={"messages": messages}).raise_for_status()
            emergency.flush(post)

    def _speak_ack(self, client, base, text, started):
        """Speak the server's first response without blocking incoming work events."""
        try:
            self._speak(client, base, text, turn_started=started)
        except Exception:
            # An optional first line must never hide the actual result or failure.
            LOG.warning("Could not speak the assistant's early response", exc_info=True)
        finally:
            with self._lock:
                if self._state == "speaking":
                    self._state = "thinking"

    def _timing(self, name, seconds):
        with self._lock:
            self._timings[name] = round(seconds, 3)
        LOG.info("Desktop latency %s=%.3fs", name, seconds)

    def _uses_local_speech(self):
        from . import tts
        try:
            saved = json.loads(settings.user_settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        return saved.get("desktop_speech", "auto") != "server" and tts.providers().get("kokoro", False)

    def _warm_speech(self):
        from . import tts
        if self._uses_local_speech():
            try:
                tts.warm_kokoro()
                LOG.info("Local speech model loaded for the next invocation")
            except Exception:
                LOG.warning("Could not preload local speech; the next request can retry", exc_info=True)

    def _local_speech(self, text, voice, speed):
        from . import tts
        if not self._uses_local_speech():
            return None
        try:
            return tts.kokoro_speech(text, voice, speed)
        except Exception:
            LOG.warning("Local speech failed; using the main node's speech service", exc_info=True)
            return None

    def _speak(self, client, base, text, *, turn_started=None):
        try:
            response = client.get(base + "/api/voice-preferences")
            response.raise_for_status()
            preferences = self._preferences = response.json()
        except httpx.HTTPError as exc:
            if not emergency.unreachable(exc):
                raise
            # The main node is away (emergency mode): the last preferences, spoken on this device.
            preferences = {**self._preferences, "assistant-tts-provider": "kokoro"}
        if str(preferences.get("assistant-speak", "true")).lower() == "false":
            self._notice("Reply saved in your assistant conversation. Speech is turned off.")
            return
        if not shutil.which("pw-play"):
            raise RuntimeError("Reply saved; PipeWire's pw-play is missing.")
        provider = preferences.get("assistant-tts-provider", "kokoro")
        if provider == "device":
            provider = "kokoro"  # Browser speech synthesis has no native PipeWire endpoint.
        voice = preferences.get("assistant-voice-" + provider) or "af_heart"
        with self._lock:
            self._state = "speaking"
        speed = float(preferences.get("assistant-voice-rate", 1))

        def generate(chunk):
            if self._cancelled.is_set():
                return None
            stage = time.monotonic()
            audio = self._local_speech(chunk, voice, speed) if provider == "kokoro" else None
            if audio is not None:
                suffix = ".wav"
            else:
                response = client.post(base + "/api/tts", json={"text": chunk, "provider": provider,
                                                               "voice": voice, "speed": speed})
                response.raise_for_status()
                audio = response.content
                suffix = ".mp3" if "mpeg" in response.headers.get("content-type", "") else ".wav"
            return audio, suffix, time.monotonic() - stage

        chunks = iter(_speech_chunks(text))
        first = next(chunks, None)
        if first is None or self._cancelled.is_set():
            return
        total_generation = 0.0
        with tempfile.TemporaryDirectory(prefix="ultimate-assistant-reply-") as folder, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="assistant-speech-next"
        ) as preparation:
            pending = preparation.submit(generate, first)
            index = 0
            while pending is not None:
                if self._cancelled.is_set():
                    pending.cancel()
                    break
                clip = pending.result()
                if clip is None or self._cancelled.is_set():
                    break
                audio, suffix, generation = clip
                total_generation += generation
                self._timing("speech_generation_seconds", total_generation)
                if index == 0:
                    self._timing("speech_first_chunk_seconds", generation)
                following = next(chunks, None)
                # Only one clip is prepared ahead; inference overlaps current playback.
                pending = preparation.submit(generate, following) if following is not None else None
                path = Path(folder) / ("reply" + suffix)
                path.write_bytes(audio)
                with self._lock:
                    if self._cancelled.is_set():
                        if pending is not None:
                            pending.cancel()
                        break
                    self._player = subprocess.Popen(["pw-play", str(path)], stdin=subprocess.DEVNULL,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    player = self._player
                    if index == 0 and turn_started is not None and "first_audio_seconds" not in self._timings:
                        self._timing("first_audio_seconds", time.monotonic() - turn_started)
                try:
                    code = player.wait(timeout=600)
                    if code and not self._cancelled.is_set():
                        raise RuntimeError("Reply saved, but speaker playback failed.")
                finally:
                    if player.poll() is None:
                        player.kill()
                        player.wait()
                    with self._lock:
                        self._player = None
                index += 1


def start() -> bool:
    """One voice controller in the node process; existing state remains on the server."""
    path = socket_path()
    if path.exists():
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(1)
                probe.connect(str(path))
            return False
        except (ConnectionRefusedError, FileNotFoundError):
            if not path.is_socket():
                raise RuntimeError(f"Refusing to replace {path}")
            path.unlink()
    desktop = Desktop()
    threading.Thread(target=desktop._warm_speech, name="assistant-speech-warmup", daemon=True).start()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            try:
                raw = self.rfile.readline(65537)
                if not raw:
                    return
                if len(raw) > 65536:
                    raise ValueError("Command is too large.")
                request = json.loads(raw)
                result = desktop.command(request.get("action", "status"), str(request.get("text") or ""))
            except Exception as exc:
                result = {"error": str(exc)}
            self.wfile.write(json.dumps(result).encode() + b"\n")

    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    server.daemon_threads = True
    path.chmod(0o600)
    threading.Thread(target=server.serve_forever, name="assistant-desktop", daemon=True).start()
    LOG.info("Desktop voice invocation ready on %s", path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Talk to Ultimate Assistant; no coding interface involved.")
    parser.add_argument("action", nargs="?", default="toggle", choices=("toggle", "converse", "start", "stop", "cancel", "status", "text", "send", "point", "teach"))
    parser.add_argument("message", nargs="?")
    args = parser.parse_args()
    if args.action == "text":
        url = "http://127.0.0.1:" + os.environ.get("ULTIMATE_ASSISTANT_UI_PORT", "8841") + "/"
        launcher = shutil.which("omarchy-launch-or-focus-webapp")
        if launcher:
            return subprocess.call([launcher, "Ultimate Assistant", url])
        import webbrowser
        return 0 if webbrowser.open(url) else 1
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(15)
            client.connect(str(socket_path()))
            client.sendall(json.dumps({"action": args.action, "text": args.message or ""}).encode() + b"\n")
            with client.makefile("r") as stream:
                result = json.loads(stream.readline(65537))
        print(json.dumps(result))
        return 1 if result.get("error") else 0
    except OSError as exc:
        print(f"Ultimate Assistant node is unavailable: {exc}. Start ultimate-assistant-node.service.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
