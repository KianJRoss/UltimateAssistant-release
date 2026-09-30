"""Startup healthcheck for environment variables.

Herald has no env var that hard-fails boot -- every setting in .env.example
either has a working default or degrades a specific feature gracefully when
absent (e.g. missing PAL_ROOT just disables clink/provider CLI discovery;
missing provider API keys just disable that provider). Silently degrading is
the current, load-bearing behavior -- don't turn any of these into a crash.

Instead, this module prints one clear, loud warning at startup naming
exactly which optional vars are unset and what they affect, so a missing var
shows up in the startup log instead of being discovered later as "why isn't
claude-cli registering" three layers down.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# name -> (what breaks if unset, "path" if the value must be an existing dir)
SOFT_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("PAL_ROOT", "clink/provider CLI discovery (claude-cli, antigravity profiles) will be skipped", "path"),
    ("GEMINI_API_KEY", "Gemini API-key backends will be unavailable", "value"),
    ("ANTHROPIC_API_KEY", "direct Anthropic API-key backend will be unavailable", "value"),
    ("OPENAI_API_KEY", "direct OpenAI API-key backend will be unavailable", "value"),
    ("OPENROUTER_API_KEY", "OpenRouter backend will be unavailable", "value"),
)

# Provider keys herald setup may have stored in the OS keyring (service
# "herald", the same convention account_registry.py's keyring: secret_ref
# scheme uses) rather than as plaintext in the generated .env.
_KEYRING_BACKED_VARS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def hydrate_provider_keys_from_keyring() -> None:
    """Populate raw provider env vars from the keyring when not already set.

    Call after loading the wizard's .env, before check_required_env(): if
    `herald setup` stored a key in the keyring (its default now), this is
    what actually gets it into os.environ for provider registries that read
    GEMINI_API_KEY/etc directly. A no-op, not an error, when the keyring is
    unavailable (headless containers, CI) or nothing was ever stored.
    """
    try:
        import keyring
    except Exception:  # noqa: BLE001
        return
    for name in _KEYRING_BACKED_VARS:
        if os.environ.get(name):
            continue
        try:
            value = keyring.get_password("herald", name)
        except Exception:  # noqa: BLE001
            return  # keyring backend unusable this run; don't retry per-var
        if value:
            os.environ[name] = value


def check_required_env(env: dict[str, str] | None = None) -> None:
    import os

    source = env if env is not None else os.environ
    problems: list[str] = []
    for name, effect, kind in SOFT_CHECKS:
        value = source.get(name)
        if not value:
            problems.append(f"  - {name} not set -> {effect}")
            continue
        if kind == "path" and not Path(value).is_dir():
            problems.append(f"  - {name}='{value}' does not exist -> {effect}")

    if problems:
        print(
            "[herald] startup env check -- these are optional but affect what's "
            "available:\n" + "\n".join(problems),
            file=sys.stderr,
        )
