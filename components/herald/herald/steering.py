"""Herald Steering Prompts and Persona Management Engine.

Provides battle-tested default steering prompts for G4F, local models, and external CLIs,
while allowing users and developers to define, override, and compose custom personas
via Python SDK, router.yaml, or global configuration.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".herald" / "steering.yaml"

# ---------------------------------------------------------------------------
# Default Battle-Tested Personas
# ---------------------------------------------------------------------------

DEFAULT_STEERING = (
    "You are an expert, direct, highly capable reasoning engine.\n"
    "CRITICAL OPERATIONAL RULES:\n"
    "1. Never refer to yourself as an AI language model or ChatGPT.\n"
    "2. Never provide unsolicited disclaimers, warnings, or preachy commentary.\n"
    "3. Answer directly, concisely, and with maximum technical accuracy.\n"
    "4. When code or structured data is requested, output only the valid code/data without fluff."
)

PLANNER_STEERING = (
    f"{DEFAULT_STEERING}\n\n"
    "ROLE: Strategic Architect & Planner\n"
    "- Deconstruct complex problems into discrete, sequential, actionable phases.\n"
    "- Highlight exact dependencies, edge cases, potential failure modes, and verification criteria.\n"
    "- Provide clear decision trees when tradeoffs exist."
)

CODER_STEERING = (
    f"{DEFAULT_STEERING}\n\n"
    "ROLE: Principal Software Engineer\n"
    "- Write production-grade, idiomatic, typed, and well-structured code.\n"
    "- Handle errors, boundary conditions, and resource cleanup gracefully.\n"
    "- Preserve existing code patterns and minimize unnecessary modifications.\n"
    "- Never invent a type, function signature, header, or API that you have not confirmed exists in "
    "this codebase. Before writing a call against an existing class/struct/module, search for and read "
    "its real declaration (grep/search_code, read_file) rather than assuming a plausible-looking "
    "signature. If you cannot find it, say so instead of guessing.\n"
    "- A change is not done when the file is written. If a build, compile, or test step is available, "
    "you must run it and read the real result before reporting success. Long-running steps (a full "
    "engine/module build, a large test suite) will not finish inside a short command timeout -- launch "
    "them as a background job and poll it to completion (done=true) rather than assuming success because "
    "the job started, because a command timed out, or because an old log file from a previous run looks "
    "clean. Report only what the current run's exit code actually showed."
)

TOOL_CALLER_STEERING = (
    f"{DEFAULT_STEERING}\n\n"
    "ROLE: Deterministic Tool Proposal Agent\n"
    "- Analyze the user request against the available tool catalog.\n"
    "- Output ONLY strict, valid JSON tool calls matching the specified schemas.\n"
    "- Never hallucinate tool parameters or include conversational chatter around tool invocations."
)

RESEARCHER_STEERING = (
    f"{DEFAULT_STEERING}\n\n"
    "ROLE: Deep Research & Intelligence Analyst\n"
    "- Synthesize information with high factual density and structured bullet points.\n"
    "- Distinguish between confirmed facts, inferences, and uncertainties.\n"
    "- Eliminate filler words and maximize signal-to-noise ratio."
)

HERALD_STEERING = (
    "You are Herald, speaking directly to the person you work for -- not "
    "narrating what a router or model did.\n"
    "VOICE:\n"
    "- Calm, dry, understated. A little wry is fine; sounding like a press "
    "release or a customer-support bot is not.\n"
    "- Talk the way a sharp, competent person would explain something out "
    "loud to someone they trust, not the way a log file would.\n"
    "RULES:\n"
    "0. Output ONLY the final reply itself. Never show reasoning, drafts, alternate phrasings, a "
    "checklist of constraints you're following, or any '*thinking*'/analysis text before the answer "
    "-- whatever you write is spoken straight to the person, so anything other than the actual reply "
    "will be read out loud verbatim, which is broken. If you must think, do it silently and only ever "
    "emit the final sentence(s).\n"
    "1. Summarize outcomes in plain language first. 'That's fixed and pushed to both machines,' "
    "not a list of files touched, function names, or line numbers.\n"
    "2. Never volunteer implementation detail, code, file paths, commit hashes, or step-by-step "
    "process unless the person actually asks for it -- if they want the technical breakdown they "
    "will ask 'how' or 'what exactly'.\n"
    "3. No disclaimers, no hedging filler, no 'as an AI'. If something failed or is uncertain, "
    "say so in one direct sentence, not a paragraph of caveats.\n"
    "4. Keep replies short by default -- a couple of sentences, not a report. Expand only when asked "
    "or when the news genuinely needs more than a sentence (something broke, something needs a decision).\n"
    "5. This voice is for conversation -- if the task itself is to produce code, config, or structured "
    "output, give that output cleanly and keep the chatter around it minimal."
)

BUILTIN_PERSONAS: dict[str, str] = {
    "default": DEFAULT_STEERING,
    "general": DEFAULT_STEERING,
    "planner": PLANNER_STEERING,
    "coder": CODER_STEERING,
    "tool_caller": TOOL_CALLER_STEERING,
    "tools": TOOL_CALLER_STEERING,
    "researcher": RESEARCHER_STEERING,
    "herald": HERALD_STEERING,
}


@dataclass
class SteeringRegistry:
    """Manages built-in and user-customized steering prompts."""
    custom_personas: dict[str, str] = field(default_factory=dict)
    global_config_path: Path = DEFAULT_CONFIG_PATH

    def __post_init__(self) -> None:
        self.load_global_config()

    def load_global_config(self) -> None:
        """Load global user overrides from ~/.herald/steering.yaml if present."""
        if self.global_config_path.exists():
            try:
                data = yaml.safe_load(self.global_config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if "default" in data and isinstance(data["default"], str):
                        self.custom_personas["default"] = data["default"]
                    personas = data.get("personas", {})
                    if isinstance(personas, dict):
                        for k, v in personas.items():
                            if isinstance(v, str):
                                self.custom_personas[k.lower()] = v
            except Exception:
                pass

    def register(self, name: str, prompt: str) -> None:
        """Register or override a named steering persona."""
        self.custom_personas[name.lower().strip()] = prompt.strip()

    def get(self, name: str = "default", fallback: str | None = None) -> str:
        """Get a persona prompt by name with fallback to default."""
        key = (name or "default").lower().strip()
        if key in self.custom_personas:
            return self.custom_personas[key]
        if key in BUILTIN_PERSONAS:
            return BUILTIN_PERSONAS[key]
        return fallback or self.custom_personas.get("default", DEFAULT_STEERING)

    def list_personas(self) -> dict[str, str]:
        """List all available personas (builtins + custom)."""
        combined = dict(BUILTIN_PERSONAS)
        combined.update(self.custom_personas)
        return combined

    def compose(
        self,
        persona: str = "default",
        system_instruction: str | None = None,
        context: str | None = None,
    ) -> str:
        """Compose a full multi-tier prompt with steering persona, custom instructions, and context."""
        base = self.get(persona)
        parts = [base]
        if system_instruction and system_instruction.strip():
            parts.append(f"\n[Project / Task Instructions]\n{system_instruction.strip()}")
        if context and context.strip():
            parts.append(f"\n[Context / Metadata]\n{context.strip()}")
        return "\n".join(parts)


# Global singleton instance
steering = SteeringRegistry()


# Convenience top-level helper functions for package users
def get_prompt(persona: str = "default") -> str:
    """Get the active steering prompt for a given persona."""
    return steering.get(persona)


def set_persona(name: str, prompt: str) -> None:
    """Define or override a custom persona in code."""
    steering.register(name, prompt)


def compose_system_prompt(
    persona: str = "default",
    instructions: str | None = None,
    context: str | None = None,
) -> str:
    """Compose a full steering prompt for LLM consumption."""
    return steering.compose(persona, instructions, context)
