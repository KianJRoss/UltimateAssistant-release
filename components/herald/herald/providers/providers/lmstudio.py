"""LM Studio provider implementation."""

import logging
from typing import Optional

from .openai_compatible import OpenAICompatibleProvider
from .registries.lmstudio import LMStudioModelRegistry
from .shared import ModelCapabilities, ProviderType

logger = logging.getLogger(__name__)


class LMStudioProvider(OpenAICompatibleProvider):
    """Provider for local LM Studio models.
    
    Extends OpenAICompatibleProvider to add dynamic model discovery via 'lms' CLI
    and specific handling for local model characteristics.
    """

    FRIENDLY_NAME = "LM Studio"
    
    _registry: Optional[LMStudioModelRegistry] = None

    def __init__(self, api_key: str = "lm-studio", base_url: str = "http://localhost:1234/v1", **kwargs):
        """Initialize LM Studio provider.
        
        Args:
            api_key: Not typically used by LM Studio, defaults to "lm-studio"
            base_url: Default LM Studio endpoint
            **kwargs: Additional config
        """
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)
        
        # Initialize registry if not already done
        if LMStudioProvider._registry is None:
            LMStudioProvider._registry = LMStudioModelRegistry()
            logger.info(f"LM Studio provider initialized with {len(self._registry.list_models())} known models")

    def get_provider_type(self) -> ProviderType:
        return ProviderType.CUSTOM

    def _lookup_capabilities(
        self, 
        canonical_name: str, 
        requested_name: Optional[str] = None
    ) -> Optional[ModelCapabilities]:
        """Look up capabilities using the LM Studio registry."""
        # Check parent first (though for Custom/OpenAI it usually doesn't have internal map)
        builtin = super()._lookup_capabilities(canonical_name, requested_name)
        if builtin is not None:
            return builtin

        if self._registry:
            registry_entry = self._registry.resolve(canonical_name)
            if registry_entry:
                return registry_entry

        return None

    def get_all_model_capabilities(self) -> dict[str, ModelCapabilities]:
        """Get capabilities for all discovered models."""
        if not self._registry:
            return {}
            
        capabilities = {}
        for model in self._registry.list_models():
            config = self._registry.resolve(model)
            if config:
                capabilities[model] = config
        return capabilities
