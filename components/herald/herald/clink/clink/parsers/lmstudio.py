"""Parser for LMStudio CLI JSON output."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseParser, ParsedCLIResponse, ParserError


class LMStudioJSONParser(BaseParser):
    """Parse stdout produced by lmstudio-cli-enhanced.py."""

    name = "lmstudio_json"

    def parse(self, stdout: str, stderr: str) -> ParsedCLIResponse:
        """Parse LMStudio CLI JSON output.

        Expected format:
        {
          "status": "success",
          "content": "response text",
          "metadata": {...}
        }
        """
        if not stdout.strip():
            raise ParserError("LMStudio CLI returned empty stdout while JSON output was expected")

        try:
            payload: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ParserError(f"Failed to decode LMStudio CLI JSON output: {exc}") from exc

        # Check status
        status = payload.get("status")
        if status == "error":
            error_msg = payload.get("content", "Unknown error")
            raise ParserError(f"LMStudio CLI reported error: {error_msg}")

        # Extract content
        content = payload.get("content")
        if not isinstance(content, str):
            raise ParserError("LMStudio CLI response is missing a 'content' field")

        content_text = content.strip()

        # Extract metadata
        metadata: dict[str, Any] = payload.get("metadata", {})

        # Add stderr if present
        if stderr and stderr.strip():
            metadata["stderr"] = stderr.strip()

        return ParsedCLIResponse(content=content_text, metadata=metadata)
