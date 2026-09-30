"""Codex-specific CLI agent hooks."""

from __future__ import annotations

from clink.models import ResolvedCLIClient
from clink.parsers.base import ParserError

from .base import AgentOutput, BaseCLIAgent


class CodexAgent(BaseCLIAgent):
    """Codex CLI agent with JSONL recovery & quota limit detection."""

    def __init__(self, client: ResolvedCLIClient):
        super().__init__(client)

    def _recover_from_error(
        self,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        sanitized_command: list[str],
        duration_seconds: float,
        output_file_content: str | None,
    ) -> AgentOutput | None:
        combined = (stderr + "\n" + stdout).lower()
        if "usage limit" in combined or "rate limit" in combined or "upgrade to pro" in combined:
            from clink.parsers.base import ParsedCLIResponse
            message = (
                "Codex quota/usage limit reached. "
                "Recommendation: Route requests to 'claude' (Claude Code) or 'antigravity' (Antigravity CLI)."
            )
            parsed = ParsedCLIResponse(
                content=message,
                metadata={"quota_exceeded": True, "recommended_fallback": ["claude", "antigravity"]}
            )
            return AgentOutput(
                parsed=parsed,
                sanitized_command=sanitized_command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration_seconds,
                parser_name=self._parser.name,
                output_file_content=output_file_content,
            )

        try:
            parsed = self._parser.parse(stdout, stderr)
        except ParserError:
            return None

        return AgentOutput(
            parsed=parsed,
            sanitized_command=sanitized_command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            parser_name=self._parser.name,
            output_file_content=output_file_content,
        )
