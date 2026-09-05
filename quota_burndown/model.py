"""Burndown math: where you are versus a linear burn of the window's budget."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from .store import Sample

ON_PACE_BAND = 2.0          # percentage points either side of pace that still count as on pace
TOO_EARLY_FRACTION = 0.03   # projections need at least this fraction of the window elapsed
RESET_TOLERANCE_S = 180     # readings whose reset times differ by less than this describe the same window
HIDE_EXPIRED_AFTER_MIN = 1440  # drop windows that ended more than a day ago from the current view
PROVIDER_ORDER = {"claude": 0, "codex": 1}


def same_window(a: datetime, b: datetime) -> bool:
    """Providers jitter the reset time by a second or two between readings, and the Claude
    endpoint sometimes rounds it to the minute. Treat nearby reset times as one window."""
    return abs((a - b).total_seconds()) <= RESET_TOLERANCE_S


@dataclass
class WindowInstance:
    key: str
    provider: str
    window: str
    window_min: int
    resets_at: datetime
    samples: list[Sample] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.resets_at - timedelta(minutes=self.window_min)


@dataclass
class Burndown:
    provider: str
    window: str
    window_min: int
    now: datetime
    used: float
    status: str  # over | under | on-pace | early | exhausted | expired | idle
    resets_at: datetime | None = None
    start: datetime | None = None
    pace: float = 0.0
    delta: float = 0.0
    elapsed_min: float = 0.0
    remaining_min: float = 0.0
    rate_per_hour: float = 0.0
    projected_end: float = 0.0
    exhaust_at: datetime | None = None
    sample_ts: datetime | None = None
    source: str = ""
    samples: list[Sample] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.window}"

    @property
    def remaining_pct(self) -> float:
        return max(0.0, 100.0 - self.used)

    @property
    def exhausts_before_reset(self) -> bool:
        return bool(self.exhaust_at and self.resets_at and self.exhaust_at < self.resets_at)

    @property
    def age_min(self) -> float | None:
        if self.sample_ts is None:
            return None
        return (self.now - self.sample_ts).total_seconds() / 60


def sort_key(bd: Burndown) -> tuple:
    return (PROVIDER_ORDER.get(bd.provider, 9), bd.window_min, bd.window)


def canonical_sample(sample: Sample) -> Sample | None:
    """Fold legacy per-version Codex keys into the shared standard Codex allowance pool.

    Older collectors stored numeric GPT versions independently (for example
    ``7d:gpt-5.6`` and ``7d:gpt-6``), even though they report the same limit. Preserve the
    append-only store and normalize those readings at the model boundary.
    """
    if sample.provider != "codex":
        return sample
    base, separator, scope = sample.window.partition(":")
    scope_lower = scope.lower()
    if scope_lower == "codex_bengalfox":
        aliases = {300: "5h:spark", 10080: "7d:spark"}
        return replace(sample, window=aliases[sample.window_min]) if sample.window_min in aliases else sample
    if scope_lower == "codex" and sample.window_min == 10080:
        return replace(sample, window="7d:codex")
    version = scope_lower.removeprefix("gpt-")
    if separator and scope_lower.startswith("gpt-") and version.replace(".", "").isdigit():
        if sample.window_min != 10080:
            return None
        return replace(sample, window=f"{base}:codex")
    return sample


def group_instances(samples: list[Sample]) -> dict[str, list[WindowInstance]]:
    """Cluster samples per window key into instances by reset time, oldest instance first."""
    by_key: dict[str, list[Sample]] = {}
    for sample in samples:
        if sample.resets_at is None or sample.window_min <= 0:
            continue
        by_key.setdefault(sample.key, []).append(sample)
    grouped: dict[str, list[WindowInstance]] = {}
    for key, items in by_key.items():
        items.sort(key=lambda s: (s.resets_at, s.ts))
        instances: list[WindowInstance] = []
        for sample in items:
            if instances and same_window(sample.resets_at, instances[-1].resets_at):
                instances[-1].samples.append(sample)
            else:
                instances.append(WindowInstance(key, sample.provider, sample.window, sample.window_min, sample.resets_at, [sample]))
        for inst in instances:
            inst.samples.sort(key=lambda s: s.ts)
            inst.window_min = inst.samples[-1].window_min
        grouped[key] = instances
    return grouped


def find_instance(instances: list[WindowInstance], resets_at: datetime) -> WindowInstance | None:
    for inst in instances:
        if same_window(inst.resets_at, resets_at):
            return inst
    return None


def compute(inst: WindowInstance, now: datetime) -> Burndown:
    last = inst.samples[-1]
    total = float(inst.window_min)
    elapsed = max((now - inst.start).total_seconds() / 60, 0.0)
    expired = now >= inst.resets_at
    elapsed_c = min(elapsed, total)
    remaining = max(total - elapsed, 0.0)
    pace = 100.0 * elapsed_c / total
    used = last.used
    delta = used - pace
    too_early = elapsed_c < TOO_EARLY_FRACTION * total
    rate = used / (elapsed_c / 60) if elapsed_c > 0 else 0.0
    projected_end = used if expired else used + rate * (remaining / 60)

    if expired:
        status = "expired"
    elif used >= 100:
        status = "exhausted"
    elif too_early:
        status = "early"
    elif delta > ON_PACE_BAND:
        status = "over"
    elif delta < -ON_PACE_BAND:
        status = "under"
    else:
        status = "on-pace"

    exhaust_at = None
    if not expired and used < 100 and rate > 0:
        exhaust_at = now + timedelta(hours=(100 - used) / rate)

    return Burndown(
        provider=inst.provider,
        window=inst.window,
        window_min=inst.window_min,
        now=now,
        used=used,
        status=status,
        resets_at=inst.resets_at,
        start=inst.start,
        pace=pace,
        delta=delta,
        elapsed_min=elapsed_c,
        remaining_min=remaining,
        rate_per_hour=rate,
        projected_end=projected_end,
        exhaust_at=exhaust_at,
        sample_ts=last.ts,
        source=last.source,
        samples=list(inst.samples),
    )


def idle(sample: Sample, now: datetime) -> Burndown:
    return Burndown(sample.provider, sample.window, sample.window_min, now, sample.used, "idle", sample_ts=sample.ts, source=sample.source, samples=[sample])


def current(samples: list[Sample], latest: dict[str, Sample], now: datetime) -> list[Burndown]:
    """One burndown per window key, built from the latest reading and that window's history."""
    samples = [normalized for sample in samples if (normalized := canonical_sample(sample)) is not None]
    canonical_latest: dict[str, Sample] = {}
    for sample in latest.values():
        sample = canonical_sample(sample)
        if sample is None:
            continue
        prior = canonical_latest.get(sample.key)
        if prior is None or sample.ts >= prior.ts:
            canonical_latest[sample.key] = sample
    latest = canonical_latest
    grouped = group_instances(samples)
    out: list[Burndown] = []
    for key, last in latest.items():
        if last.resets_at is None or last.window_min <= 0:
            out.append(idle(last, now))
            continue
        if (now - last.resets_at).total_seconds() / 60 > HIDE_EXPIRED_AFTER_MIN:
            continue
        inst = find_instance(grouped.get(key, []), last.resets_at)
        if inst is None:
            inst = WindowInstance(key, last.provider, last.window, last.window_min, last.resets_at, [last])
        elif inst.samples[-1].ts < last.ts:
            inst.samples.append(last)
        out.append(compute(inst, now))
    out.sort(key=sort_key)
    return out


def from_latest(latest: dict[str, Sample], now: datetime) -> list[Burndown]:
    """Fast path for the status line: no history, only the latest reading per window."""
    return current([], latest, now)
