"""Claude-specific CLI agent hooks."""

from __future__ import annotations

from clink.models import ResolvedCLIRole
from clink.parsers.base import ParserError

from .base import AgentOutput, BaseCLIAgent


class ClaudeAgent(BaseCLIAgent):
    """Claude CLI agent with system-prompt injection support."""

    def _build_command(self, *, role: ResolvedCLIRole, system_prompt: str | None) -> list[str]:
        command = list(self.client.executable)
        command.extend(self.client.internal_args)
        command.extend(self.client.config_args)

        # Add MCP config if specified (for nested spawning support)
        if self.client.mcp_config_path:
            mcp_flags = self._get_mcp_config_flags()
            command.extend(mcp_flags)

        if system_prompt and "--append-system-prompt" not in self.client.config_args:
            command.extend(["--append-system-prompt", system_prompt])

        command.extend(role.role_args)
        return command

    def _get_mcp_config_flags(self) -> list[str]:
        """Return Claude CLI flags for MCP config."""
        if not self.client.mcp_config_path:
            return []
        return ["--mcp-config", str(self.client.mcp_config_path)]

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
