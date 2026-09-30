"""Usage-budget gate for automated/self-improvement processes -- protects
quota the user needs for other things (a separate trading agent, direct
personal use) from being consumed by Herald's own background loops.

Same shape as approval_gate.py's capability-ceiling pattern (a checker
function that raises on violation, logged either way) applied to quota
instead of tool-execution risk: a hard reserved floor, checked proactively
using both the current live quota snapshot and the recent consumption rate
-- not just "is it already at zero."
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger("herald.usage_budget")

# Automated processes may only ever consume an account's quota down to this
# remaining-percent floor -- the rest is reserved for direct/other use.
# This is a hard floor, not a soft preference. Override per-deployment via
# HERALD_USAGE_RESERVE_PERCENT (e.g. a solo-account setup with no other
# consumer might reasonably lower it).
DEFAULT_RESERVE_PERCENT = 30.0


def _reserve_percent() -> float:
    try:
        return float(os.environ.get("HERALD_USAGE_RESERVE_PERCENT", DEFAULT_RESERVE_PERCENT))
    except ValueError:
        return DEFAULT_RESERVE_PERCENT


class BudgetExceeded(Exception):
    """Raised when firing a loop against this backend would already be at,
    or is projected to soon cross, the reserved quota floor."""


def _weekly_remaining_percent(backend_name: str) -> tuple[float, str | None] | None:
    """The lowest currently-active remaining_percent for this backend
    across its tracked limit windows, plus that limit's resets_at -- the
    binding constraint is whichever window is tightest right now. Returns
    None if this backend has no CLI-subscription quota data at all (e.g. a
    local/free API-key backend with no such concept -- nothing to gate)."""
    from herald.router import cli_usage

    for item in cli_usage.all_usage(refresh=False):
        cli_name = str(item.get("cli") or "")
        if not _matches_backend(cli_name, backend_name):
            continue
        limits = item.get("limits") or []
        if not limits:
            continue
        tightest = min(
            limits,
            key=lambda limit: (
                100 if limit.get("remaining_percent") is None
                else float(limit["remaining_percent"])
            ),
        )
        remaining = tightest.get("remaining_percent")
        return (100.0 if remaining is None else float(remaining)), tightest.get("resets_at")
    return None


def _matches_backend(cli_name: str, backend_name: str) -> bool:
    # Mirrors quota_router.get_exhausted_backend_names()'s own name mapping
    # so the two stay in agreement about which cli_usage row belongs to
    # which registry backend name -- "antigravity" must be checked before
    # the bare "claude" substring match for the same reason documented
    # there (a real cli_name can be "antigravity (Claude and GPT models)").
    name = cli_name.lower()
    if "codex" in name:
        return backend_name == ("codex-backup" if "backup" in name else "codex-primary")
    if "antigravity" in name:
        if "claude" in name or "gpt" in name:
            return backend_name in ("antigravity-claude", "antigravity-gpt")
        if "gemini" in name:
            return backend_name == "antigravity-gemini"
        return False
    if "claude" in name:
        return backend_name == "claude-cli"
    return False


def project_will_cross_reserve(backend_name: str, *, window_hours: int = 6) -> tuple[bool, str]:
    """Forecast, not just react: given the recent consumption rate, will
    this account cross the reserved floor before its quota window resets?
    Returns (would_cross, reason)."""
    from herald.router import telemetry

    snapshot = _weekly_remaining_percent(backend_name)
    if snapshot is None:
        return False, f"'{backend_name}' has no tracked CLI-subscription quota; nothing to gate"
    remaining_pct, resets_at = snapshot
    reserve = _reserve_percent()

    if remaining_pct <= reserve:
        return True, f"'{backend_name}' already at {remaining_pct:.0f}% remaining, at/below the {reserve:.0f}% reserve floor"

    burn = telemetry.recent_burn_rate(backend_name, window_hours=window_hours)
    if not burn or not resets_at:
        # No recent activity to project from, or no reset timestamp to
        # project against -- can't forecast, only react to the snapshot
        # above, which already passed.
        return False, f"'{backend_name}' at {remaining_pct:.0f}% remaining, above the {reserve:.0f}% reserve floor"

    try:
        reset_stamp = (
            datetime.fromtimestamp(resets_at, tz=UTC)
            if isinstance(resets_at, (int, float))
            else datetime.fromisoformat(str(resets_at))
        )
        hours_until_reset = max(0.0, (reset_stamp - datetime.now(UTC)).total_seconds() / 3600)
    except Exception:
        return False, f"'{backend_name}' at {remaining_pct:.0f}% remaining, above the {reserve:.0f}% reserve floor"

    calls_per_hour = burn["calls_per_hour"]
    if calls_per_hour <= 0:
        return False, f"'{backend_name}' at {remaining_pct:.0f}% remaining, no recent burn to project from"

    # Percent-per-call isn't directly available (several CLI backends don't
    # reliably report usage tokens/cost, and cli_usage's quota data is a
    # point-in-time snapshot, not a per-call ledger) -- derive it from this
    # SPECIFIC account's own observed ratio of used-quota to lifetime call
    # count instead of guessing a universal flat rate (a flat "1 call ~= 1%"
    # guess overestimated a healthy account's burn by ~2.5x when checked
    # against real data -- codex-backup's actual 22% used over its real
    # call history projected as if it would blow through 250%+). This is
    # still a rough estimate (lifetime calls aren't scoped to the current
    # reset window), but it's grounded in the account's real behavior
    # rather than an arbitrary constant.
    from herald.router import telemetry as _telemetry
    used_pct = 100.0 - remaining_pct
    lifetime_calls = next(
        (row["total_calls"] for row in _telemetry.usage_summary()["by_backend"]
         if row["backend_name"] == backend_name),
        0,
    )
    pct_per_call = (used_pct / lifetime_calls) if lifetime_calls else 0.05
    projected_pct_per_hour = calls_per_hour * pct_per_call
    projected_remaining_at_reset = remaining_pct - projected_pct_per_hour * hours_until_reset
    if projected_remaining_at_reset <= reserve:
        return True, (
            f"'{backend_name}' at {remaining_pct:.0f}% remaining, but recent burn rate "
            f"({calls_per_hour:.1f} calls/hr) projects crossing the {reserve:.0f}% reserve "
            f"floor before reset in {hours_until_reset:.1f}h"
        )
    return False, f"'{backend_name}' at {remaining_pct:.0f}% remaining, burn rate not projected to cross the {reserve:.0f}% reserve floor before reset"


def check_usage_budget(backend_name: str) -> None:
    """Raise BudgetExceeded if firing more work against this backend would
    already be at, or is projected to soon cross, the reserved floor.
    Logs every check either way -- a real record of what automated work
    was allowed or skipped and why."""
    would_cross, reason = project_will_cross_reserve(backend_name)
    if would_cross:
        logger.warning("usage_budget: BLOCKED %s -- %s", backend_name, reason)
        raise BudgetExceeded(reason)
    logger.info("usage_budget: allowed %s -- %s", backend_name, reason)


def has_bounded_cycle_headroom(
    backend_name: str, *, estimated_percent: float = 2.0,
) -> tuple[bool, str]:
    """Check whether one bounded Admin cycle can run above the hard reserve.

    This is a fallback for a backend whose long-horizon burn projection is
    unsafe.  It never relaxes the reserve floor; callers must also reduce the
    cycle size and cadence.
    """
    snapshot = _weekly_remaining_percent(backend_name)
    if snapshot is None:
        return True, f"'{backend_name}' has no tracked quota and can run a bounded cycle"
    remaining, _ = snapshot
    reserve = _reserve_percent()
    allowed = remaining - estimated_percent > reserve
    reason = (
        f"'{backend_name}' at {remaining:.0f}% remaining; bounded cycle estimate "
        f"{estimated_percent:.1f}% preserves the {reserve:.0f}% reserve floor"
    )
    return allowed, reason
