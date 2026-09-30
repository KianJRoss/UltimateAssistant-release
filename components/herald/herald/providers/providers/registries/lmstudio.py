"""Registry loader for LM Studio models."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

from ..shared import ModelCapabilities, ProviderType, TemperatureConstraint
from .base import CapabilityModelRegistry

logger = logging.getLogger(__name__)


class LMStudioModelRegistry(CapabilityModelRegistry):
    """Capability registry backed by ``conf/lmstudio_models.json`` and dynamic CLI discovery."""

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__(
            env_var_name="LMSTUDIO_MODELS_CONFIG_PATH",
            default_filename="lmstudio_models.json",
            provider=ProviderType.CUSTOM,  # Uses Custom/OpenAI compatible interface
            friendly_prefix="LM Studio ({model})",
            config_path=config_path,
        )

    def reload(self) -> None:
        """Reload from config file and attempt dynamic discovery via 'lms' CLI."""
        # First load from file (base implementation)
        super().reload()
        
        # Then try dynamic discovery
        self._discover_local_models()

    def _discover_local_models(self) -> None:
        """Attempt to discover models using 'lms' CLI."""
        lms_path = shutil.which("lms")
        if not lms_path:
            logger.debug("'lms' CLI not found, skipping dynamic model discovery")
            return

        try:
            # Run 'lms ls --json' if available, or just 'lms ls' and parse
            # Current lms CLI might not support --json, so we'll parse text
            # Format usually: "  publisher/repo/model-file  (size)"
            result = subprocess.run(
                ["lms", "ls"], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            
            if result.returncode != 0:
                logger.debug(f"'lms ls' failed: {result.stderr}")
                return

            discovered_count = 0
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("SIZE") or " " not in line:
                    continue
                
                # Simple parsing: assume first token is model ID
                # This depends on lms ls output format
                parts = line.split()
                if not parts:
                    continue
                    
                model_id = parts[0]
                
                # If model not already in registry, add it with defaults
                if model_id not in self.model_map:
                    # Determine capabilities based on name
                    is_vision = "vision" in model_id.lower() or "vl" in model_id.lower()
                    context_window = 128000 if "128k" in model_id.lower() else \
                                    32000 if "32k" in model_id.lower() else \
                                    8192 if "8k" in model_id.lower() else \
                                    4096
                                    
                    cap = ModelCapabilities(
                        model_name=model_id,
                        friendly_name=f"LM Studio Local ({model_id})",
                        description=f"Locally hosted model: {model_id}",
                        context_window=context_window,
                        max_output_tokens=context_window, # Usually bounded by context
                        supports_images=is_vision,
                        supports_extended_thinking=False, # Can't know for sure
                        provider=ProviderType.CUSTOM
                    )
                    
                    self.model_map[model_id] = cap
                    
                    # Add simple alias (e.g. filename only)
                    if "/" in model_id:
                        simple_name = model_id.split("/")[-1]
                        if simple_name not in self.alias_map:
                            self.alias_map[simple_name.lower()] = model_id
                            
                    discovered_count += 1

            if discovered_count > 0:
                logger.info(f"Discovered {discovered_count} local LM Studio models via CLI")
                
        except Exception as e:
            logger.debug(f"Failed to discover local models: {e}")
