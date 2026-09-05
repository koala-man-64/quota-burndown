"""Versioned, account-scoped capacity observations and conservative derived guidance."""
from __future__ import annotations

import copy
import json
import math
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from .util import atomic_write_text, iso, now_utc, parse_iso, read_json


@dataclass(frozen=True)
class Observation:
    provider: str
    account_scope: str
    limit_id: str
    window: str
    window_min: int
    used_pct: float | None
    resets_at: datetime | None
    observed_at: datetime
    received_at: datetime
    source: str
    reset_provenance: str = "reported"
    models: tuple[str, ...] = ()
    mapping_confidence: str = "reported"
    observation_id: str = ""
    complete_snapshot: bool = False
    observation_time_provenance: str = "reported"

    @property
    def pool_key(self) -> str:
        return ":".join((self.provider, self.account_scope, self.limit_id))

    @property
    def key(self) -> tuple[str, str]:
        return self.pool_key, self.window

    def to_dict(self) -> dict:
        out = asdict(self)
        for key in ("observed_at", "received_at", "resets_at"):
            out[key] = iso(out[key]) if out[key] else None
        return out

    @classmethod
    def from_dict(cls, value: dict) -> "Observation":
        data = dict(value)
        for key in ("observed_at", "received_at", "resets_at"):
            data[key] = parse_iso(data.get(key))
        data["models"] = tuple(data.get("models") or ())
        return cls(**data)


def valid_observation(item: Observation, now: datetime) -> bool:
    try:
        return bool(
            item.provider in ("codex", "claude", "antigravity")
            and item.account_scope and item.limit_id and item.window
            and type(item.window_min) is int and item.window_min > 0
            and item.observed_at and item.received_at
            and item.observed_at <= now + timedelta(seconds=5)
            and item.received_at <= now + timedelta(seconds=5)
            and (item.used_pct is None or (
                not isinstance(item.used_pct, bool) and math.isfinite(item.used_pct) and 0 <= item.used_pct <= 100
            ))
        )
    except (TypeError, ValueError):
        return False


def freshness_seconds(source: str) -> int:
    return 1200 if source in ("desktop", "claude_desktop", "desktop-history") else 60


SOURCE_RANK = {"app-server": 4, "statusline": 3, "rollout": 2, "desktop-history": 1}
_RESET_RANK = {"reported": 2, "inferred": 1, "unknown": 0}


def downgrades_reading(item: Observation, prior: Observation, now: datetime) -> bool:
    """A weaker source must not replace a still-valid reading with worse reset information.

    The desktop-history collector runs on its own cadence, so it always looks newer than the
    status-line reading it duplicates.  Recency alone would let it discard a provider-reported
    reset time, downgrade the reset provenance and erase the remaining budget it implies.
    Sources that report resets just as well (rollout against app-server) still take over
    normally, and once the better reading expires the weaker source resumes filling the pool.
    """
    return (
        prior.used_pct is not None
        and SOURCE_RANK.get(item.source, 0) < SOURCE_RANK.get(prior.source, 0)
        and _RESET_RANK.get(item.reset_provenance, 0) < _RESET_RANK.get(prior.reset_provenance, 0)
        and now < prior.observed_at + timedelta(seconds=freshness_seconds(prior.source))
    )


def provider_groups(pools: list[dict]) -> list[dict]:
    """Requested display slots reference observations without creating quota pools.

    A missing slot has no provider reading, timestamp or independent allowance.
    Additional reported windows stay visible even when they are not in this layout.
    """
    specifications = (
        ("codex", "Codex", (
            ("general-weekly", "General weekly", "codex", 10080),
            ("spark-five-hour", "GPT-5.3-Codex-Spark · 5-hour", "codex_bengalfox", 300),
            ("spark-weekly", "GPT-5.3-Codex-Spark · weekly", "codex_bengalfox", 10080),
        )),
        ("antigravity", "Antigravity / Gemini", (
            ("gemini-five-hour", "Gemini · 5-hour", None, 300),
            ("gemini-weekly", "Gemini · weekly", None, 10080),
        )),
        ("claude", "Claude", (
            ("current-session", "Current session · 5-hour", "claude", 300),
            ("all-models-weekly", "All models · weekly", "claude", 10080),
            ("fable-weekly", "Fable · weekly", None, 10080),
        )),
    )
    groups = []
    for provider, title, slots in specifications:
        observed_pools = [pool for pool in pools if pool.get("provider") == provider]
        limits, matched = [], set()

        def entry(slot, label, minutes, pool=None, window=None):
            unavailable = window is None
            if unavailable:
                window = dict.fromkeys(("used_pct", "remaining_pct", "usable_pct", "resets_at",
                    "observed_at", "received_at", "valid_until", "freshness_ttl_s", "source_age_s",
                    "upstream_reporting_delay_s", "whole_window_rate_pph", "recent_rate_pph",
                    "conservative_rate_pph", "sustainable_rate_pph", "runway_minutes", "exhaust_at"))
                window.update(window=f"{minutes}m", window_min=minutes, allowance_state="unknown",
                    freshness="unknown", source="unavailable", reset_provenance="unknown",
                    observation_time_provenance="unknown")
            reason = None
            if unavailable:
                reason = "No reading for this limit from the configured provider sources."
                if provider == "antigravity":
                    reason = "Configured Antigravity sources report activity, not Gemini quota limits."
                elif slot == "fable-weekly":
                    reason = "Supported Claude status-line and desktop history sources do not report Fable weekly quota."
            return {
                "id": f"{provider}:{slot}" + (":" + pool["id"] if pool else ""),
                "label": label, "pool_id": pool["id"] if pool else None,
                "account_scope": pool.get("account_scope") if pool else None,
                "account_scope_confidence": pool.get("account_scope_confidence", "unknown") if pool else "unknown",
                "models": pool.get("models", []) if pool else [],
                "mapping_confidence": pool.get("mapping_confidence", "unknown") if pool else "unknown",
                "constraining_window": pool.get("constraining_window") if pool else None,
                "window": copy.deepcopy(window), "availability_reason": reason,
                "display_only": unavailable,
            }

        for slot, label, limit_id, minutes in slots:
            relevant = [p for p in observed_pools if limit_id and p.get("limit_id") == limit_id]
            for pool in relevant or [None]:
                window = next((w for w in pool.get("windows", []) if w.get("window_min") == minutes), None) if pool else None
                limits.append(entry(slot, label, minutes, pool, window))
                if window is not None:
                    matched.add((pool["id"], window["window"]))
        for pool in observed_pools:
            for window in pool.get("windows", []):
                if (pool["id"], window["window"]) not in matched:
                    limits.append(entry(f"reported-{pool['id']}-{window['window']}",
                        f"{pool.get('label', pool['id'])} · {window['window']}", window["window_min"], pool, window))
        groups.append({"provider": provider, "label": title, "limits": limits})
    return groups


def window_view(history: list[Observation], now: datetime, reserve: float, baseline: tuple[datetime, float] | None = None) -> dict:
    last = history[-1]
    used = last.used_pct
    expired = last.resets_at is not None and now >= last.resets_at
    valid_until = last.observed_at + timedelta(seconds=freshness_seconds(last.source))
    freshness = "fresh" if now < valid_until else "stale"
    if used is None:
        freshness = "unknown"
    active = used is not None and last.resets_at is not None and not expired
    remaining = max(0.0, 100 - used) if active else None
    usable = max(0.0, 100 - reserve - used) if active else None
    allowance = "unknown"
    if active:
        allowance = "exhausted" if used >= 100 else "reserve_reached" if usable == 0 else "available"
    hours_left = max(0.0, (last.resets_at - now).total_seconds() / 3600) if active else 0
    whole = recent = conservative = runway = exhaust_at = None
    if active and freshness == "fresh":
        start = last.resets_at - timedelta(minutes=last.window_min)
        base_ts, base_used = baseline or (start, 0.0)
        elapsed = (last.observed_at - base_ts).total_seconds() / 3600
        points = [p for p in history if p.used_pct is not None and p.observed_at >= last.observed_at - timedelta(hours=1)]
        if len(points) >= 3:
            span = (last.observed_at - points[0].observed_at).total_seconds() / 3600
            if span >= 0.25:
                recent = max(0.0, (used - points[0].used_pct) / span)
                if elapsed >= 0.25:
                    whole = max(0.0, (used - base_used) / elapsed)
        rates = [r for r in (whole, recent) if r is not None]
        conservative = max(rates) if rates else None
        if conservative is not None and conservative > 0:
            # Charge the unobserved interval at the same conservative rate.
            age_hours = max(0, (now - last.observed_at).total_seconds() / 3600)
            runway = max(0.0, usable / conservative - age_hours) * 60
            exhaust_at = iso(now + timedelta(hours=max(0.0, remaining / conservative - age_hours)))
        elif usable == 0:
            runway = 0.0
    return {
        "window": last.window, "window_min": last.window_min,
        "used_pct": used, "remaining_pct": remaining, "usable_pct": usable,
        "resets_at": iso(last.resets_at) if last.resets_at else None,
        "reset_provenance": last.reset_provenance,
        "observation_time_provenance": last.observation_time_provenance,
        "observed_at": iso(last.observed_at), "received_at": iso(last.received_at),
        "source": last.source, "valid_until": iso(valid_until),
        "freshness_ttl_s": freshness_seconds(last.source),
        "source_age_s": max(0, (now - last.observed_at).total_seconds()),
        "upstream_reporting_delay_s": None,
        "freshness": freshness, "allowance_state": allowance,
        "whole_window_rate_pph": whole, "recent_rate_pph": recent,
        "conservative_rate_pph": conservative,
        "sustainable_rate_pph": usable / hours_left if hours_left and freshness == "fresh" else None,
        "runway_minutes": runway, "exhaust_at": exhaust_at,
    }


class CapacityState:
    """One writer publishes immutable snapshots; HTTP readers only copy cached state."""

    def __init__(self, home: Path, clock=now_utc):
        self.home, self.clock = home, clock
        self.instance_id = str(uuid.uuid4())
        self.condition = threading.Condition()
        self.history: dict[tuple[str, str], deque] = {}
        self.baselines: dict[tuple[str, str], tuple[datetime, float]] = {}
        self.models: dict[str, set[str]] = {}
        self.active_accounts: dict[str, str] = {}
        self.accepted: list[Observation] = []
        self.health = {p: {"state": "starting", "last_error": None} for p in ("codex", "claude", "antigravity")}
        self.health["antigravity"] = {"state": "unsupported", "last_error": None, "detail": "token activity only; quota not reported"}
        self.revision = 0
        self.snapshot: dict = {}
        self.reserve_pct = 10
        self._policy_mtime = None
        saved = read_json(home / "capacity-state.json", {})
        if isinstance(saved, dict):
            self.active_accounts = saved.get("active_accounts", {})
        for entry in saved.get("windows", []) if isinstance(saved, dict) else []:
            try:
                items = [Observation.from_dict(v) for v in entry["history"]]
                items = [i for i in items if valid_observation(i, self.clock())]
                if items:
                    self.history[items[-1].key] = deque(items, maxlen=1024)
                    self.models.setdefault(items[-1].pool_key, set()).update(entry.get("models", []))
                    baseline = entry.get("baseline")
                    if baseline and parse_iso(baseline[0]):
                        self.baselines[items[-1].key] = (parse_iso(baseline[0]), float(baseline[1]))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        self.restored = bool(self.history)
        self.publish()

    def ingest(self, items: list[Observation]) -> bool:
        now = self.clock()
        items = [i for i in items if valid_observation(i, now)]
        changed = False
        self.accepted = []
        # A complete native reading reconciles even windows recovered from disk.
        full = {(i.provider, i.account_scope) for i in items if i.complete_snapshot}
        for provider, account in full:
            newest = max(i.observed_at for i in items if (i.provider, i.account_scope) == (provider, account))
            known = [h[-1] for h in self.history.values() if h[-1].provider == provider]
            if known and newest < max(i.observed_at for i in known):
                continue
            if self.active_accounts.get(provider) != account:
                self.active_accounts[provider] = account
                changed = True
            present = {i.key for i in items if (i.provider, i.account_scope) == (provider, account)}
            if any(i.limit_id != "unreported" for i in items if i.provider == provider):
                for key in list(self.history):
                    if self.history[key][-1].provider == provider and self.history[key][-1].limit_id == "unreported":
                        del self.history[key]
                known = [i for i in known if i.limit_id != "unreported"]
            for prior in known:
                if prior.account_scope == account and prior.key not in present and prior.used_pct is not None:
                    items.append(replace(prior, used_pct=None, resets_at=None, observed_at=newest,
                                         received_at=self.clock(), reset_provenance="unknown",
                                         observation_id="", complete_snapshot=False))
        for item in items:
            models = self.models.setdefault(item.pool_key, set())
            if not set(item.models).issubset(models):
                models.update(item.models)
                changed = True
            history = self.history.setdefault(item.key, deque(maxlen=1024))
            if history:
                prior = history[-1]
                if item.observed_at < prior.observed_at:
                    continue
                if item.observation_id and item.observation_id == prior.observation_id:
                    continue
                if downgrades_reading(item, prior, now):
                    continue
                if item.observed_at == prior.observed_at:
                    if SOURCE_RANK.get(item.source, 0) <= SOURCE_RANK.get(prior.source, 0):
                        continue
                    history.pop()
                reset_changed = item.resets_at != prior.resets_at and (
                    item.resets_at is None or prior.resets_at is None or abs((item.resets_at - prior.resets_at).total_seconds()) > 180
                )
                corrected = item.used_pct is not None and prior.used_pct is not None and item.used_pct < prior.used_pct
                if reset_changed or corrected:
                    history.clear()
                    self.baselines.pop(item.key, None)
                    if item.used_pct is not None:
                        self.baselines[item.key] = (item.observed_at, item.used_pct)
            history.append(item)
            self.accepted.append(item)
            changed = True
        return changed

    def set_health(self, provider: str, state: str, error: str | None = None):
        prior = self.health.get(provider, {})
        if prior.get("state") != state or prior.get("last_error") != error:
            self.health[provider] = {"state": state, "last_error": error, "changed_at": iso(self.clock())}

    def set_policy(self, reserve: int) -> None:
        if isinstance(reserve, bool) or reserve not in (5, 10, 20):
            raise ValueError("reserve_pct must be 5, 10, or 20")
        atomic_write_text(self.home / "capacity-policy.json", json.dumps({"reserve_pct": reserve}))

    def _read_policy(self):
        raw = read_json(self.home / "capacity-policy.json", {})
        reserve = raw.get("reserve_pct", 10) if isinstance(raw, dict) else 10
        self.reserve_pct = reserve if type(reserve) is int and reserve in (5, 10, 20) else 10

    def publish(self, persist: bool = False) -> dict:
        self._read_policy()
        now = self.clock()
        pools: dict[str, dict] = {}
        for key, history in sorted(self.history.items()):
            last = history[-1]
            if last.provider in self.active_accounts and last.account_scope != self.active_accounts[last.provider]:
                continue
            pool = pools.setdefault(last.pool_key, {
                "id": last.pool_key, "provider": last.provider, "account_scope": last.account_scope,
                "limit_id": last.limit_id, "label": last.limit_id,
                "models": sorted(self.models.get(last.pool_key, ())),
                "mapping_confidence": "observed" if self.models.get(last.pool_key) else "unknown", "windows": [],
                "account_scope_confidence": "reported" if last.provider == "codex" else "unverified_local_scope",
                "limit_id_provenance": "reported" if last.provider == "codex" else "adapter_window_group",
            })
            pool["windows"].append(window_view(list(history), now, self.reserve_pct, self.baselines.get(key)))
        if not any(p["provider"] == "antigravity" for p in pools.values()):
            pools["antigravity:local:unknown"] = {
                "id": "antigravity:local:unknown", "provider": "antigravity", "account_scope": "local",
                "limit_id": "unknown", "label": "Quota not reported", "models": [],
                "mapping_confidence": "unknown", "windows": [],
            }
        for pool in pools.values():
            windows = pool["windows"]
            states = {w["allowance_state"] for w in windows}
            pool["allowance_state"] = next((s for s in ("exhausted", "unknown", "reserve_reached", "available") if s in states), "unknown")
            fresh = {w["freshness"] for w in windows}
            pool["freshness"] = "unknown" if not fresh or "unknown" in fresh else "stale" if "stale" in fresh else "fresh"
            ranked = sorted(windows, key=lambda w: (
                {"exhausted": 0, "reserve_reached": 1, "unknown": 2, "available": 3}[w["allowance_state"]],
                w["runway_minutes"] if w["runway_minutes"] is not None else float("inf"),
                w["usable_pct"] if w["usable_pct"] is not None else float("inf"),
            ))
            pool["constraining_window"] = ranked[0]["window"] if ranked else None
        with self.condition:
            candidate = {
                "schema_version": 1, "instance_id": self.instance_id, "revision": self.revision + 1,
                "generated_at": iso(now), "collector_health": copy.deepcopy(self.health),
                "policy": {"reserve_pct": self.reserve_pct, "presets": [5, 10, 20]},
                "pools": list(pools.values()), "unreported_in_flight_usage": "unknown",
                "provider_groups": provider_groups(list(pools.values())),
                "service_state": "running", "restored_from_disk": self.restored,
                "provider_states": {p: "observed" if any(v["provider"] == p and v["windows"] for v in pools.values()) else "unknown"
                                    for p in ("codex", "claude", "antigravity")},
            }
            def signature(value):
                result = copy.deepcopy(value)
                # Grouped display is entirely derived from pools plus fixed slot labels.
                for k in ("generated_at", "revision", "provider_groups"):
                    result.pop(k, None)
                for pool in result.get("pools", []):
                    for window in pool["windows"]:
                        for k in ("source_age_s", "runway_minutes", "sustainable_rate_pph", "exhaust_at"):
                            window.pop(k, None)
                return result
            if signature(candidate) != signature(self.snapshot):
                self.revision += 1
                self.snapshot = candidate
                self.condition.notify_all()
            snapshot = self.snapshot
        if persist:
            entries = [{"history": [i.to_dict() for i in h], "models": sorted(self.models.get(h[-1].pool_key, ())),
                        "baseline": [iso(self.baselines[k][0]), self.baselines[k][1]] if k in self.baselines else None}
                       for k, h in self.history.items()]
            atomic_write_text(self.home / "capacity-state.json", json.dumps({"windows": entries, "active_accounts": self.active_accounts}, separators=(",", ":")))
            atomic_write_text(self.home / "capacity.json", json.dumps(snapshot, separators=(",", ":")))
        return snapshot

    def read(self) -> dict:
        with self.condition:
            return copy.deepcopy(self.snapshot)

    def wait(self, revision: int, timeout: float = 15) -> dict:
        with self.condition:
            self.condition.wait_for(lambda: self.revision != revision, timeout=timeout)
            return copy.deepcopy(self.snapshot)
