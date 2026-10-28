"""Optional, local audio turn detectors behind the desktop's silence candidate.

No audio leaves this device. Dependencies are loaded only for an explicitly
selected detector; missing packages/models or inference errors retain the VAD
endpoint. The owner calls this object from one capture-monitor thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

from .settings import settings

LOG = logging.getLogger(__name__)


class _SmartTurn:
    def __init__(self):
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
        self.analyzer = LocalSmartTurnAnalyzerV3(sample_rate=16000)
        self.analyzer.set_sample_rate(16000)

    async def append(self, pcm: bytes, speech: bool) -> None:
        self.analyzer.append_audio(pcm, speech)

    async def complete(self) -> bool:
        from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
        state, _ = await self.analyzer.analyze_end_of_turn()
        return state == EndOfTurnState.COMPLETE

    async def close(self) -> None:
        await self.analyzer.cleanup()


class _LiveKit:
    def __init__(self):
        from livekit.agents import inference
        # Explicit local version: never silently choose hosted inference.
        self.detector = inference.TurnDetector(version="v1-mini", sample_rate=16000)
        self.stream = self.detector.stream()

    async def append(self, pcm: bytes, speech: bool) -> None:
        from livekit import rtc
        self.stream.push_audio(rtc.AudioFrame(
            data=pcm, sample_rate=16000, num_channels=1, samples_per_channel=len(pcm) // 2))
        await asyncio.sleep(0)

    async def complete(self) -> bool:
        event = await self.stream.predict()
        probability = float(event.end_of_turn_probability)
        threshold = await self.stream.unlikely_threshold(None)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("invalid turn probability")
        return probability >= (threshold if threshold is not None else 0.5)

    async def close(self) -> None:
        await self.stream.aclose()


class VoiceTurnDetector:
    """Per-capture detector; ``None`` means use the existing silence endpoint."""

    def __init__(self, provider: str, *, factories: dict[str, Any] | None = None):
        self.provider = provider
        self._factories = factories or {"smart_turn": _SmartTurn, "livekit": _LiveKit}
        self._runner: asyncio.Runner | None = None
        self._detector = None
        self._failed = False

    def _run(self, coroutine):
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner.run(asyncio.wait_for(coroutine, timeout=1.5))

    async def _initialize(self):
        # LiveKit constructs tasks and therefore needs a running event loop.
        self._detector = self._factories[self.provider]()

    def append(self, pcm: bytes, speech: bool) -> None:
        if self.provider == "vad" or self._failed:
            return
        try:
            if self._detector is None:
                self._run(self._initialize())
            usable = pcm[:len(pcm) - len(pcm) % 2]
            if usable:
                self._run(self._detector.append(usable, speech))
        except Exception:
            self._failed = True
            LOG.info("Voice turn detector %s unavailable; using silence detection", self.provider)

    def complete(self) -> bool | None:
        if self._failed or self._detector is None:
            return None
        try:
            value = self._run(self._detector.complete())
            if not isinstance(value, bool):
                raise ValueError("invalid completion result")
            return value
        except Exception:
            self._failed = True
            LOG.info("Voice turn detector %s failed; using silence detection", self.provider)
            return None

    def close(self) -> None:
        try:
            if self._detector is not None:
                self._run(self._detector.close())
        except Exception:
            pass
        finally:
            if self._runner is not None:
                self._runner.close()


def configured_detector() -> VoiceTurnDetector:
    try:
        config = json.loads(settings.user_settings_file.read_text(encoding="utf-8"))
        provider = config.get("voice_turn_detector", "vad") if isinstance(config, dict) else "vad"
    except (OSError, ValueError):
        provider = "vad"
    if provider not in {"vad", "smart_turn", "livekit"}:
        provider = "vad"
    return VoiceTurnDetector(provider)
