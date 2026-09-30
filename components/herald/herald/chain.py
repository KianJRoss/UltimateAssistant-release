"""Named fallback chains — Layer 3 of the herald API.

A Chain is an ordered list of backends tried in sequence. The first
successful response wins. Unlike pool_name (which groups identical-model
keys), chains are explicitly heterogeneous: different models, different
types, different capabilities.

    from herald import Chain
    best = Chain("best", ["antigravity", "claude-cli", "gemini-2.5-flash"])
    router.chat(best, "complex reasoning task")

Chains can be registered in the router for reuse by name:

    router.register_chain(best)
    # other projects can then call them by name:
    herald.chat("task", chain="best")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from herald.client import RouterClient


@dataclass
class Chain:
    """An ordered list of backends to try in sequence.

    Args:
        name:    Unique name for this chain (used to reference it later).
        models:  Backend names in priority order (first = most preferred).
        stop_on: If set, only try backends matching this task type hint.
    """
    name: str
    models: list[str]
    stop_on: str | None = None

    def chat(self, prompt: str, client: "RouterClient") -> str:
        """Execute the chain: try each model in order, return first success."""
        errors = []
        for model in self.models:
            # chat_with_status(), not chat() + a "[error]"-prefix check: a
            # real model response that happened to start with those exact
            # characters would otherwise look identical to a real failure.
            ok, content = client.chat_with_status(prompt, model=model)
            if ok:
                return content
            errors.append(f"{model}: {content}")
        return f"[error] all models in chain '{self.name}' failed:\n" + "\n".join(errors)

    def __iter__(self):
        return iter(self.models)

    def __repr__(self) -> str:
        return f"Chain({self.name!r}, {self.models})"


# ---------------------------------------------------------------------------
# Built-in sensible defaults — these are starting points, projects should
# tune them to their own registered backends.
# ---------------------------------------------------------------------------

CHAIN_BEST = Chain(
    name="best",
    models=["antigravity", "claude-cli", "gemini-2.5-flash"],
)

CHAIN_CODE = Chain(
    name="code",
    models=["claude-cli", "codex-cli", "antigravity"],
    stop_on="code",
)

CHAIN_FAST = Chain(
    name="fast",
    models=["gemini-2.5-flash", "antigravity"],
    stop_on="fast",
)

CHAIN_LOCAL_ONLY = Chain(
    name="local-only",
    models=["lmstudio-qwen3-8b", "ollama-qwen3-8b"],
)

CHAIN_NO_CLI = Chain(
    name="no-cli",
    models=["gemini-2.5-flash"],
)
