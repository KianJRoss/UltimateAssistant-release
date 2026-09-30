from __future__ import annotations

import argparse
import sys
from pathlib import Path

import soundfile
from kokoro_onnx import Kokoro


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--voices", required=True, type=Path)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--speed", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    text = sys.stdin.read()
    engine = Kokoro(str(args.model), str(args.voices))
    audio, sample_rate = engine.create(
        text, voice=args.voice, speed=args.speed, lang="en-us"
    )
    soundfile.write(str(args.output), audio, sample_rate)


if __name__ == "__main__":
    main()
