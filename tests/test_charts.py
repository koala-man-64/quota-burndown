from datetime import datetime, timedelta, timezone

from quota_burndown import charts
from quota_burndown.model import current
from quota_burndown.store import Sample
from quota_burndown.util import to_local

UTC = timezone.utc
NOW = datetime(2026, 9, 4, 3, 0, tzinfo=UTC)


def sample(ts, used, resets_at, window="5h", window_min=300, provider="claude"):
    return Sample(ts, provider, window, used, resets_at, window_min, "desktop")


def five_hour_history():
    """Two finished sessions and a live one over the last 24 hours."""
    out = []
    reset_a = NOW - timedelta(hours=16)
    for m in range(0, 300, 30):
        out.append(sample(reset_a - timedelta(minutes=300 - m), m / 3, reset_a))
    reset_b = NOW - timedelta(hours=7)
    for m in range(0, 300, 15):
        out.append(sample(reset_b - timedelta(minutes=300 - m), m / 5, reset_b))
    reset_c = NOW + timedelta(hours=2)
    for m in range(0, 180, 10):
        out.append(sample(reset_c - timedelta(minutes=300 - m), m / 4, reset_c))
    return out, (reset_a, reset_b, reset_c)


def burndown_for(history, key="claude:5h"):
    latest = {key: max((s for s in history if s.key == key), key=lambda s: s.ts)}
    return current(history, latest, NOW)[0]


def test_bucket_keeps_first_point_and_peaks():
    base = NOW
    points = [charts.Point(base + timedelta(minutes=i), used) for i, used in enumerate([1, 9, 3, 4, 40, 42, 41])]
    out = charts.bucket(points, timedelta(minutes=4))
    assert [p.used for p in out] == [1, 9, 42]
    assert out[1].ts == base + timedelta(minutes=1)  # the peak's own timestamp
    assert charts.bucket(points[:2], timedelta(minutes=4)) == points[:2]
    assert charts.bucket(points, timedelta(0)) == points


def test_segments_never_join_across_a_reset_and_resets_are_marked():
    history, (reset_a, reset_b, reset_c) = five_hour_history()
    data = charts.build(burndown_for(history), history, NOW)
    assert data is not None and data.span_label == "two cycles (10h)"
    assert data.span_start == reset_c - timedelta(hours=10) and data.span_end == reset_c
    assert [seg.current for seg in data.segments] == [False, True]
    assert all(all(b.used >= a.used for a, b in zip(seg.points, seg.points[1:])) for seg in data.segments)
    assert [(r.ts, r.current) for r in data.resets] == [(reset_b, False), (reset_c, True)]
    assert data.end_point is not None and data.end_point.used == 170 / 4
    assert data.pace is not None and data.pace.start == (reset_c - timedelta(minutes=300), 0.0) and data.pace.end == (reset_c, 100.0)
    assert data.projection is not None and data.projection.start == (NOW, 170 / 4)


def test_leading_point_is_carried_in_for_a_clipped_window():
    reset = NOW + timedelta(hours=1)
    history = [sample(NOW - timedelta(hours=30), 5.0, reset), sample(NOW - timedelta(hours=20), 30.0, reset), sample(NOW - timedelta(minutes=5), 60.0, reset)]
    bd = burndown_for(history)
    data = charts.build(bd, history, NOW)
    seg = data.segments[0]
    assert seg.points[0] == charts.Point(data.span_start, 30.0)  # the last reading before the span, pinned to its edge
    assert seg.points[-1].used == 60.0


def test_idle_window_gets_history_only():
    reset = NOW - timedelta(hours=3)
    history = [sample(NOW - timedelta(hours=6), 20.0, reset), sample(NOW - timedelta(hours=4), 80.0, reset)]
    latest = {"claude:5h": sample(NOW - timedelta(minutes=10), 0.0, None)}
    bd = current(history, latest, NOW)[0]
    assert bd.status == "idle"
    data = charts.build(bd, history, NOW)
    assert data is not None and not data.active and data.projection is None and data.span_end == NOW
    assert [seg.current for seg in data.segments] == [False, False]  # the ended window, then the idle 0% reading
    assert [seg.points[-1].used for seg in data.segments] == [80.0, 0.0]
    assert data.resets == [charts.ResetMark(reset, False)]


def test_spark_idle_sentinel_has_no_timer_pace_or_poll_history():
    readings = [
        Sample(NOW - timedelta(minutes=10), "codex", "5h:spark", 0, NOW + timedelta(hours=4, minutes=50), 300, "app-server"),
        Sample(NOW - timedelta(minutes=5), "codex", "5h:spark", 0, NOW + timedelta(hours=4, minutes=55), 300, "app-server"),
        Sample(NOW, "codex", "5h:spark", 0, NOW + timedelta(hours=5), 300, "app-server"),
    ]
    bd = current(readings, {"codex:5h:spark": readings[-1]}, NOW)[0]
    data = charts.build(bd, readings, NOW)
    assert data is not None and not data.active and data.pace is None and data.projection is None
    assert data.resets == []
    assert [point.used for segment in data.segments for point in segment.points] == [0]


def test_projection_geometry_by_status():
    reset = NOW + timedelta(hours=2)
    over = [sample(reset - timedelta(hours=3), 5.0, reset), sample(NOW, 90.0, reset)]
    data = charts.build(burndown_for(over), over, NOW)
    assert data.projection.end[1] == 100.0 and data.projection.end[0] < reset  # exhausts before reset
    under = [sample(reset - timedelta(hours=3), 1.0, reset), sample(NOW, 10.0, reset)]
    data = charts.build(burndown_for(under), under, NOW)
    assert data.projection.end[0] == reset and 10.0 < data.projection.end[1] < 100.0
    exhausted = [sample(NOW, 100.0, reset)]
    data = charts.build(burndown_for(exhausted), exhausted, NOW)
    assert data.pace is not None and data.projection is None
    early = [sample(NOW, 1.0, NOW + timedelta(minutes=299))]
    data = charts.build(burndown_for(early), early, NOW)
    assert data.pace is not None and data.projection is None


def test_ticks_are_clock_aligned_and_follow_the_span():
    ten_hours = charts.x_ticks(NOW - timedelta(hours=10), NOW, timedelta(hours=10))
    assert 5 <= len(ten_hours) <= 6
    assert all(to_local(t.ts).minute == 0 and to_local(t.ts).hour % 2 == 0 for t in ten_hours)
    short = charts.x_ticks(NOW - timedelta(hours=24), NOW + timedelta(hours=2), timedelta(hours=24))
    assert 4 <= len(short) <= 5
    assert all(to_local(t.ts).minute == 0 and to_local(t.ts).hour % 6 == 0 for t in short)
    assert all(len(t.label) == 5 for t in short)
    three = charts.x_ticks(NOW - timedelta(days=3), NOW, timedelta(days=3))
    assert 6 <= len(three) <= 7 and all(to_local(t.ts).hour % 12 == 0 for t in three)
    week = charts.x_ticks(NOW - timedelta(days=7), NOW + timedelta(days=3), timedelta(days=7))
    assert 10 <= len(week) <= 11
    assert all(to_local(t.ts).hour == 0 and to_local(t.ts).minute == 0 for t in week)
    fortnight = charts.x_ticks(NOW - timedelta(days=14), NOW, timedelta(days=14))
    assert 7 <= len(fortnight) <= 8 and all(to_local(t.ts).hour == 0 for t in fortnight)
    month = charts.x_ticks(NOW - timedelta(days=30), NOW, timedelta(days=30))
    assert 6 <= len(month) <= 7 and all(to_local(t.ts).hour == 0 for t in month)
    assert charts.span_for(300) == timedelta(hours=10) and charts.span_for(10080) == timedelta(days=14)
    assert charts.default_span_key(300) == "10h" and charts.default_span_key(10080) == "14d"


def test_legacy_range_arguments_cannot_override_the_two_cycle_domain():
    history, (reset_a, reset_b, reset_c) = five_hour_history()
    bd = burndown_for(history)
    for key in (None, "24h", "3d", "7d", "14d", "30d"):
        data = charts.build(bd, history, NOW, key)
        assert data.span_key == "10h" and data.span_label == "two cycles (10h)"
        assert data.span_start == reset_c - timedelta(hours=10) and data.span_end == reset_c
        assert [seg.current for seg in data.segments][-1] is True
    assert charts.build(bd, history, NOW).span_key == "10h"


def test_weekly_span_and_no_samples():
    reset = NOW + timedelta(days=2)
    history = [sample(NOW - timedelta(days=d), 10.0 * (6 - d), reset, "7d", 10080) for d in range(6, 0, -1)]
    bd = burndown_for(history, "claude:7d")
    data = charts.build(bd, history, NOW)
    assert data.span_key == "14d" and data.span_label == "two cycles (14d)" and data.span_end == reset and len(data.segments) == 1
    assert data.span_end - data.span_start == timedelta(days=14)
    assert charts.build(bd, [], NOW) is not None  # the burndown's own samples still draw
    bare = current([], {"claude:7d": sample(NOW, 5.0, None, "7d", 10080)}, NOW)[0]
    data = charts.build(bare, [], NOW)
    assert data is not None and not data.active and data.segments[0].points[0].used == 5.0 and not data.segments[0].current
    old = current([], {"claude:7d": sample(NOW - timedelta(days=20), 5.0, None, "7d", 10080)}, NOW)[0]
    assert charts.build(old, [], NOW) is None


def test_reset_less_readings_form_runs_split_at_gaps():
    reset = NOW + timedelta(hours=1)
    history = [
        sample(NOW - timedelta(hours=8), 0.0, None), sample(NOW - timedelta(hours=7, minutes=45), 0.0, None),   # idle run
        sample(NOW - timedelta(hours=4), 10.0, reset), sample(NOW - timedelta(minutes=30), 40.0, reset),  # live window
        sample(NOW - timedelta(hours=5), 0.0, None),  # a lone idle reading, hours from the others
    ]
    data = charts.build(burndown_for(history), history, NOW)
    assert [(len(seg.points), seg.current) for seg in data.segments] == [(2, False), (1, False), (2, True)]


def test_two_cycle_domain_for_exhausted_expired_and_unknown_reset_windows():
    for window, minutes in (("5h", 300), ("7d", 10080)):
        for reset, used in ((NOW + timedelta(hours=1), 100), (NOW - timedelta(minutes=10), 50), (None, 0)):
            reading = sample(NOW - timedelta(minutes=20), used, reset, window, minutes)
            bd = current([reading], {reading.key: reading}, NOW)[0]
            data = charts.build(bd, [reading], NOW)
            assert data is not None
            assert data.span_end == (reset if bd.status == "exhausted" else NOW)
            assert data.span_end - data.span_start == timedelta(minutes=2 * minutes)
            assert all(data.span_start <= tick.ts <= data.span_end for tick in data.x_ticks)


def test_domain_clips_samples_and_keeps_reset_marks_at_both_boundaries():
    end = NOW + timedelta(hours=2)
    start = end - timedelta(hours=10)
    history = [sample(start - timedelta(hours=1), 100, start),
               sample(start - timedelta(minutes=30), 5, end),
               sample(start + timedelta(minutes=30), 10, end),
               sample(NOW, 40, end)]
    bd = burndown_for(history)
    future = sample(end + timedelta(hours=1), 90, end)
    all_readings = history + [future]
    data = charts.build(bd, all_readings, NOW)
    assert data.span_start == start and data.span_end == end
    assert data.resets == [charts.ResetMark(start, False), charts.ResetMark(end, True)]
    assert data.segments[0].points[0] == charts.Point(start, 5)
    assert all(start <= point.ts <= end and point.used != 90 for segment in data.segments for point in segment.points)
    assert all_readings == history + [future]  # Domain clipping never edits stored history.


def test_reset_pace_and_projections_with_credits():
    start = NOW - timedelta(days=1)
    reset = NOW + timedelta(days=6)
    history = [
        Sample(start, "codex", "7d", 10.0, reset, 10080, "app-server"),
        Sample(NOW, "codex", "7d", 70.0, reset, 10080, "app-server"),
    ]
    bd = burndown_for(history, "codex:7d")
    credits = [
        {"id": "c1", "expires_at": reset + timedelta(days=10)},
        {"id": "c2", "expires_at": reset + timedelta(days=11)},
    ]
    data = charts.build(bd, history, NOW, credits=credits)
    assert data.reset_credits_count == 2
    # 2 credits = 3 legs (k+1 = 3)
    # Lines: (up, drop, up, drop, up, drop) -> 6 lines
    assert len(data.reset_pace) == 6
    assert data.reset_pace[0].start == (start, 0.0)
    assert data.reset_pace[0].end[1] == 100.0
    assert data.reset_pace[1].start == data.reset_pace[0].end
    assert data.reset_pace[1].end == (data.reset_pace[0].end[0], 0.0)
    assert data.reset_pace[-1].start == (reset, 100.0)
    assert data.reset_pace[-1].end == (reset, 0.0)

    # Multi-stage projection
    # Burning 60% in 24 hours = 2.5%/hr.
    # From NOW (70%), 30% left to 100% = 12 hours -> exhaust 1 at NOW + 12h.
    assert len(data.reset_projections) >= 4
    assert data.reset_projections[0].start == (NOW, 70.0)
    assert data.reset_projections[0].end[1] == 100.0
    t_ex1 = data.reset_projections[0].end[0]
    assert t_ex1 == bd.exhaust_at
    assert data.next_reset_time == t_ex1
    assert data.reset_projections[-1].end == (reset, 0.0)
    assert len(data.reset_markers) >= 2
    assert data.reset_markers[0][0] == t_ex1
    assert "Reset 1" in data.reset_markers[0][2]
    expected_runway = ((100.0 - bd.used) + 2 * 100.0) / bd.rate_per_hour * 60.0
    assert abs(data.runway_with_resets_min - expected_runway) < 0.001


def test_reset_projection_pre_expiry_when_credit_expires_before_exhaustion():
    start = NOW - timedelta(days=2)
    reset = NOW + timedelta(days=5)
    history = [
        Sample(start, "codex", "7d", 10.0, reset, 10080, "app-server"),
        Sample(NOW, "codex", "7d", 20.0, reset, 10080, "app-server"),
    ]
    bd = burndown_for(history, "codex:7d")
    # Rate = 10% / 48h = 0.20833%/hr. At this rate, 80% to 100% would take 384 hours (> 5 days reset).
    # Credit expires in 24 hours (within the window).
    c_exp = NOW + timedelta(hours=24)
    credits = [{"id": "c1", "expires_at": c_exp}]
    data = charts.build(bd, history, NOW, credits=credits)
    assert data.reset_credits_count == 1
    assert len(data.credit_expiries) == 1
    assert data.credit_expiries[0].ts == c_exp
    # Should trigger pre-expiry reset at c_exp
    assert any(m[0] == c_exp and "pre-expiry" in m[2] for m in data.reset_markers)
    assert data.next_reset_time == c_exp
    assert data.reset_pace[-1].end == (reset, 0.0)
    assert data.reset_projections[-1].end == (reset, 0.0)


def test_no_reset_pace_when_zero_credits():
    start = NOW - timedelta(days=1)
    reset = NOW + timedelta(days=6)
    history = [
        Sample(start, "codex", "7d", 10.0, reset, 10080, "app-server"),
        Sample(NOW, "codex", "7d", 70.0, reset, 10080, "app-server"),
    ]
    bd = burndown_for(history, "codex:7d")
    data = charts.build(bd, history, NOW, credits=[])
    assert data.reset_credits_count == 0
    assert data.reset_pace == []
    assert data.reset_projections == []
    assert data.reset_markers == []
    assert data.credit_expiries == []

