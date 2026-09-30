"""Parser for Antigravity CLI (agy) JSON output."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseParser, ParsedCLIResponse, ParserError


class AntigravityJSONParser(BaseParser):
    """Parse stdout produced by `agy -p ... --output-format json`."""

    name = "antigravity_json"

    def parse(self, stdout: str, stderr: str) -> ParsedCLIResponse:
        if not stdout.strip():
            raise ParserError("Antigravity CLI (agy) returned empty stdout while JSON output was expected")

        try:
            payload: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ParserError(f"Failed to decode Antigravity CLI JSON output: {exc}") from exc

        response = payload.get("response")
        response_text = response.strip() if isinstance(response, str) else ""

        metadata: dict[str, Any] = {"raw": payload}

        usage = payload.get("usage")
        if isinstance(usage, dict):
            metadata["token_usage"] = usage

        if "duration_seconds" in payload:
            metadata["duration_seconds"] = payload["duration_seconds"]

        if response_text:
            if stderr and stderr.strip():
                metadata["stderr"] = stderr.strip()
            return ParsedCLIResponse(content=response_text, metadata=metadata)

        if stderr and stderr.strip():
            metadata["stderr"] = stderr.strip()
            return ParsedCLIResponse(content=f"Antigravity CLI returned no output. Stderr: {stderr.strip()}", metadata=metadata)

        raise ParserError("Antigravity CLI response is missing a textual 'response' field")
