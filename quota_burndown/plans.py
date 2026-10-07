"""Paid-credit plans and explicitly sourced spending observations.

The local token ledger measures workload, not paid debits. Only a provider's
monotonic spending counter establishes actual spend.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from . import ledger
from .charts import pool_member, token_efficiency
from .store import Sample
from .util import atomic_write_text, iso, now_utc, parse_iso

PROVIDERS = ("codex", "claude")
UNITS = {"codex": "credits", "claude": "USD"}
# Version 2026-10-07. Rates are input/cache-read/output per million tokens.
# https://learn.chatgpt.com/docs/pricing#token-rates
# https://platform.claude.com/docs/en/about-claude/pricing
RATES_VERSION = "2026-10-07"
RATES = {
    "codex": {
        "gpt-6.1-sol": ("GPT-6.1 Sol", "50", "2.5", "250", ("standard", "fast")),
        "gpt-6-sol": ("GPT-6 Sol", "50", "5", "250", ("standard", "fast")),
        "gpt-6-luna": ("GPT-6 Luna", "2.5", ".25", "12.5", ("standard", "fast")),
        "gpt-6-astra": ("GPT-6 Astra", "250", "25", "1250", ("standard", "fast", "ultrafast")),
    },
    "claude": {
        "claude-fable-5-1": ("Claude Fable 5.1", "10", ".25", "50", ("standard",)),
        "claude-opus-5-5": ("Claude Opus 5.5", "4", ".20", "20", ("standard", "fast")),
        "claude-sonnet-5-5": ("Claude Sonnet 5.5", "2", ".20", "10", ("standard",)),
        "claude-opus-4-6": ("Claude Opus 4.6", "5", ".50", "25", ("standard",)),
        "claude-sonnet-4-6": ("Claude Sonnet 4.6", "3", ".30", "15", ("standard",)),
        "claude-haiku-4-5": ("Claude Haiku 4.5", "1", ".10", "5", ("standard",)),
    },
}
MAX_AMOUNT = Decimal("1000000000")


class Conflict(ValueError):
    """Plan revision differs from the caller's revision."""


def _decimal(value: object, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("amount must be a decimal string")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("amount must be a decimal string") from exc
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0) or number > MAX_AMOUNT:
        raise ValueError("amount is outside the allowed range")
    if number.as_tuple().exponent < -12:
        raise ValueError("amount supports at most 12 decimal places")
    return number


def _utc(value: object) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _as_text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class CreditObservation:
    provider: str
    account_scope: str
    observed_at: datetime
    source: str
    unit: str
    balance: Decimal | str | None = None
    cumulative_spent: Decimal | str | None = None
    counter_period: str | None = None
    enabled: bool | None = None
    account_verified: bool = True
    note: str = ""


def _validate_plan(provider: str, raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("plan must be an object")
    amount = _decimal(raw.get("amount"), allow_zero=False)
    start, end = _utc(raw.get("starts_at")), _utc(raw.get("ends_at"))
    if start >= end:
        raise ValueError("deadline must follow start")
    model, speed = raw.get("model"), raw.get("speed", "standard")
    if model not in RATES[provider] or speed not in RATES[provider][model][4]:
        raise ValueError("unsupported model or speed")
    return {"amount": _as_text(amount), "starts_at": iso(start), "ends_at": iso(end),
            "model": model, "speed": speed}


class PlanState:
    """One JSON authority for plans and confirmed spending, with atomic writes."""

    def __init__(self, home: Path, clock: Callable[[], datetime] = now_utc):
        self.home, self.clock = Path(home), clock
        self.path = self.home / "credit-plans.json"
        self._lock = threading.RLock()
        self._state = {"revision": 0, "plans": {}, "observations": {}, "tracking": {}}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("revision"), int):
                raise ValueError("invalid credit plan state")
            self._state = {key: copy.deepcopy(saved.get(key, default)) for key, default in
                           self._state.items()}

    def _write(self, state: dict) -> None:
        atomic_write_text(self.path, json.dumps(state, separators=(",", ":"), sort_keys=True))

    def apply(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("invalid plan request")
        if "plan" not in payload:
            raise ValueError("plan is required")
        provider = payload.get("provider")
        if provider not in PROVIDERS:
            raise ValueError("invalid provider")
        if type(payload.get("expected_revision")) is not int:
            raise ValueError("expected_revision must be an integer")
        plan = None if payload.get("plan") is None else _validate_plan(provider, payload["plan"])
        with self._lock:
            if payload["expected_revision"] != self._state["revision"]:
                raise Conflict("plan revision changed")
            old = self._state["plans"].get(provider)
            if plan and old and plan["starts_at"] != old["starts_at"]:
                if _utc(plan["starts_at"]) < _utc(self.clock()):
                    raise ValueError("new plan start cannot be backdated")
            next_state = copy.deepcopy(self._state)
            if plan is None:
                next_state["plans"].pop(provider, None)
                next_state["tracking"].pop(provider, None)
            else:
                next_state["plans"][provider] = plan
                if old is None or old["starts_at"] != plan["starts_at"]:
                    next_state["tracking"][provider] = {
                        "confirmed": "0", "baseline": None, "account_scope": None,
                        "counter_period": None, "unknown_gap": False}
            next_state["revision"] += 1
            self._write(next_state)
            self._state = next_state
            return self.snapshot()

    def ingest(self, items: list[CreditObservation]) -> bool:
        with self._lock:
            next_state = copy.deepcopy(self._state)
            changed = False
            for item in sorted(items, key=lambda x: x.observed_at):
                if item.provider not in PROVIDERS or item.unit != UNITS[item.provider] or not item.account_scope:
                    continue
                try:
                    ts = _utc(item.observed_at)
                    if ts > _utc(self.clock()) + timedelta(minutes=5):
                        continue
                    balance = None if item.balance is None else _decimal(item.balance)
                    counter = None if item.cumulative_spent is None else _decimal(item.cumulative_spent)
                except ValueError:
                    continue
                previous = next_state["observations"].get(item.provider)
                if previous and ts <= _utc(previous["observed_at"]):
                    continue
                observation = {
                    "provider": item.provider, "account_scope": item.account_scope,
                    "observed_at": iso(ts), "source": item.source, "unit": item.unit,
                    "balance": None if balance is None else _as_text(balance),
                    "cumulative_spent": None if counter is None else _as_text(counter),
                    "counter_period": item.counter_period, "enabled": item.enabled,
                    "account_verified": item.account_verified, "note": item.note,
                }
                next_state["observations"][item.provider] = observation
                changed = True
                plan = next_state["plans"].get(item.provider)
                if not plan or ts < _utc(plan["starts_at"]):
                    continue
                track = next_state["tracking"].setdefault(item.provider, {
                    "confirmed": "0", "baseline": None, "account_scope": None,
                    "counter_period": None, "unknown_gap": False})
                if ts > _utc(plan["ends_at"]):
                    track["unknown_gap"] = True
                    continue  # A later counter includes spending outside this plan.
                if not item.account_verified:
                    track["unknown_gap"] = True
                    track["baseline"] = None
                    continue
                if counter is None:
                    track["unknown_gap"] = True
                    track["baseline"] = None
                    continue
                old_counter = None if track["baseline"] is None else Decimal(track["baseline"])
                if old_counter is None and ts > _utc(plan["starts_at"]):
                    track["unknown_gap"] = True
                same_series = (track["account_scope"] == item.account_scope
                               and track["counter_period"] == item.counter_period
                               and old_counter is not None and counter >= old_counter)
                if same_series:
                    track["confirmed"] = _as_text(Decimal(track["confirmed"]) + counter - old_counter)
                elif old_counter is not None:
                    track["unknown_gap"] = True
                track["baseline"] = _as_text(counter)
                track["account_scope"] = item.account_scope
                track["counter_period"] = item.counter_period
                # The new baseline can establish subsequent deltas, but an old gap remains noted.
            if changed:
                self._write(next_state)
                self._state = next_state
            return changed

    def snapshot(self) -> dict:
        with self._lock:
            state = copy.deepcopy(self._state)
        now = _utc(self.clock())
        plans = {}
        for provider in PROVIDERS:
            raw = state["plans"].get(provider)
            if raw is None:
                plans[provider] = None
                continue
            start, end, amount = _utc(raw["starts_at"]), _utc(raw["ends_at"]), Decimal(raw["amount"])
            track = state["tracking"].get(provider, {})
            established = track.get("baseline") is not None
            spent = Decimal(track.get("confirmed", "0")) if track.get("account_scope") is not None else None
            observation = state["observations"].get(provider, {})
            stale = not observation or (now - _utc(observation["observed_at"])).total_seconds() > 900
            remaining = max(Decimal(0), amount - spent) if spent is not None else amount
            hours = Decimal(str((end - max(now, start)).total_seconds())) / Decimal(3600)
            full_hours = Decimal(str((end - start).total_seconds())) / Decimal(3600)
            report = dict(raw)
            report.update({
                "unit": UNITS[provider],
                "status": "scheduled" if now < start else "expired" if now >= end else
                          "complete" if spent is not None and spent >= amount else "active",
                "confirmed_spent": _as_text(spent) if spent is not None else None,
                "remaining": _as_text(remaining),
                "required_rate_per_hour": _as_text(remaining / hours) if hours > 0 else None,
                "original_rate_per_hour": _as_text(amount / full_hours),
                "progress_known": established and not track.get("unknown_gap", False) and not stale,
                "note": ("Spending counter unavailable; confirmed spend is a lower bound" if not established and spent is not None else
                         "Spending counter unavailable; forecast only" if not established else
                         "Spending history has a gap; confirmed spend is a lower bound" if track.get("unknown_gap") else
                         "Spending report is stale; confirmed spend is a lower bound" if stale else
                         "Confirmed provider counter spending"),
            })
            plans[provider] = report
        observations = state["observations"]
        for provider, obs in observations.items():
            age = (now - _utc(obs["observed_at"])).total_seconds()
            obs["freshness"] = "fresh" if age <= 900 else "stale"
            obs["age_seconds"] = max(0, age)
            obs["balance_available"] = obs["balance"] is not None
            obs["spending_counter_available"] = obs["cumulative_spent"] is not None and obs.get("account_verified", False)
        models = {
            provider: [{"id": key, "label": value[0], "speeds": list(value[4]),
                        "rates_version": RATES_VERSION} for key, value in rates.items()]
            for provider, rates in RATES.items()
        }
        return {"revision": state["revision"], "plans": plans,
                "observations": observations, "models": models}


def _categories(row: sqlite3.Row) -> tuple[int, int, int, int] | None:
    """Uncached input, cache read, cache write, output; reasoning is in output."""
    if row["input_tokens_inferred"] is not None:
        return None
    inp, read, write, out = (row[k] for k in
                             ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))
    if inp is None or out is None or min(inp, read or 0, write or 0, out) < 0:
        return None
    # Codex input includes both cached reads and cache writes; Claude input
    # excludes both categories. Reasoning is already included in output.
    uncached = inp - (read or 0) - (write or 0) if row["provider"] == "codex" else inp
    if uncached < 0:
        return None
    return uncached, read or 0, write or 0, out


def _cost_range(provider: str, model: str, speed: str, category: tuple[int, int, int, int]) -> tuple[Decimal, Decimal]:
    _, input_rate, read_rate, output_rate, _ = RATES[provider][model]
    uncached, read, write, output = map(Decimal, category)
    multiplier = Decimal(6 if speed == "ultrafast" else 2 if speed == "fast" else 1)
    base = (uncached * Decimal(input_rate) + read * Decimal(read_rate) + output * Decimal(output_rate))
    if provider == "claude":
        low, high = base + write * Decimal(input_rate) * Decimal("1.25"), base + write * Decimal(input_rate) * 2
    else:
        # Codex ledger does not identify separately billed cache-write tokens.
        low = high = base + write * Decimal(input_rate)
    return low * multiplier / 1_000_000, high * multiplier / 1_000_000


def _transition_at(now: datetime, deadline: datetime, reset: datetime | None,
                   period_minutes: int | None, included: Decimal, renewed: Decimal,
                   tokens_per_hour: Decimal | None) -> str | None:
    """Earliest projected paid transition under periodic allowance renewal."""
    if tokens_per_hour is None or tokens_per_hour <= 0:
        return None
    cursor = now
    available = included
    boundary = reset if reset and reset > now and period_minutes and period_minutes > 0 else None
    period = timedelta(minutes=period_minutes) if boundary else None
    for _ in range(10001):
        segment_end = min(deadline, boundary) if boundary else deadline
        if segment_end <= cursor:
            return None
        hours = Decimal(str((segment_end - cursor).total_seconds())) / Decimal(3600)
        if available / tokens_per_hour < hours:
            return iso(cursor + timedelta(hours=float(available / tokens_per_hour)))
        if segment_end >= deadline or boundary is None:
            return None
        cursor = boundary
        available = renewed
        boundary += period
    return None


def _simulate_workload(rate: float, windows: list[dict], now: datetime,
                       deadline: datetime) -> tuple[float, datetime | None]:
    """Consume every applicable included allowance before paid tokens.

    Each quota pool charges the same free workload. Once any pool is exhausted,
    additional work uses paid capacity until a reset renews the applicable pool.
    """
    remaining = [float(w["remaining"]) for w in windows]
    resets = [w["reset"] for w in windows]
    periods = [timedelta(minutes=w["period_minutes"]) for w in windows]
    cursor, paid, transition = now, 0.0, None
    for _ in range(10001):
        if cursor >= deadline:
            return paid, transition
        boundary = min([deadline] + [ts for ts in resets if ts > cursor])
        segment_hours = (boundary - cursor).total_seconds() / 3600
        work = rate * segment_hours
        free = min(work, min(remaining))
        paid += max(0.0, work - free)
        if transition is None and work > free:
            transition = cursor + timedelta(hours=free / rate)
        remaining = [max(0.0, value - free) for value in remaining]
        cursor = boundary
        for idx, ts in enumerate(resets):
            if ts == cursor:
                remaining[idx] = float(windows[idx]["renewed"])
                resets[idx] += periods[idx]
    raise ValueError("too many forecast reset intervals")


def _required_workload_rate(paid_tokens: float, windows: list[dict],
                            now: datetime, deadline: datetime) -> tuple[float, datetime | None]:
    hours = (deadline - now).total_seconds() / 3600
    if hours <= 0:
        raise ValueError("expired forecast")
    if paid_tokens <= 0:
        return 0.0, None
    ceiling = max(1.0, paid_tokens / hours)
    while _simulate_workload(ceiling, windows, now, deadline)[0] < paid_tokens:
        ceiling *= 2
        if not ceiling < 1e30:
            raise ValueError("forecast workload exceeds supported range")
    floor = 0.0
    for _ in range(72):
        midpoint = (floor + ceiling) / 2
        if _simulate_workload(midpoint, windows, now, deadline)[0] >= paid_tokens:
            ceiling = midpoint
        else:
            floor = midpoint
    _, transition = _simulate_workload(ceiling, windows, now, deadline)
    return ceiling, transition


def forecast_snapshot(snapshot: dict, conn: sqlite3.Connection, samples: list[Sample],
                      capacity: dict, now: datetime) -> dict:
    """Add conservative workload forecasts without changing observed quota readings."""
    result = copy.deepcopy(snapshot)
    now = _utc(now)
    usage_rows = ledger.rows(conn, since=now - timedelta(days=7), until=now + timedelta(microseconds=1),
                             kind=ledger.REQUEST)
    for provider, plan in result["plans"].items():
        if plan is None:
            continue
        unavailable = {"available": False, "note": "Insufficient recorded token history or quota calibration",
                       "token_capacity_low": None, "token_capacity_high": None,
                       "required_tokens_per_hour_low": None, "required_tokens_per_hour_high": None,
                       "required_paid_tokens_per_hour_low": None, "required_paid_tokens_per_hour_high": None,
                       "windows": []}
        plan["forecast"] = unavailable
        model = plan["model"]
        selected = [r for r in usage_rows if r["provider"] == provider and r["model"] == model
                    and _categories(r) is not None]
        fallback = False
        if not selected or sum(sum(_categories(row)) for row in selected) == 0:
            selected = [r for r in usage_rows if r["provider"] == provider and _categories(r) is not None]
            fallback = True
        if not selected:
            continue
        categories = tuple(sum(_categories(row)[idx] for row in selected) for idx in range(4))
        total = sum(categories)
        if total <= 0:
            continue
        low_cost, high_cost = _cost_range(provider, model, plan["speed"], categories)
        if low_cost <= 0 or high_cost <= 0:
            continue
        remaining = Decimal(plan["remaining"])
        low_tokens = remaining * total / high_cost
        high_tokens = remaining * total / low_cost
        rate = Decimal(plan["required_rate_per_hour"]) if plan["required_rate_per_hour"] else None
        unavailable["token_capacity_low"] = float(low_tokens)
        unavailable["token_capacity_high"] = float(high_tokens)
        if rate is not None:
            unavailable["required_paid_tokens_per_hour_low"] = float(rate * total / high_cost)
            unavailable["required_paid_tokens_per_hour_high"] = float(rate * total / low_cost)
        quota_usage = [(ledger.row_ts(r), r["model"], int(r["total_tokens"]))
                       for r in usage_rows if r["provider"] == provider and r["total_tokens"] is not None
                       and r["input_tokens_inferred"] is None]
        windows = []
        simulated = []
        invalid_window = False
        for pool in capacity.get("pools", []):
            if pool.get("provider") != provider:
                continue
            for window in pool.get("windows", []):
                raw_window = window.get("window")
                window_name = raw_window if raw_window and ":" in raw_window else (
                    f"{raw_window}:{pool.get('limit_id')}" if raw_window and pool.get("limit_id") else None)
                if not window_name or not pool_member(provider, window_name)(model):
                    continue
                observation = result["observations"].get(provider, {})
                if (observation.get("account_verified") and pool.get("account_scope")
                        and observation.get("account_scope") != pool["account_scope"]):
                    invalid_window = True
                    continue
                window_samples = [s for s in samples if s.provider == provider and s.window == window_name]
                # Never calibrate across different reset instances.
                if window.get("resets_at"):
                    reset = parse_iso(window["resets_at"])
                    window_samples = [s for s in window_samples if s.resets_at == reset]
                efficiency = token_efficiency(window_samples, quota_usage, provider, window_name) if window_name else None
                reset = parse_iso(window.get("resets_at"))
                minutes = window.get("window_min")
                if (efficiency is None or window.get("freshness") != "fresh"
                        or window.get("remaining_pct") is None or reset is None
                        or reset <= now or not isinstance(minutes, int) or minutes <= 0):
                    invalid_window = True
                    continue
                tpp = Decimal(str(efficiency.tokens_per_point))
                remaining_pct = window.get("remaining_pct")
                included = max(Decimal(0), Decimal(str(remaining_pct))) * tpp
                usable_pct = window.get("usable_pct")
                reserved = (max(Decimal(0), Decimal(str(remaining_pct)) - Decimal(str(usable_pct))) * tpp
                            if usable_pct is not None else None)
                resets = 0
                if reset and minutes and reset > now and reset < _utc(plan["ends_at"]):
                    period = timedelta(minutes=int(minutes))
                    boundary = reset
                    while boundary < _utc(plan["ends_at"]) and resets < 10000:
                        resets += 1
                        boundary += period
                windows.append({
                    "key": window_name,
                    "extra_percentage_points_low": float(low_tokens / tpp),
                    "extra_percentage_points_high": float(high_tokens / tpp),
                    "remaining_included_tokens": float(included),
                    "reserve_tokens": float(reserved) if reserved is not None else None,
                    "projected_replenishment_tokens": float(Decimal(resets) * 100 * tpp),
                    "projected_resets": resets,
                    "transition_at": None,
                    "calibration_tokens_per_point": float(tpp),
                })
                simulated.append({"remaining": float(included), "renewed": float(Decimal(100) * tpp),
                                  "reset": reset, "period_minutes": minutes})
        if not windows or invalid_window or _utc(plan["starts_at"]) > now or _utc(plan["ends_at"]) <= now:
            unavailable["note"] = ("Future start or expired deadline has no reliable quota trajectory" if
                                   _utc(plan["starts_at"]) > now or _utc(plan["ends_at"]) <= now else
                                   "Applicable quota window is stale, unknown, or lacks token calibration")
            continue
        try:
            low_workload, low_transition = _required_workload_rate(float(low_tokens), simulated, now, _utc(plan["ends_at"]))
            high_workload, high_transition = _required_workload_rate(float(high_tokens), simulated, now, _utc(plan["ends_at"]))
        except ValueError:
            unavailable["note"] = "Timeframe exceeds the bounded quota-reset forecast; paid budget remains available."
            continue
        transition = high_transition or low_transition
        for window in windows:
            window["transition_at"] = iso(transition) if transition else None
        disabled = result["observations"].get(provider, {}).get("enabled") is False
        plan["forecast"] = {
            "available": True, "note": ("Hypothetical: paid usage disabled. " if disabled else "") +
                    ("Provider-wide token proportions used. " if fallback else "Selected-model token proportions used. ") +
                    ("Cache-write duration unknown; shown as a range." if categories[2] and provider == "claude"
                     else "Local token-to-quota calibration is an estimate."),
            "rates_version": RATES_VERSION, "mix_source": "provider" if fallback else "model",
            "hypothetical": disabled,
            "token_capacity_low": float(low_tokens), "token_capacity_high": float(high_tokens),
            "required_tokens_per_hour_low": low_workload,
            "required_tokens_per_hour_high": high_workload,
            "required_paid_tokens_per_hour_low": float(rate * total / high_cost) if rate is not None else None,
            "required_paid_tokens_per_hour_high": float(rate * total / low_cost) if rate is not None else None,
            "windows": windows,
        }
    return result
