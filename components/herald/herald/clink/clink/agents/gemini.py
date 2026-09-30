"""Gemini-specific CLI agent hooks."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Sequence

from clink.models import ResolvedCLIClient, ResolvedCLIRole
from clink.parsers.base import ParsedCLIResponse

from .base import AgentOutput, BaseCLIAgent


class GeminiAgent(BaseCLIAgent):
    """Gemini-specific behaviour."""

    def __init__(self, client: ResolvedCLIClient):
        super().__init__(client)
        self._temp_config_dir: Path | None = None

    def _prepare_mcp_config(self) -> Path | None:
        """Prepare MCP config for Gemini by creating project-level .gemini/settings.json."""
        import logging
        logger = logging.getLogger("clink.agents.gemini")

        if not self.client.mcp_config_path:
            logger.debug("No mcp_config_path set, skipping MCP config setup")
            return None

        # Create temp directory with .gemini/settings.json
        temp_dir = Path(tempfile.mkdtemp(prefix="gemini-clink-"))
        gemini_config_dir = temp_dir / ".gemini"
        gemini_config_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created temp Gemini config dir: {temp_dir}")

        # Read the MCP config (Claude Code format)
        mcp_config_path = Path(self.client.mcp_config_path)
        logger.info(f"Reading MCP config from: {mcp_config_path}")
        if not mcp_config_path.exists():
            logger.error(f"MCP config not found: {mcp_config_path}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None

        with open(mcp_config_path, "r", encoding="utf-8") as f:
            mcp_config = json.load(f)

        # Convert to Gemini settings.json format
        gemini_settings = {
            "mcpServers": mcp_config.get("mcpServers", {})
        }

        # Write to .gemini/settings.json
        settings_path = gemini_config_dir / "settings.json"
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(gemini_settings, f, indent=2)
        logger.info(f"Wrote Gemini settings.json with {len(gemini_settings['mcpServers'])} MCP servers to: {settings_path}")

        self._temp_config_dir = temp_dir
        return temp_dir

    def _cleanup_mcp_config(self) -> None:
        """Clean up temporary MCP config directory."""
        if self._temp_config_dir and self._temp_config_dir.exists():
            shutil.rmtree(self._temp_config_dir, ignore_errors=True)
            self._temp_config_dir = None

    async def run(
        self,
        *,
        role: ResolvedCLIRole,
        prompt: str,
        system_prompt: str | None = None,
        files: Sequence[str],
        images: Sequence[str],
    ) -> AgentOutput:
        """Override run to set up Gemini MCP config via temp directory."""
        import logging
        logger = logging.getLogger("clink.agents.gemini")

        # Prepare MCP config if needed (creates .gemini/settings.json in temp dir)
        temp_dir = self._prepare_mcp_config()
        logger.info(f"Gemini agent run() - temp_dir: {temp_dir}")

        try:
            if temp_dir:
                logger.info(f"Using temp working directory: {temp_dir}")
                # Temporarily override working_dir to use temp directory
                # Create updated client with temp working directory
                temp_client = ResolvedCLIClient(
                    name=self.client.name,
                    executable=self.client.executable,
                    internal_args=self.client.internal_args,
                    config_args=self.client.config_args,
                    env=self.client.env,
                    roles=self.client.roles,
                    runner=self.client.runner,
                    parser=self.client.parser,
                    timeout_seconds=self.client.timeout_seconds,
                    working_dir=temp_dir,
                    output_to_file=self.client.output_to_file,
                    mcp_config_path=self.client.mcp_config_path,
                )

                original_client = self.client
                self.client = temp_client

                try:
                    return await super().run(
                        role=role, prompt=prompt, system_prompt=system_prompt,
                        files=files, images=images
                    )
                finally:
                    self.client = original_client
            else:
                # No MCP config, run normally
                return await super().run(
                    role=role, prompt=prompt, system_prompt=system_prompt,
                    files=files, images=images
                )
        finally:
            # Always cleanup temp directory
            self._cleanup_mcp_config()

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
        combined = "\n".join(part for part in (stderr, stdout) if part)
        if not combined:
            return None

        brace_index = combined.find("{")
        if brace_index == -1:
            return None

        json_candidate = combined[brace_index:]
        try:
            payload: dict[str, Any] = json.loads(json_candidate)
        except json.JSONDecodeError:
            return None

        error_block = payload.get("error")
        if not isinstance(error_block, dict):
            return None

        code = error_block.get("code")
        err_type = error_block.get("type")
        detail_message = error_block.get("message")

        prologue = combined[:brace_index].strip()
        lines: list[str] = []
        if prologue and (not detail_message or prologue not in detail_message):
            lines.append(prologue)
        if detail_message:
            lines.append(detail_message)

        header = "Gemini CLI reported a tool failure"
        if code:
            header = f"{header} ({code})"
        elif err_type:
            header = f"{header} ({err_type})"

        content_lines = [header.rstrip(".") + "."]
        content_lines.extend(lines)
        message = "\n".join(content_lines).strip()

        metadata = {
            "cli_error_recovered": True,
            "cli_error_code": code,
            "cli_error_type": err_type,
            "cli_error_payload": payload,
        }

        parsed = ParsedCLIResponse(content=message or header, metadata=metadata)
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
