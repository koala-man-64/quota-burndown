"""Data behind each window's usage chart: dates and percentages only, no markup, no pixels.

One chart per window spans exactly two allowance intervals, ending at the active
window's reset or at now when no active window is known. Every window instance (one 5-hour
session, one 7-day period) is its own segment; segments never join across a reset, and
past instances end at their last reading instead of being held flat.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import default_home
from .model import Burndown, canonical_samples, find_instance, group_instances
from .store import Sample
from .util import fmt_local, parse_iso, read_json, to_local

TARGET_POINTS = 300
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
    reset_pace: list[Line] = field(default_factory=list)
    reset_projections: list[Line] = field(default_factory=list)
    reset_markers: list[tuple[datetime, float, str]] = field(default_factory=list)
    credit_expiries: list[ResetMark] = field(default_factory=list)
    reset_credits_count: int = 0
    next_reset_time: datetime | None = None
    runway_with_resets_min: float | None = None
    x_ticks: list[Tick] = field(default_factory=list)
    y_ticks: tuple[int, ...] = (0, 25, 50, 75, 100)
    span_key: str = ""
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


def default_span_key(window_min: int) -> str:
    minutes = 2 * window_min
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def span_for(window_min: int) -> timedelta:
    return timedelta(minutes=2 * window_min)


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


def x_ticks(span_start: datetime, span_end: datetime, span: timedelta) -> list[Tick]:
    """Clock-aligned local ticks with spacing suited to the exact plotted domain."""
    local_start = to_local(span_start)
    if span <= timedelta(hours=12):
        step, fmt, hours = timedelta(hours=2), "%H:%M", 2
    elif span <= timedelta(hours=36):
        step, fmt, hours = timedelta(hours=6), "%H:%M", 6
    elif span <= timedelta(days=4):
        step, fmt, hours = timedelta(hours=12), "%a %H:%M", 12
    elif span <= timedelta(days=8):
        step, fmt, hours = timedelta(days=1), "%a %d", 24
    elif span <= timedelta(days=16):
        step, fmt, hours = timedelta(days=2), "%d %b", 48
    else:
        step, fmt, hours = timedelta(days=5), "%d %b", 120
    first = local_start.replace(minute=0, second=0, microsecond=0)
    if hours >= 24:
        first = first.replace(hour=0)
    while first < local_start or (hours < 24 and first.hour % hours):
        first += timedelta(hours=1) if hours < 24 else timedelta(days=1)
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



def load_credits_for(bd: Burndown, now: datetime) -> list[dict]:
    """Retrieve and normalize unexpired reset credits applicable to this burndown."""
    candidates = list(bd.reset_credits)
    if not candidates:
        try:
            home = default_home()
            cap = read_json(home / "capacity.json", {})
            if isinstance(cap, dict):
                for pool in cap.get("pools", []):
                    if isinstance(pool, dict) and pool.get("provider") == bd.provider:
                        limit_id = pool.get("limit_id") or ""
                        if limit_id in bd.window or bd.provider == "codex":
                            candidates = list(pool.get("reset_credits", []))
                            if candidates:
                                break
            if not candidates:
                res_cr = read_json(home / "reset_credits.json", {})
                if isinstance(res_cr, dict):
                    raw = res_cr.get(bd.provider) or res_cr.get(f"{bd.provider}:{bd.window}") or []
                    if isinstance(raw, list):
                        candidates = list(raw)
            if not candidates:
                pol = read_json(home / "capacity-policy.json", {})
                if isinstance(pol, dict):
                    raw = pol.get("reset_credits", {}).get(bd.provider, [])
                    if isinstance(raw, list):
                        candidates = list(raw)
        except Exception:
            pass

    out: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        c = dict(item)
        if c.get("status") not in (None, "available"):
            continue
        exp = c.get("expires_at")
        if isinstance(exp, str):
            exp = parse_iso(exp)
            c["expires_at"] = exp
        if exp is not None and exp <= now:
            continue
        out.append(c)

    out.sort(key=lambda c: (c.get("expires_at") is None, c.get("expires_at")))
    return out


def build(bd: Burndown, samples: list[Sample], now: datetime, span_key: str | None = None, credits: list[dict] | None = None) -> ChartData | None:
    """Chart data for one window from its burndown and its sample history (the burndown's
    own samples are merged in). Legacy `span_key` arguments cannot override the fixed
    two-cycle domain. None when the interval is unknown or there is nothing to draw."""
    if bd.window_min <= 0:
        return None
    span_key = default_span_key(bd.window_min)
    span = span_for(bd.window_min)
    active = (bd.status in ACTIVE_STATUSES and bd.start is not None
              and bd.resets_at is not None and bd.resets_at > now)
    span_end = bd.resets_at if active else now
    span_start = span_end - span
    # Apply the same provider normalization as the burndown selection. In particular,
    # Spark's rolling idle sentinel must not be reconstructed as dozens of windows here.
    history = _dedupe(canonical_samples([s for s in samples if s.key == bd.key] + list(bd.samples)))
    all_instances = group_instances(history).get(bd.key, [])
    instances = [
        inst for inst in all_instances
        if inst.samples and inst.samples[-1].ts >= span_start and inst.samples[0].ts <= span_end
    ]
    current_inst = find_instance(instances, bd.resets_at) if active else None
    width = span / TARGET_POINTS

    segments: list[Segment] = []
    resets: list[ResetMark] = []
    for inst in instances:
        inside = [Point(s.ts, s.used) for s in inst.samples if span_start <= s.ts <= span_end]
        earlier = [s for s in inst.samples if s.ts < span_start]
        if earlier:
            inside.insert(0, Point(span_start, earlier[-1].used))
        if inside:
            segments.append(Segment(bucket(inside, width), inst is current_inst))
    # A reset on a domain boundary remains visible even when the preceding
    # instance's final reading is outside the plotted domain.
    resets = [ResetMark(inst.resets_at, inst is current_inst) for inst in all_instances
              if span_start <= inst.resets_at <= span_end]
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
    reset_pace: list[Line] = []
    reset_projections: list[Line] = []
    reset_markers: list[tuple[datetime, float, str]] = []
    credit_expiries: list[ResetMark] = []
    next_reset_time: datetime | None = None
    runway_with_resets_min: float | None = None

    if credits is None:
        credits = load_credits_for(bd, now)
    else:
        parsed = []
        for c in credits:
            cd = dict(c)
            if isinstance(cd.get("expires_at"), str):
                cd["expires_at"] = parse_iso(cd["expires_at"])
            parsed.append(cd)
        credits = sorted(parsed, key=lambda c: (c.get("expires_at") is None, c.get("expires_at")))

    k = len(credits)
    for c in credits:
        exp = c.get("expires_at")
        if exp is not None and span_start <= exp <= span_end:
            credit_expiries.append(ResetMark(exp, False))

    if active:
        pace = Line((bd.start, 0.0), (bd.resets_at, 100.0))
        if k > 0 and bd.start is not None and bd.resets_at is not None:
            duration = bd.resets_at - bd.start
            dt = duration / (k + 1)
            t_cur = bd.start
            for i in range(k):
                t_next = bd.start + (i + 1) * dt
                exp = credits[i].get("expires_at")
                if exp is not None and t_next > exp:
                    t_next = max(t_cur + timedelta(minutes=1), exp)
                reset_pace.append(Line((t_cur, 0.0), (t_next, 100.0)))
                reset_pace.append(Line((t_next, 100.0), (t_next, 0.0)))
                t_cur = t_next
            reset_pace.append(Line((t_cur, 0.0), (bd.resets_at, 100.0)))

        if bd.status in PROJECTED_STATUSES and bd.rate_per_hour > 0:
            if bd.exhausts_before_reset and bd.exhaust_at is not None:
                end = (bd.exhaust_at, 100.0)
            else:
                end = (bd.resets_at, min(bd.projected_end, 100.0))
            projection = Line((now, bd.used), end)

            if k > 0:
                r = bd.rate_per_hour
                points_avail = (100.0 - bd.used) + k * 100.0
                runway_with_resets_min = (points_avail / r) * 60.0

                t_cur = now
                u_cur = bd.used
                rem_credits = list(credits)
                credit_idx = 1

                while t_cur < bd.resets_at:
                    h_to_100 = (100.0 - u_cur) / r
                    t_exhaust = t_cur + timedelta(hours=h_to_100)

                    if t_exhaust < bd.resets_at and rem_credits:
                        c = rem_credits.pop(0)
                        exp = c.get("expires_at")
                        if exp is not None and t_exhaust > exp:
                            t_use = max(t_cur, exp)
                            u_use = min(100.0, u_cur + (t_use - t_cur).total_seconds() / 3600.0 * r)
                            reset_projections.append(Line((t_cur, u_cur), (t_use, u_use)))
                            reset_projections.append(Line((t_use, u_use), (t_use, 0.0)))
                            label = f"Reset {credit_idx} ({u_use:.0f}% pre-expiry)"
                            reset_markers.append((t_use, u_use, label))
                            if next_reset_time is None:
                                next_reset_time = t_use
                            t_cur, u_cur = t_use, 0.0
                        else:
                            reset_projections.append(Line((t_cur, u_cur), (t_exhaust, 100.0)))
                            reset_projections.append(Line((t_exhaust, 100.0), (t_exhaust, 0.0)))
                            label = f"Reset {credit_idx} (100% capacity)"
                            reset_markers.append((t_exhaust, 100.0, label))
                            if next_reset_time is None:
                                next_reset_time = t_exhaust
                            t_cur, u_cur = t_exhaust, 0.0
                        credit_idx += 1
                    else:
                        if rem_credits:
                            c = rem_credits[0]
                            exp = c.get("expires_at")
                            if exp is not None and t_cur < exp < bd.resets_at:
                                rem_credits.pop(0)
                                t_use = exp
                                u_use = min(100.0, u_cur + (t_use - t_cur).total_seconds() / 3600.0 * r)
                                reset_projections.append(Line((t_cur, u_cur), (t_use, u_use)))
                                reset_projections.append(Line((t_use, u_use), (t_use, 0.0)))
                                label = f"Reset {credit_idx} ({u_use:.0f}% pre-expiry)"
                                reset_markers.append((t_use, u_use, label))
                                if next_reset_time is None:
                                    next_reset_time = t_use
                                t_cur, u_cur = t_use, 0.0
                                credit_idx += 1
                                continue
                        u_end = min(100.0, u_cur + (bd.resets_at - t_cur).total_seconds() / 3600.0 * r)
                        reset_projections.append(Line((t_cur, u_cur), (bd.resets_at, u_end)))
                        break

    return ChartData(
        key=bd.key, provider=bd.provider, window=bd.window,
        span_start=span_start, span_end=span_end, now=now,
        segments=segments, resets=resets, pace=pace, projection=projection,
        reset_pace=reset_pace, reset_projections=reset_projections,
        reset_markers=reset_markers, credit_expiries=credit_expiries,
        reset_credits_count=k, next_reset_time=next_reset_time,
        runway_with_resets_min=runway_with_resets_min,
        x_ticks=x_ticks(span_start, span_end, span),
        span_key=span_key, span_label=f"two cycles ({span_key})",
    )


def local_label(ts: datetime) -> str:
    return fmt_local(ts, "%a %H:%M")
