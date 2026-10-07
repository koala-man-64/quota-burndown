"""Narrow adapters for paid balances/counters, independent of quota reset credits."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from .plans import CreditObservation


def amount(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    return result if result.is_finite() and result >= 0 else None


def codex(payload: dict, scope: str, observed_at: datetime, *, complete: bool = False) -> list[CreditObservation]:
    """The account balance is shared; never sum copies in different quota pools."""
    root = payload if isinstance(payload, dict) else {}
    limits = root.get("rateLimitsByLimitId") or root.get("rate_limits_by_limit_id")
    reports = []
    if isinstance(limits, dict):
        reports = [item["credits"] for item in limits.values()
                   if isinstance(item, dict) and isinstance(item.get("credits"), dict)]
    for name in ("rateLimits", "rate_limits"):
        value = root.get(name)
        if isinstance(value, dict) and isinstance(value.get("credits"), dict):
            reports.append(value["credits"])
    if isinstance(root.get("credits"), dict):
        reports.append(root["credits"])
    if not reports and not complete:
        return []  # Notifications omitting balances carry no new credit information.
    balances = {amount(report.get("balance")) for report in reports}
    balance = next(iter(balances)) if len(balances) == 1 else None
    flags = {report.get("hasCredits") for report in reports if type(report.get("hasCredits")) is bool}
    enabled = next(iter(flags)) if len(flags) == 1 else None
    if any(report.get("unlimited") is True for report in reports):
        enabled = True
    return [CreditObservation("codex", scope, observed_at, "app-server", "credits",
                              balance=balance, enabled=enabled)]


def claude(payload: dict, scope: str, observed_at: datetime, *, account_verified: bool = False) -> list[CreditObservation]:
    """No USD/cents assumption: require explicit currency and scale metadata.

    Claude's OAuth usage endpoint is undocumented. Unknown shape means unknown
    spending, rather than silently adopting an API-dollar estimate as a debit.
    """
    extra = payload.get("extra_usage") if isinstance(payload, dict) else None
    if not isinstance(extra, dict):
        return []
    enabled = extra.get("is_enabled") if type(extra.get("is_enabled")) is bool else None
    places = extra.get("decimal_places")
    valid_unit = extra.get("currency") == "USD" and type(places) is int and 0 <= places <= 6
    counter = amount(extra.get("used_credits")) if valid_unit and account_verified else None
    balance = amount(extra.get("balance")) if valid_unit else None
    if valid_unit:
        divisor = Decimal(10) ** places
        counter = counter / divisor if counter is not None else None
        balance = balance / divisor if balance is not None else None
    period = extra.get("resets_at") or extra.get("period_start")
    if not isinstance(period, str) or len(period) > 128:
        period = None
    # Account-unverified balances are useful to display, but cannot prove a debit.
    return [CreditObservation("claude", scope, observed_at, "oauth-usage", "USD",
                              balance=balance, cumulative_spent=counter,
                              counter_period=period, enabled=enabled, account_verified=account_verified,
                              note="" if account_verified else "Account attribution unverified; spending unavailable.")]
