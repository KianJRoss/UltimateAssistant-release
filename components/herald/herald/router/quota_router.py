"""Predictive Quota & Reset-Timer Routing Engine for Herald.

Dynamically ranks candidate backends using real-time quota telemetry and reset schedules.
Exhausted accounts with active cooldown windows are automatically demoted behind healthy backends,
and automatically promoted back to primary rank the moment their reset timestamp passes.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from herald.router import cli_usage, quota_tracker
from herald.router.registry import Backend


def get_exhausted_backend_names(usage_data: list[dict[str, Any]] | None = None) -> set[str]:
    """Return set of backend names currently rate-limited or locked out before reset.

    `usage_data`: pass pre-fetched rows (e.g. from `GET /usage/cli`) when
    calling from a process that doesn't share this module's in-memory cache
    -- cli_usage's cache is per-process, so a short-lived subprocess (an MCP
    tool like herald/consult_tools.py) calling `cli_usage.all_usage(refresh=
    False)` directly always sees an empty cache and gets nothing useful.
    Defaults to the in-process cache for existing in-router callers
    (rank_candidates, used on every routing decision) where that's correct.
    """
    demoted: set[str] = set()
    now = time.time()

    if usage_data is None:
        try:
            usage_data = cli_usage.all_usage(refresh=False)
        except Exception:
            usage_data = []

    for item in usage_data:
        cli_name = str(item.get("cli") or "").lower()
        for limit in item.get("limits", []):
            rem_pct = limit.get("remaining_percent")
            if rem_pct is not None and rem_pct <= 0:
                resets_at = limit.get("resets_at")
                if resets_at:
                    try:
                        stamp = (
                            datetime.fromtimestamp(resets_at).timestamp()
                            if isinstance(resets_at, (int, float))
                            else datetime.fromisoformat(str(resets_at)).timestamp()
                        )
                        if stamp > now:
                            # Quota is exhausted and reset timer has not yet arrived.
                            # "antigravity" must be checked before the bare "claude"
                            # match below -- a real cli_name here can be literally
                            # "antigravity (Claude and GPT models)", which contains
                            # "claude" too, so checking "claude" first silently
                            # misattributed every antigravity-claude/-gpt exhaustion
                            # to claude-cli instead and never reached this branch.
                            if "codex" in cli_name:
                                if "backup" in cli_name:
                                    demoted.add("codex-backup")
                                else:
                                    demoted.add("codex-primary")
                            elif "antigravity" in cli_name:
                                if "claude" in cli_name or "gpt" in cli_name:
                                    demoted.add("antigravity-claude")
                                    demoted.add("antigravity-gpt")
                                elif "gemini" in cli_name:
                                    demoted.add("antigravity-gemini")
                            elif "claude" in cli_name:
                                demoted.add("claude-cli")
                    except Exception:
                        pass
    return demoted


def rank_candidates(
    candidates: list[Backend],
    policy: Any = None,
    prompt: str = "",
) -> list[Backend]:
    """Re-rank candidate backends based on policy automatic_order tiers, live quota headroom, and reset schedules.

    Strictly groups candidates by their policy backend-type tier (e.g. all 'cli' backends
    before any 'api_key' backends in quality mode), then prioritizes healthy accounts
    over exhausted accounts within each tier, and finally sorts by configured priority and name.
    """
    if len(candidates) <= 1:
        return candidates

    exhausted = get_exhausted_backend_names()

    # Determine backend type ranking based on policy's automatic_order
    type_order: dict[str, int] = {}
    if policy:
        order = policy.automatic_order
        if prompt:
            from herald.routing_policy import _effective_order
            order = _effective_order(policy, prompt)
        type_order = {backend_type: index for index, backend_type in enumerate(order)}

    def sort_key(b: Backend) -> tuple[int, int, int, str]:
        # 1. Policy backend-type tier (e.g. all CLI candidates = 0, API keys = 1, etc.)
        tier = type_order.get(b.backend_type, 99)
        # 2. Healthy accounts (is_exhausted=0) always rank ahead of exhausted accounts (is_exhausted=1)
        is_exhausted = 1 if b.name in exhausted else 0
        # 3. Configured priority and name
        return (tier, is_exhausted, b.priority, b.name)

    return sorted(candidates, key=sort_key)
