"""Data behind each window's usage chart: dates and percentages only, no markup, no pixels.

One chart per window shows % used rising over time: the last 24 hours for windows up to a
day long, the last 7 days for longer ones, extended to the right as far as the current
window's reset so its pace line and projection fit. Every window instance (one 5-hour
session, one 7-day period) is its own segment; segments never join across a reset, and
past instances end at their last reading instead of being held flat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .model import Burndown, find_instance, group_instances
from .store import Sample
from .util import fmt_local, to_local

TARGET_POINTS = 300
SHORT_SPAN = timedelta(hours=24)
LONG_SPAN = timedelta(days=7)
SHORT_SPAN_MAX_MIN = 1440
ACTIVE_STATUSES = ("over", "under", "on-pace", "early", "exhausted")
PROJECTED_STATUSES = ("over", "under", "on-pace")


@dataclass(frozen=True)
class Point:
    ts: datetime
    used: float


@dataclass
class Segment:
    points: list[Point]
    current: bool


@dataclass(frozen=True)
class ResetMark:
    ts: datetime
    current: bool


@dataclass(frozen=True)
class Line:
    start: tuple[datetime, float]
    end: tuple[datetime, float]


@dataclass(frozen=True)
class Tick:
    ts: datetime
    label: str


@dataclass
class ChartData:
    key: str
    provider: str
    window: str
    span_start: datetime
    span_end: datetime
    now: datetime
    segments: list[Segment] = field(default_factory=list)
    resets: list[ResetMark] = field(default_factory=list)
    pace: Line | None = None
    projection: Line | None = None
    x_ticks: list[Tick] = field(default_factory=list)
    y_ticks: tuple[int, ...] = (0, 25, 50, 75, 100)
    span_label: str = ""

    @property
    def active(self) -> bool:
        return self.pace is not None

    @property
    def end_point(self) -> Point | None:
        for segment in self.segments:
            if segment.current and segment.points:
                return segment.points[-1]
        return None


def span_for(window_min: int) -> timedelta:
    return SHORT_SPAN if window_min <= SHORT_SPAN_MAX_MIN else LONG_SPAN


def span_label_for(window_min: int) -> str:
    return "last 24 hours" if window_min <= SHORT_SPAN_MAX_MIN else "last 7 days"


def bucket(points: list[Point], width: timedelta) -> list[Point]:
    """At most one point per bucket of `width`, keeping the highest reading in each bucket
    (used % only rises inside one window, so the peak is also the latest value). The first
    point is always kept so a segment starts where its data starts."""
    if len(points) <= 2 or width <= timedelta(0):
        return list(points)
    out = [points[0]]
    origin = points[0].ts
    index: int | None = None
    best: Point | None = None
    for point in points[1:]:
        slot = int((point.ts - origin) / width)
        if slot != index:
            if best is not None:
                out.append(best)
            index, best = slot, point
        elif point.used >= best.used:
            best = point
    if best is not None:
        out.append(best)
    return out


def x_ticks(span_start: datetime, span_end: datetime, window_min: int) -> list[Tick]:
    """Clock-aligned local ticks: every 6 hours for the short span, every local midnight for
    the long one."""
    local_start = to_local(span_start)
    if window_min <= SHORT_SPAN_MAX_MIN:
        step = timedelta(hours=6)
        first = local_start.replace(minute=0, second=0, microsecond=0)
        while first.hour % 6 or first < local_start:
            first += timedelta(hours=1)
        fmt = "%H:%M"
    else:
        step = timedelta(days=1)
        first = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
        if first < local_start:
            first += timedelta(days=1)
        fmt = "%a %d"
    ticks: list[Tick] = []
    stamp = first
    while stamp <= to_local(span_end):
        ticks.append(Tick(stamp.astimezone(span_start.tzinfo), stamp.strftime(fmt)))
        stamp += step
    return ticks


def _dedupe(samples: list[Sample]) -> list[Sample]:
    seen: set[tuple] = set()
    out: list[Sample] = []
    for sample in sorted(samples, key=lambda s: s.ts):
        ident = (sample.key, sample.ts, sample.used, sample.resets_at)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(sample)
    return out


def build(bd: Burndown, samples: list[Sample], now: datetime) -> ChartData | None:
    """Chart data for one window from its burndown and its sample history (the burndown's
    own samples are merged in, so the latest reading is always present). None when there
    is nothing to draw."""
    span = span_for(bd.window_min)
    span_start = now - span
    active = bd.status in ACTIVE_STATUSES and bd.start is not None and bd.resets_at is not None
    span_end = max(now, bd.resets_at) if active else now
    history = _dedupe([s for s in samples if s.key == bd.key] + list(bd.samples))
    instances = [
        inst for inst in group_instances(history).get(bd.key, [])
        if inst.samples and inst.samples[-1].ts >= span_start and inst.samples[0].ts <= span_end
    ]
    current_inst = find_instance(instances, bd.resets_at) if active else None
    width = span / TARGET_POINTS

    segments: list[Segment] = []
    resets: list[ResetMark] = []
    for inst in instances:
        inside = [Point(s.ts, s.used) for s in inst.samples if s.ts >= span_start]
        earlier = [s for s in inst.samples if s.ts < span_start]
        if earlier:
            inside.insert(0, Point(span_start, earlier[-1].used))
        if inside:
            segments.append(Segment(bucket(inside, width), inst is current_inst))
        if span_start <= inst.resets_at <= span_end:
            resets.append(ResetMark(inst.resets_at, inst is current_inst))
    # Readings with no reset time (idle 0 % readings, or a weekly reading before any reset is
    # known) belong to no instance; draw them too, as runs split at long gaps.
    loose = [Point(s.ts, s.used) for s in history if s.resets_at is None and span_start <= s.ts <= span_end]
    gap = span / 24
    run: list[Point] = []
    for point in loose:
        if run and point.ts - run[-1].ts > gap:
            segments.append(Segment(bucket(run, width), False))
            run = []
        run.append(point)
    if run:
        segments.append(Segment(bucket(run, width), False))
    segments.sort(key=lambda seg: seg.points[0].ts)
    if not segments:
        return None

    pace = projection = None
    if active:
        pace = Line((bd.start, 0.0), (bd.resets_at, 100.0))
        if bd.status in PROJECTED_STATUSES and bd.rate_per_hour > 0:
            if bd.exhausts_before_reset and bd.exhaust_at is not None:
                end = (bd.exhaust_at, 100.0)
            else:
                end = (bd.resets_at, min(bd.projected_end, 100.0))
            projection = Line((now, bd.used), end)

    return ChartData(
        key=bd.key, provider=bd.provider, window=bd.window,
        span_start=span_start, span_end=span_end, now=now,
        segments=segments, resets=resets, pace=pace, projection=projection,
        x_ticks=x_ticks(span_start, span_end, bd.window_min),
        span_label=span_label_for(bd.window_min),
    )


def local_label(ts: datetime) -> str:
    return fmt_local(ts, "%a %H:%M")
