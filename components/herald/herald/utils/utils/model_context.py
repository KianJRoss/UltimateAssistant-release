"""
Model context management for dynamic token allocation.

This module provides a clean abstraction for model-specific token management,
ensuring that token limits are properly calculated based on the current model
being used, not global constants.

CONVERSATION MEMORY INTEGRATION:
This module works closely with the conversation memory system to provide
optimal token allocation for multi-turn conversations:

1. DUAL PRIORITIZATION STRATEGY SUPPORT:
   - Provides separate token budgets for conversation history vs. files
   - Enables the conversation memory system to apply newest-first prioritization
   - Ensures optimal balance between context preservation and new content

2. MODEL-SPECIFIC ALLOCATION:
   - Dynamic allocation based on model capabilities (context window size)
   - Conservative allocation for smaller models (O3: 200K context)
   - Generous allocation for larger models (Gemini: 1M+ context)
   - Adapts token distribution ratios based on model capacity

3. CROSS-TOOL CONSISTENCY:
   - Provides consistent token budgets across different tools
   - Enables seamless conversation continuation between tools
   - Supports conversation reconstruction with proper budget management
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

from config import DEFAULT_MODEL
from herald.providers.providers import ModelCapabilities, ModelProviderRegistry

logger = logging.getLogger(__name__)

try:
    import tiktoken
except ImportError:  # pragma: no cover - tiktoken is an optional accelerant
    tiktoken = None

_TOKENIZER_CACHE: dict[str, Any] = {}


def _get_tokenizer_for_model(model_name: str):
    """Return a cached tiktoken encoding matched to the model family, or None.

    OpenAI models get their exact registered encoding. Every other known
    family (Claude, Gemini, local/open models) uses cl100k_base as the closest
    available approximation - still far more accurate than a flat char ratio.
    Unknown/unrecognized names fall back to cl100k_base too, since it's a
    reasonable default for modern BPE-tokenized models.
    """
    if tiktoken is None:
        return None

    name = (model_name or "").lower()
    cache_key = name
    if cache_key in _TOKENIZER_CACHE:
        return _TOKENIZER_CACHE[cache_key]

    encoding = None
    try:
        if any(marker in name for marker in ("gpt-", "o1", "o3", "o4", "openai", "chatgpt")):
            try:
                encoding = tiktoken.encoding_for_model(model_name)
            except KeyError:
                encoding = tiktoken.get_encoding("cl100k_base")
        else:
            # claude/anthropic, gemini, local/gguf, and unrecognized families:
            # no public tiktoken-compatible tokenizer, so use cl100k_base as
            # the best available approximation rather than crashing.
            encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 - tokenizer setup must never break callers
        encoding = None

    _TOKENIZER_CACHE[cache_key] = encoding
    return encoding


@dataclass
class TokenAllocation:
    """Token allocation strategy for a model."""

    total_tokens: int
    content_tokens: int
    response_tokens: int
    file_tokens: int
    history_tokens: int

    @property
    def available_for_prompt(self) -> int:
        """Tokens available for the actual prompt after allocations."""
        return self.content_tokens - self.file_tokens - self.history_tokens


class ModelContext:
    """
    Encapsulates model-specific information and token calculations.

    This class provides a single source of truth for all model-related
    token calculations, ensuring consistency across the system.
    """

    def __init__(self, model_name: str, model_option: Optional[str] = None):
        self.model_name = model_name
        self.model_option = model_option  # Store optional model option (e.g., "for", "against", etc.)
        self._provider = None
        self._capabilities = None
        self._token_allocation = None

    @property
    def provider(self):
        """Get the model provider lazily."""
        if self._provider is None:
            self._provider = ModelProviderRegistry.get_provider_for_model(self.model_name)
            if not self._provider:
                available_models = ModelProviderRegistry.get_available_model_names()
                if available_models:
                    available_text = ", ".join(available_models)
                else:
                    available_text = (
                        "No models detected. Configure provider credentials or set DEFAULT_MODEL to a valid option."
                    )

                raise ValueError(
                    f"Model '{self.model_name}' is not available with current API keys. Available models: {available_text}."
                )
        return self._provider

    @property
    def capabilities(self) -> ModelCapabilities:
        """Get model capabilities lazily."""
        if self._capabilities is None:
            self._capabilities = self.provider.get_capabilities(self.model_name)
        return self._capabilities

    def calculate_token_allocation(self, reserved_for_response: Optional[int] = None) -> TokenAllocation:
        """
        Calculate token allocation based on model capacity and conversation requirements.

        This method implements the core token budget calculation that supports the
        dual prioritization strategy used in conversation memory and file processing:

        TOKEN ALLOCATION STRATEGY:
        1. CONTENT vs RESPONSE SPLIT:
           - Smaller models (< 300K): 60% content, 40% response (conservative)
           - Larger models (≥ 300K): 80% content, 20% response (generous)

        2. CONTENT SUB-ALLOCATION:
           - File tokens: 30-40% of content budget for newest file versions
           - History tokens: 40-50% of content budget for conversation context
           - Remaining: Available for tool-specific prompt content

        3. CONVERSATION MEMORY INTEGRATION:
           - History allocation enables conversation reconstruction in reconstruct_thread_context()
           - File allocation supports newest-first file prioritization in tools
           - Remaining budget passed to tools via _remaining_tokens parameter

        Args:
            reserved_for_response: Override response token reservation

        Returns:
            TokenAllocation with calculated budgets for dual prioritization strategy
        """
        total_tokens = self.capabilities.context_window

        # Dynamic allocation based on model capacity
        if total_tokens < 300_000:
            # Smaller context models (O3): Conservative allocation
            content_ratio = 0.6  # 60% for content
            response_ratio = 0.4  # 40% for response
            file_ratio = 0.3  # 30% of content for files
            history_ratio = 0.5  # 50% of content for history
        else:
            # Larger context models (Gemini): More generous allocation
            content_ratio = 0.8  # 80% for content
            response_ratio = 0.2  # 20% for response
            file_ratio = 0.4  # 40% of content for files
            history_ratio = 0.4  # 40% of content for history

        # Calculate allocations
        content_tokens = int(total_tokens * content_ratio)
        response_tokens = reserved_for_response or int(total_tokens * response_ratio)

        # Sub-allocations within content budget
        file_tokens = int(content_tokens * file_ratio)
        history_tokens = int(content_tokens * history_ratio)

        allocation = TokenAllocation(
            total_tokens=total_tokens,
            content_tokens=content_tokens,
            response_tokens=response_tokens,
            file_tokens=file_tokens,
            history_tokens=history_tokens,
        )

        logger.debug(f"Token allocation for {self.model_name}:")
        logger.debug(f"  Total: {allocation.total_tokens:,}")
        logger.debug(f"  Content: {allocation.content_tokens:,} ({content_ratio:.0%})")
        logger.debug(f"  Response: {allocation.response_tokens:,} ({response_ratio:.0%})")
        logger.debug(f"  Files: {allocation.file_tokens:,} ({file_ratio:.0%} of content)")
        logger.debug(f"  History: {allocation.history_tokens:,} ({history_ratio:.0%} of content)")

        return allocation

    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for text using a model-family-aware tokenizer.

        Uses tiktoken encodings matched to the model family (OpenAI models get
        their exact encoding; Claude/Gemini/other families use tiktoken's
        cl100k_base as the closest available approximation, which is far more
        accurate than a flat character ratio). Falls back to the conservative
        character-based estimate if tiktoken is unavailable or the text can't
        be tokenized (e.g. unknown/local model families).
        """
        encoding = _get_tokenizer_for_model(self.model_name)
        if encoding is not None:
            try:
                return len(encoding.encode(text, disallowed_special=()))
            except Exception:  # noqa: BLE001 - never let token counting break callers
                logger.debug("Tokenizer encode failed for %s; falling back to estimate", self.model_name)
        return len(text) // 3  # Conservative estimate

    @classmethod
    def from_arguments(cls, arguments: dict[str, Any]) -> "ModelContext":
        """Create ModelContext from tool arguments."""
        model_name = arguments.get("model") or DEFAULT_MODEL
        return cls(model_name)
