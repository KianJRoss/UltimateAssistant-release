"""Mistral AI model provider implementation."""

import logging
from typing import TYPE_CHECKING, ClassVar, Optional

if TYPE_CHECKING:
    from tools.models import ToolModelCategory

from .openai_compatible import OpenAICompatibleProvider
from .registries.mistral import MistralModelRegistry
from .registry_provider_mixin import RegistryBackedProviderMixin
from .shared import ModelCapabilities, ProviderType

logger = logging.getLogger(__name__)


class MistralModelProvider(RegistryBackedProviderMixin, OpenAICompatibleProvider):
    """Integration for Mistral AI models exposed over an OpenAI-style API.

    Publishes capability metadata for officially supported models and
    maps tool-category preferences to appropriate Mistral models.
    """

    FRIENDLY_NAME = "Mistral AI"

    REGISTRY_CLASS = MistralModelRegistry
    MODEL_CAPABILITIES: ClassVar[dict[str, ModelCapabilities]] = {}

    # Canonical model identifiers used for category routing
    PRIMARY_MODEL = "mistral-large-latest"
    CODING_MODEL = "codestral-latest"
    FAST_MODEL = "ministral-8b-latest"
    VISION_MODEL = "pixtral-large-latest"

    def __init__(self, api_key: str, **kwargs):
        """Initialize Mistral AI provider with API key."""
        # Set Mistral AI base URL
        kwargs.setdefault("base_url", "https://api.mistral.ai/v1")
        self._ensure_registry()
        super().__init__(api_key, **kwargs)
        self._invalidate_capability_cache()

    def get_provider_type(self) -> ProviderType:
        """Get the provider type."""
        return ProviderType.MISTRAL

    def get_preferred_model(self, category: "ToolModelCategory", allowed_models: list[str]) -> Optional[str]:
        """Get Mistral AI's preferred model for a given category from allowed models.

        Args:
            category: The tool category requiring a model
            allowed_models: Pre-filtered list of models allowed by restrictions

        Returns:
            Preferred model name or None
        """
        from tools.models import ToolModelCategory

        if not allowed_models:
            return None

        # For code generation tasks, prefer Codestral
        # Check if any model has allow_code_generation=True
        for model_name in allowed_models:
            try:
                caps = self.get_capabilities(model_name)
                if caps.allow_code_generation:
                    if model_name == self.CODING_MODEL:
                        return self.CODING_MODEL
            except (ValueError, AttributeError):
                continue

        # If Codestral is in the allowed list but not yet returned, prioritize it for coding
        if self.CODING_MODEL in allowed_models:
            return self.CODING_MODEL

        # For extended reasoning tasks, use flagship model
        if category == ToolModelCategory.EXTENDED_REASONING:
            if self.PRIMARY_MODEL in allowed_models:
                return self.PRIMARY_MODEL
            # Fall back to vision model if available
            if self.VISION_MODEL in allowed_models:
                return self.VISION_MODEL
            return allowed_models[0]

        # For fast response tasks, use lightweight model
        elif category == ToolModelCategory.FAST_RESPONSE:
            if self.FAST_MODEL in allowed_models:
                return self.FAST_MODEL
            return allowed_models[0]

        # For balanced or default tasks, use flagship
        else:
            if self.PRIMARY_MODEL in allowed_models:
                return self.PRIMARY_MODEL
            if self.VISION_MODEL in allowed_models:
                return self.VISION_MODEL
            return allowed_models[0]


# Load registry data at import time
MistralModelProvider._ensure_registry()
