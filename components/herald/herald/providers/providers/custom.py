"""Custom API provider implementation."""

import asyncio
import json
import logging
from urllib.parse import urlparse

import aiohttp

from herald.utils.utils.env import get_env

from .openai_compatible import OpenAICompatibleProvider
from .registries.custom import CustomEndpointModelRegistry
from .registries.openrouter import OpenRouterModelRegistry
from .shared import ModelCapabilities, ModelResponse, ProviderType


class CustomProvider(OpenAICompatibleProvider):
    """Adapter for self-hosted or local OpenAI-compatible endpoints.

    Role
        Provide a uniform bridge between the MCP server and user-managed
        OpenAI-compatible services (Ollama, vLLM, LM Studio, bespoke gateways).
        By subclassing :class:`OpenAICompatibleProvider` it inherits request and
        token handling, while the custom registry exposes locally defined model
        metadata.

    Notable behaviour
        * Uses :class:`OpenRouterModelRegistry` to load model definitions and
          aliases so custom deployments share the same metadata pipeline as
          OpenRouter itself.
        * Normalises version-tagged model names (``model:latest``) and applies
          restriction policies just like cloud providers, ensuring consistent
          behaviour across environments.
    """

    FRIENDLY_NAME = "Custom API"

    # Model registry for managing configurations and aliases
    _registry: CustomEndpointModelRegistry | None = None

    def __init__(self, api_key: str = "", base_url: str = "", **kwargs):
        """Initialize Custom provider for local/self-hosted models.

        This provider supports any OpenAI-compatible API endpoint including:
        - Ollama (typically no API key required)
        - vLLM (may require API key)
        - LM Studio (may require API key)
        - Text Generation WebUI (may require API key)
        - Enterprise/self-hosted APIs (typically require API key)

        Args:
            api_key: API key for the custom endpoint. Can be empty string for
                    providers that don't require authentication (like Ollama).
                    Falls back to CUSTOM_API_KEY environment variable if not provided.
            base_url: Base URL for the custom API endpoint (e.g., 'http://localhost:11434/v1').
                     Falls back to CUSTOM_API_URL environment variable if not provided.
            **kwargs: Additional configuration passed to parent OpenAI-compatible provider

        Raises:
            ValueError: If no base_url is provided via parameter or environment variable
        """
        # Fall back to environment variables only if not provided
        if not base_url:
            base_url = get_env("CUSTOM_API_URL", "") or ""
        if not api_key:
            api_key = get_env("CUSTOM_API_KEY", "") or ""

        if not base_url:
            raise ValueError(
                "Custom API URL must be provided via base_url parameter or CUSTOM_API_URL environment variable"
            )

        # For Ollama and other providers that don't require authentication,
        # set a dummy API key to avoid OpenAI client header issues
        if not api_key:
            api_key = "dummy-key-for-unauthenticated-endpoint"
            logging.debug("Using dummy API key for unauthenticated custom endpoint")

        logging.info(f"Initializing Custom provider with endpoint: {base_url}")

        self._alias_cache: dict[str, str] = {}

        super().__init__(api_key, base_url=base_url, **kwargs)

        # Initialize model registry
        if CustomProvider._registry is None:
            CustomProvider._registry = CustomEndpointModelRegistry()
            # Log loaded models and aliases only on first load
            models = self._registry.list_models()
            aliases = self._registry.list_aliases()
            logging.info(f"Custom provider loaded {len(models)} models with {len(aliases)} aliases")

    # ------------------------------------------------------------------
    # Capability surface
    # ------------------------------------------------------------------
    def _lookup_capabilities(
        self,
        canonical_name: str,
        requested_name: str | None = None,
    ) -> ModelCapabilities | None:
        """Return capabilities for models explicitly marked as custom."""

        builtin = super()._lookup_capabilities(canonical_name, requested_name)
        if builtin is not None:
            return builtin

        registry_entry = self._registry.resolve(canonical_name)
        if registry_entry:
            registry_entry.provider = ProviderType.CUSTOM
            return registry_entry

        logging.debug(
            "Custom provider cannot resolve model '%s'; ensure it is declared in custom_models.json",
            canonical_name,
        )
        return None

    def get_provider_type(self) -> ProviderType:
        """Identify this provider for restriction and logging logic."""

        return ProviderType.CUSTOM

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------

    def _resolve_model_name(self, model_name: str) -> str:
        """Resolve registry aliases and strip version tags for local models."""

        cache_key = model_name.lower()
        if cache_key in self._alias_cache:
            return self._alias_cache[cache_key]

        config = self._registry.resolve(model_name)
        if config:
            if config.model_name != model_name:
                logging.debug("Resolved model alias '%s' to '%s'", model_name, config.model_name)
            resolved = config.model_name
            self._alias_cache[cache_key] = resolved
            self._alias_cache.setdefault(resolved.lower(), resolved)
            return resolved

        if ":" in model_name:
            base_model = model_name.split(":")[0]
            logging.debug(f"Stripped version tag from '{model_name}' -> '{base_model}'")

            base_config = self._registry.resolve(base_model)
            if base_config:
                logging.debug("Resolved base model '%s' to '%s'", base_model, base_config.model_name)
                resolved = base_config.model_name
                self._alias_cache[cache_key] = resolved
                self._alias_cache.setdefault(resolved.lower(), resolved)
                return resolved
            self._alias_cache[cache_key] = base_model
            return base_model

        logging.debug(f"Model '{model_name}' not found in registry, using as-is")
        # Attempt to resolve via OpenRouter registry so aliases still map cleanly
        openrouter_registry = OpenRouterModelRegistry()
        openrouter_config = openrouter_registry.resolve(model_name)
        if openrouter_config:
            resolved = openrouter_config.model_name
            self._alias_cache[cache_key] = resolved
            self._alias_cache.setdefault(resolved.lower(), resolved)
            return resolved

        self._alias_cache[cache_key] = model_name
        return model_name

    def get_all_model_capabilities(self) -> dict[str, ModelCapabilities]:
        """Expose registry capabilities for models marked as custom."""

        if not self._registry:
            return {}

        capabilities = {}
        for model in self._registry.list_models():
            config = self._registry.resolve(model)
            if config:
                capabilities[model] = config
        return capabilities

    async def _ensure_model_loaded(self, model_name: str):
        """Ensure the specified model is loaded in Ollama if the endpoint is Ollama."""

        # Check if this is an Ollama endpoint (contains "ollama" in URL or uses port 11434)
        parsed_url = urlparse(self.base_url)
        is_ollama = (
            "ollama" in self.base_url.lower() or
            parsed_url.port == 11434 or
            "localhost:11434" in self.base_url
        )

        if not is_ollama:
            # Not an Ollama endpoint, skip model loading check
            return

        # Extract the actual model name, removing any tags like ":latest"
        base_model_name = model_name.split(':')[0] if ':' in model_name else model_name

        # Make a request to check if the model is loaded
        ollama_base_url = self.base_url.replace('/v1', '')  # Remove /v1 if present for Ollama API
        tags_url = f"{ollama_base_url}/api/tags"

        try:
            # Check if model is already loaded
            async with aiohttp.ClientSession() as session:
                async with session.get(tags_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        loaded_models = [model['name'].split(':')[0] for model in data.get('models', [])]

                        if base_model_name in loaded_models:
                            logging.debug(f"Ollama model '{base_model_name}' is already loaded")
                            return
                        else:
                            logging.info(f"Ollama model '{base_model_name}' not loaded, initiating pull...")
                    else:
                        logging.warning(f"Failed to get loaded models from Ollama: {response.status}")
                        return

            # Model not loaded, need to pull it
            pull_url = f"{ollama_base_url}/api/pull"

            # Send the pull request
            payload = {"name": base_model_name}
            async with session.post(pull_url, json=payload) as pull_response:
                if pull_response.status == 200:
                    logging.info(f"Pulling Ollama model '{base_model_name}'...")

                    # Stream the response to track progress
                    async for line in pull_response.content:
                        if line:
                            try:
                                progress_data = json.loads(line.decode('utf-8'))
                                status = progress_data.get('status', '')
                                completed = progress_data.get('completed', 0)
                                total = progress_data.get('total', 1)

                                if 'completed' in progress_data:
                                    percentage = (completed / total) * 100 if total > 0 else 0
                                    logging.debug(f"Loading {base_model_name}: {percentage:.1f}% - {status}")
                                else:
                                    logging.debug(f"Loading {base_model_name}: {status}")
                            except json.JSONDecodeError:
                                # Handle cases where the response isn't JSON
                                logging.debug(f"Ollama response: {line.decode('utf-8')}")

                    logging.info(f"Successfully loaded Ollama model '{base_model_name}'")
                else:
                    error_text = await pull_response.text()
                    logging.error(f"Failed to pull Ollama model '{base_model_name}': {pull_response.status} - {error_text}")
                    raise Exception(f"Failed to load Ollama model '{base_model_name}'. Is the model name correct?")

            # After pulling, verify the model is ready by making a quick generate call
            check_url = f"{ollama_base_url}/api/generate"
            check_payload = {
                "model": base_model_name,
                "prompt": "",  # Empty prompt for quick check
                "stream": False
            }

            # Retry mechanism to check if model is ready
            max_retries = 10
            for attempt in range(max_retries):
                try:
                    async with session.post(check_url, json=check_payload) as check_response:
                        if check_response.status == 200:
                            logging.debug(f"Ollama model '{base_model_name}' is ready for use")
                            return
                        else:
                            # Model might still be initializing, wait and retry
                            logging.debug(f"Waiting for model '{base_model_name}' to be ready... (attempt {attempt + 1}/{max_retries})")
                            await asyncio.sleep(1)
                except Exception as e:
                    logging.debug(f"Attempt {attempt + 1} failed while checking model '{base_model_name}': {str(e)}")
                    await asyncio.sleep(1)

            # If we get here, the model isn't ready after retries
            raise Exception(f"Ollama model '{base_model_name}' was pulled but is not responding after {max_retries} attempts")

        except Exception as e:
            logging.error(f"Error ensuring Ollama model '{base_model_name}' is loaded: {str(e)}")
            raise

    async def generate_content(
        self,
        prompt: str,
        model_name: str,
        system_prompt: str = None,
        temperature: float = 0.3,
        max_output_tokens: int = None,
        images: list[str] = None,
        **kwargs,
    ) -> ModelResponse:
        """Generate content using the OpenAI-compatible API with automatic model loading for Ollama."""
        # Ensure the model is loaded before proceeding (especially important for Ollama)
        await self._ensure_model_loaded(model_name)

        # Call the parent method to perform the actual API call
        return await super().generate_content(
            prompt=prompt,
            model_name=model_name,
            system_prompt=system_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            images=images,
            **kwargs
        )
