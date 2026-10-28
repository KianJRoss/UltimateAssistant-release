from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def serve(model: Path, voices: Path) -> None:
    """Own one loaded model for the calling app; no listening network service."""
    import soundfile
    from kokoro_onnx import Kokoro
    engine = Kokoro(str(model), str(voices))
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not request.get("warm"):
                audio, sample_rate = engine.create(request["text"], voice=request["voice"],
                                                   speed=request["speed"], lang="en-us")
                soundfile.write(request["output"], audio, sample_rate)
            result = {"ok": True}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        print(json.dumps(result), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--voices", required=True, type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--voice")
    parser.add_argument("--speed", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.serve:
        serve(args.model, args.voices)
        return
    if args.voice is None or args.speed is None or args.output is None:
        parser.error("--voice, --speed and --output are required without --serve")
    import soundfile
    from kokoro_onnx import Kokoro
    text = sys.stdin.read()
    engine = Kokoro(str(args.model), str(args.voices))
    audio, sample_rate = engine.create(
        text, voice=args.voice, speed=args.speed, lang="en-us"
    )
    soundfile.write(str(args.output), audio, sample_rate)


if __name__ == "__main__":
    main()
