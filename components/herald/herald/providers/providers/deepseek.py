"""DeepSeek model provider implementation."""

import logging
from typing import TYPE_CHECKING, ClassVar, Optional

if TYPE_CHECKING:
    from tools.models import ToolModelCategory

from .openai_compatible import OpenAICompatibleProvider
from .registries.deepseek import DeepSeekModelRegistry
from .registry_provider_mixin import RegistryBackedProviderMixin
from .shared import ModelCapabilities, ProviderType

logger = logging.getLogger(__name__)


class DeepSeekModelProvider(RegistryBackedProviderMixin, OpenAICompatibleProvider):
    """Integration for DeepSeek models exposed over an OpenAI-style API.

    Publishes capability metadata for officially supported models and
    maps tool-category preferences to appropriate DeepSeek models.
    """

    FRIENDLY_NAME = "DeepSeek"

    REGISTRY_CLASS = DeepSeekModelRegistry
    MODEL_CAPABILITIES: ClassVar[dict[str, ModelCapabilities]] = {}

    # Canonical model identifiers used for category routing
    PRIMARY_MODEL = "deepseek-chat"
    CODING_MODEL = "deepseek-coder"
    REASONING_MODEL = "deepseek-reasoner"

    def __init__(self, api_key: str, **kwargs):
        """Initialize DeepSeek provider with API key."""
        # Set DeepSeek base URL
        kwargs.setdefault("base_url", "https://api.deepseek.com/v1")
        self._ensure_registry()
        super().__init__(api_key, **kwargs)
        self._invalidate_capability_cache()

    def get_provider_type(self) -> ProviderType:
        """Get the provider type."""
        return ProviderType.DEEPSEEK

    def get_preferred_model(self, category: "ToolModelCategory", allowed_models: list[str]) -> Optional[str]:
        """Get DeepSeek's preferred model for a given category from allowed models.

        Args:
            category: The tool category requiring a model
            allowed_models: Pre-filtered list of models allowed by restrictions

        Returns:
            Preferred model name or None
        """
        from tools.models import ToolModelCategory

        if not allowed_models:
            return None

        # For code generation tasks, prefer DeepSeek Coder
        # Check if any model has allow_code_generation=True
        for model_name in allowed_models:
            try:
                caps = self.get_capabilities(model_name)
                if caps.allow_code_generation:
                    if model_name == self.CODING_MODEL:
                        return self.CODING_MODEL
            except (ValueError, AttributeError):
                continue

        # If DeepSeek Coder is in the allowed list but not yet returned, prioritize it for coding
        if self.CODING_MODEL in allowed_models:
            return self.CODING_MODEL

        # For extended reasoning tasks, use reasoning model
        if category == ToolModelCategory.EXTENDED_REASONING:
            if self.REASONING_MODEL in allowed_models:
                return self.REASONING_MODEL
            if self.PRIMARY_MODEL in allowed_models:
                return self.PRIMARY_MODEL
            return allowed_models[0]

        # For fast response tasks, use primary chat model
        elif category == ToolModelCategory.FAST_RESPONSE:
            if self.PRIMARY_MODEL in allowed_models:
                return self.PRIMARY_MODEL
            return allowed_models[0]

        # For balanced or default tasks, use primary model
        else:
            if self.PRIMARY_MODEL in allowed_models:
                return self.PRIMARY_MODEL
            if self.REASONING_MODEL in allowed_models:
                return self.REASONING_MODEL
            return allowed_models[0]


# Load registry data at import time
DeepSeekModelProvider._ensure_registry()
