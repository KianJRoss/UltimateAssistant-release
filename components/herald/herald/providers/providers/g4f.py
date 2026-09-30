"""G4F multi-account provider for PAL.

Routes requests through the g4f gateway (start_g4f.py) which multiplexes
multiple browser-session-authenticated accounts behind one OpenAI-compatible
endpoint.  Model names use the format "g4f-<account>/<underlying-model>",
e.g. "g4f-burner1/gpt-4o".

The provider is purely additive — no existing provider is modified.  It is
disabled unless G4F_GATEWAY_URL is set in the environment.

Auth tokens are managed externally:
  * HAR files:  drop a .har export into g4f_accounts/<name>/har_and_cookies/
  * Env token:  set CHATGPT_ACCESS_TOKEN_<NAME> (e.g. CHATGPT_ACCESS_TOKEN_BURNER1)
    and the gateway's launch_account.py will inject it automatically.
  * Auto-auth:  run g4f_accounts/auto_auth.py (Playwright-based, see that file).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .openai_compatible import OpenAICompatibleProvider
from .shared import ModelCapabilities, ProviderType
from .shared.temperature import RangeTemperatureConstraint

logger = logging.getLogger(__name__)

# Where accounts.json lives relative to this file.
# Resolved at import time so it works whether PAL is run from any cwd.
_ACCOUNTS_JSON = Path(__file__).resolve().parents[2] / "g4f_accounts" / "accounts.json"

# Default gateway started by start_g4f.py
_DEFAULT_GATEWAY_URL = "http://127.0.0.1:4900/v1"

# Underlying models each g4f/ChatGPT Plus account can serve.
# Extend this list as g4f adds providers.
_UNDERLYING_MODELS: list[tuple[str, int]] = [
    # (model_name, intelligence_score)
    ("gpt-4o",          16),
    ("gpt-4o-mini",     13),
    ("gpt-4-turbo",     15),
    ("o3",              18),
    ("o4-mini",         14),
]


def _load_accounts() -> list[dict]:
    """Return enabled accounts from accounts.json, or [] if unavailable."""
    try:
        data = json.loads(_ACCOUNTS_JSON.read_text(encoding="utf-8"))
        return [a for a in data.get("accounts", []) if a.get("enabled", True)]
    except Exception as exc:  # noqa: BLE001
        logger.debug("G4F accounts.json not readable: %s", exc)
        return []


def _make_capabilities(account_name: str, underlying_model: str, score: int) -> ModelCapabilities:
    """Build a ModelCapabilities entry for one account × model combination."""
    canonical = f"g4f-{account_name}/{underlying_model}"
    return ModelCapabilities(
        provider=ProviderType.CUSTOM,
        model_name=canonical,
        friendly_name=f"G4F {account_name} / {underlying_model}",
        intelligence_score=score,
        description=(
            f"Browser-session ChatGPT Plus ({account_name}) proxied through "
            f"the local g4f gateway. Separate usage pool from API subscriptions."
        ),
        aliases=[],
        context_window=128_000,
        max_output_tokens=16_384,
        supports_system_prompts=True,
        supports_streaming=True,
        supports_function_calling=False,
        supports_json_mode=True,
        supports_images=("vision" in underlying_model or underlying_model in {"gpt-4o", "gpt-4-turbo"}),
        supports_temperature=True,
        temperature_constraint=RangeTemperatureConstraint(0.0, 2.0, 0.3),
    )


class G4FProvider(OpenAICompatibleProvider):
    """PAL provider that routes through the g4f multi-account gateway.

    The gateway (start_g4f.py / gateway.py) must already be running before
    any request is made.  The provider reads accounts.json at construction
    time so adding a new account + restarting PAL is all that is needed to
    expose new capacity.

    Set G4F_GATEWAY_URL to override the default gateway address.
    Leave it unset and this provider returns an empty model list (safe no-op).
    """

    FRIENDLY_NAME = "G4F Gateway"

    def __init__(self, **kwargs):
        gateway_url = os.getenv("G4F_GATEWAY_URL", _DEFAULT_GATEWAY_URL).rstrip("/")
        # G4F gateway needs no real API key — send a dummy so OpenAI SDK is happy.
        super().__init__(api_key="g4f-no-key", base_url=gateway_url, **kwargs)
        self._gateway_url = gateway_url
        self._capabilities: dict[str, ModelCapabilities] = {}
        self._build_capabilities()

    # ------------------------------------------------------------------
    # Provider identity
    # ------------------------------------------------------------------
    def get_provider_type(self) -> ProviderType:
        return ProviderType.G4F

    # ------------------------------------------------------------------
    # Dynamic capability map (rebuilt from accounts.json each startup)
    # ------------------------------------------------------------------
    def _build_capabilities(self) -> None:
        accounts = _load_accounts()
        if not accounts:
            logger.info("G4FProvider: no enabled accounts found — provider will be empty.")
            return
        for account in accounts:
            name = account["name"]
            for model, score in _UNDERLYING_MODELS:
                cap = _make_capabilities(name, model, score)
                self._capabilities[cap.model_name] = cap
        logger.info(
            "G4FProvider: loaded %d model entries across %d account(s) — gateway %s",
            len(self._capabilities),
            len(accounts),
            self._gateway_url,
        )

    def get_all_model_capabilities(self) -> dict[str, ModelCapabilities]:
        return dict(self._capabilities)

    def _lookup_capabilities(
        self,
        canonical_name: str,
        requested_name: Optional[str] = None,
    ) -> Optional[ModelCapabilities]:
        return self._capabilities.get(canonical_name)
