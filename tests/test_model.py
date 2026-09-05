from datetime import datetime, timedelta, timezone

from quota_burndown.model import WindowInstance, canonical_samples, compute, current, find_instance, group_instances
from quota_burndown.store import Sample

UTC = timezone.utc
RESET = datetime(2026, 9, 3, 1, 20, tzinfo=UTC)
START = RESET - timedelta(minutes=300)


def sample(ts, used, resets_at=RESET, window="5h", window_min=300, provider="claude", source="api"):
    return Sample(ts, provider, window, used, resets_at, window_min, source)


def inst(samples, window_min=300):
    return WindowInstance("claude:5h", "claude", "5h", window_min, RESET, sorted(samples, key=lambda s: s.ts))


def test_pace_and_delta_over():
    now = START + timedelta(minutes=150)  # halfway
    bd = compute(inst([sample(now, 60.0)]), now)
    assert bd.pace == 50.0
    assert bd.delta == 10.0
    assert bd.status == "over"
    assert bd.remaining_min == 150
    assert bd.rate_per_hour == 60.0 / 2.5
    # 40 points left at 24/h -> 100 min from now, before reset
    assert bd.exhaust_at == now + timedelta(minutes=100)
    assert bd.exhausts_before_reset
    assert round(bd.projected_end) == 120


def test_under_pace_projects_end_use_below_100():
    now = START + timedelta(minutes=200)
    bd = compute(inst([sample(now, 30.0)]), now)
    assert bd.status == "under"
    assert bd.delta < 0
    assert not bd.exhausts_before_reset
    assert 40 < bd.projected_end < 50


def test_on_pace_band():
    now = START + timedelta(minutes=100)
    assert compute(inst([sample(now, 34.0)]), now).status == "on-pace"


def test_too_early_exhausted_and_expired():
    early_now = START + timedelta(minutes=2)
    assert compute(inst([sample(early_now, 1.0)]), early_now).status == "early"
    mid = START + timedelta(minutes=100)
    done = compute(inst([sample(mid, 100.0)]), mid)
    assert done.status == "exhausted" and done.exhaust_at is None
    after = RESET + timedelta(minutes=5)
    expired = compute(inst([sample(mid, 70.0)]), after)
    assert expired.status == "expired"
    assert expired.pace == 100.0 and expired.remaining_min == 0
    assert expired.projected_end == 70.0
    # a window that ended at 100% is reported as ended, not exhausted
    assert compute(inst([sample(mid, 100.0)]), after).status == "expired"


def test_group_instances_clusters_jittered_resets():
    jitter_a = sample(START + timedelta(minutes=10), 10.0, RESET - timedelta(seconds=0.1))   # 01:19:59.9
    jitter_b = sample(START + timedelta(minutes=20), 20.0, RESET)                              # 01:20:00
    jitter_c = sample(START + timedelta(minutes=30), 30.0, RESET + timedelta(seconds=2))
    older = sample(RESET - timedelta(days=1), 90.0, RESET - timedelta(hours=10))
    grouped = group_instances([jitter_b, older, jitter_c, jitter_a])
    instances = grouped["claude:5h"]
    assert len(instances) == 2
    assert instances[0].resets_at == RESET - timedelta(hours=10)
    assert [s.used for s in instances[1].samples] == [10.0, 20.0, 30.0]
    assert find_instance(instances, RESET + timedelta(seconds=90)) is instances[1]
    assert find_instance(instances, RESET + timedelta(hours=1)) is None


def test_current_uses_latest_and_history_and_idle():
    now = START + timedelta(minutes=120)
    history = [sample(START + timedelta(minutes=30), 5.0), sample(START + timedelta(minutes=90), 25.0, RESET + timedelta(seconds=1))]
    latest = {
        "claude:5h": sample(now, 40.0, RESET - timedelta(seconds=1), source="statusline"),
        "codex:7d": sample(now, 12.0, None, "7d", 10080, "codex"),
    }
    out = current(history, latest, now)
    assert [bd.key for bd in out] == ["claude:5h", "codex:7d"]
    assert out[0].used == 40.0 and len(out[0].samples) == 3 and out[0].source == "statusline"
    assert out[1].status == "idle"


def test_current_hides_windows_that_ended_long_ago_but_keeps_recent_ones():
    now = RESET + timedelta(hours=2)
    recent = {"claude:5h": sample(RESET - timedelta(minutes=10), 80.0)}
    assert current([], recent, now)[0].status == "expired"
    long_ago = {"codex:5h": sample(RESET - timedelta(days=3), 100.0, RESET - timedelta(days=2), "5h", 300, "codex", "rollout")}
    assert current([], long_ago, now) == []


def test_legacy_codex_versions_collapse_to_one_pool_while_spark_stays_separate():
    now = START + timedelta(minutes=60)
    earlier = now - timedelta(minutes=5)
    latest = {
        "codex:7d:gpt-5.6": sample(earlier, 27.0, RESET + timedelta(days=3), "7d:gpt-5.6", 10080, "codex", "rollout"),
        "codex:7d:gpt-6": sample(now, 28.0, RESET + timedelta(days=3), "7d:gpt-6", 10080, "codex", "rollout"),
        "codex:5h:gpt-5.6": sample(now, 9.0, RESET, "5h:gpt-5.6", 300, "codex", "rollout"),
        "codex:7d:spark": sample(now, 50.0, RESET + timedelta(days=4), "7d:spark", 10080, "codex", "rollout"),
        "codex:5h:spark": sample(now, 8.0, RESET, "5h:spark", 300, "codex", "rollout"),
        "codex:10080m:Codex": sample(now, 28.0, RESET + timedelta(days=3), "10080m:Codex", 10080, "codex", "app-server"),
        "codex:10080m:Codex_bengalfox": sample(now, 50.0, RESET + timedelta(days=4), "10080m:Codex_bengalfox", 10080, "codex", "app-server"),
        "codex:300m:Codex_bengalfox": sample(now, 8.0, RESET, "300m:Codex_bengalfox", 300, "codex", "app-server"),
    }
    out = current(list(latest.values()), latest, now)
    assert [bd.key for bd in out] == ["codex:5h:spark", "codex:7d:codex", "codex:7d:spark"]
    assert [bd.used for bd in out] == [8.0, 28.0, 50.0]
    assert [sample.used for sample in out[1].samples] == [27.0, 28.0, 28.0]


def test_current_without_history_still_computes():
    now = START + timedelta(minutes=60)
    out = current([], {"claude:5h": sample(now, 20.0)}, now)
    assert out[0].pace == 20.0 and out[0].status == "on-pace"


def test_claude_aliases_use_newest_state_in_either_order():
    now = START + timedelta(minutes=60)
    entries = [
        sample(now - timedelta(minutes=30), 100, source="desktop"),
        sample(now, 0, None, "300m:Claude", source="desktop-history"),
        sample(now - timedelta(minutes=30), 24, RESET, "7d", 10080, source="desktop"),
        sample(now, 25, RESET, "10080m:claude", 10080, source="desktop-history"),
        sample(now, 70, RESET, "7d:fable", 10080),
    ]
    for ordered in (entries, list(reversed(entries))):
        result = {bd.key: bd for bd in current(ordered, {s.key: s for s in ordered}, now)}
        assert set(result) == {"claude:5h", "claude:7d", "claude:7d:fable"}
        assert result["claude:5h"].used == 0 and result["claude:5h"].status == "idle"
        assert result["claude:5h"].sample_ts == now
        assert result["claude:7d"].used == 25
        assert result["claude:7d:fable"].used == 70


def test_claude_duplicate_timestamp_prefers_direct_then_canonical_reading():
    now = START + timedelta(minutes=60)
    canonical = sample(now, 40, source="desktop")
    for alias_source, expected in (("desktop-history", 40), ("statusline", 45)):
        alias = sample(now, 45, RESET + timedelta(seconds=60), window="300m:claude", source=alias_source)
        for ordered in ([canonical, alias], [alias, canonical]):
            result = current(ordered, {s.key: s for s in ordered}, now)
            assert len(result) == 1 and result[0].used == expected
            assert len(result[0].samples) == 1


def test_claude_same_timestamp_retains_distinct_historical_reset_boundaries():
    now = START + timedelta(minutes=60)
    old = sample(now, 100, now - timedelta(minutes=5), source="desktop")
    new = sample(now, 20, now + timedelta(hours=4), "300m:claude", source="statusline")
    for ordered in ([old, new], [new, old]):
        history = canonical_samples(ordered)
        instances = group_instances(history)["claude:5h"]
        assert len(instances) == 2
        assert [inst.resets_at for inst in instances] == [old.resets_at, new.resets_at]
        result = current(history, {s.key: s for s in ordered}, now)
        assert len(result) == 1 and result[0].used == 20
        assert result[0].resets_at == new.resets_at
