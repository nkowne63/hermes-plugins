"""``/usage`` for Devin: the account's plan, daily/weekly quota windows and credit balances, all from the one
``SeatManagementService/GetUserStatus`` unary RPC the Devin CLI issues at startup.

The mapping follows oh-my-pi's Devin usage provider: ``GetUserStatusResponse.plan_info`` is authoritative (the
copy under ``plan_status`` only fills in for older servers); a quota window is shown only when the server dated
its reset or the plan is quota-billed, because credit-billed plans leave the percents at their proto default 0,
which would read as an exhausted window; credit buckets with a monthly grant are gauges over the billing cycle
(``plan_end`` is the reset), buckets without one (top-ups) are a balance line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Optional

from . import protocol as wire

if TYPE_CHECKING:
    from agent.account_usage import AccountUsageSnapshot

# Under Hermes' 10 s deadline for a provider's usage hook.
TIMEOUT_S = 8.0

_QUOTAS = (  # (label, remaining-percent field, reset field, plan field that hides it)
    ("Daily quota", "daily_quota_remaining_percent", "daily_quota_reset_at_unix", "hide_daily_quota"),
    ("Weekly quota", "weekly_quota_remaining_percent", "weekly_quota_reset_at_unix", "hide_weekly_quota"),
)
_CREDITS = (  # (label, monthly grant field on PlanInfo, used field, available field on PlanStatus)
    ("Prompt credits", "monthly_prompt_credits", "used_prompt_credits", "available_prompt_credits"),
    ("Flow credits", "monthly_flow_credits", "used_flow_credits", "available_flow_credits"),
    ("Flex credits", "monthly_flex_credit_purchase_amount", "used_flex_credits", "available_flex_credits"),
)


def _tier_label(tier: int) -> Optional[str]:
    """``DEVIN_PRO`` → ``Devin Pro``, for plans that ship no ``plan_name``."""
    name = wire.TEAMS_TIERS.get(tier)
    return name.replace("_", " ").title() if name else None


def _time(seconds: int) -> Optional[datetime]:
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


def snapshot(response: dict[str, Any]) -> Optional[AccountUsageSnapshot]:
    """A decoded ``GetUserStatusResponse`` → Hermes' account-usage snapshot; None without a user status."""
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

    user = response.get("user_status")
    if user is None:
        return None
    status = user.get("plan_status") or {}
    plan = response.get("plan_info") or status.get("plan_info") or {}
    plan_name = str(plan.get("plan_name") or "").strip() or _tier_label(
        int(plan.get("teams_tier") or user.get("teams_tier") or 0))

    windows: list[AccountUsageWindow] = []
    details: list[str] = []
    for label, percent_key, reset_key, hide_key in _QUOTAS:
        reset = int(status.get(reset_key) or 0)
        if plan.get(hide_key) or (reset <= 0 and plan.get("billing_strategy") != wire.BILLING_STRATEGY_QUOTA):
            continue
        remaining = max(0, min(100, int(status.get(percent_key) or 0)))
        windows.append(AccountUsageWindow(label=label, used_percent=float(100 - remaining), reset_at=_time(reset)))
    plan_end = _time(int((status.get("plan_end") or {}).get("seconds") or 0))
    for label, grant_key, used_key, available_key in _CREDITS:
        grant = int(plan.get(grant_key) or 0)
        used = max(0, int(status.get(used_key) or 0))
        available = max(0, int(status.get(available_key) or 0))
        if grant > 0:
            windows.append(AccountUsageWindow(label=label, used_percent=min(100.0, used / grant * 100.0),
                                              reset_at=plan_end, detail=f"{available:,} of {grant:,} left"))
        elif used or available:
            details.append(f"{label}: {available:,} available ({used:,} used)")
    overage = int(status.get("overage_balance_micros") or 0) / 1_000_000
    if overage:
        details.append(f"Overage balance: ${overage:,.2f}")
    return AccountUsageSnapshot(
        provider="devin", source="get_user_status", fetched_at=datetime.now(UTC), plan=plan_name,
        windows=tuple(windows), details=tuple(details),
        unavailable_reason=None if windows or details else "Devin reported no quota or credit balance for this plan",
    )
