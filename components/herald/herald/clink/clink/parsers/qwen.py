"""Parser for Qwen CLI text output."""

from __future__ import annotations

from .base import BaseParser, ParsedCLIResponse, ParserError


class QwenTextParser(BaseParser):
    """Parse stdout produced by Qwen CLI (plain text output)."""

    name = "qwen_text"

    def parse(self, stdout: str, stderr: str) -> ParsedCLIResponse:
        """Parse Qwen CLI plain text output.

        Qwen CLI outputs plain text responses directly to stdout.
        """
        if not stdout.strip():
            raise ParserError("Qwen CLI returned empty stdout")

        content = stdout.strip()

        metadata: dict[str, str] = {
            "raw": stdout,
        }

        if stderr and stderr.strip():
            metadata["stderr"] = stderr.strip()

        return ParsedCLIResponse(content=content, metadata=metadata)
