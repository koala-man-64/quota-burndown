from datetime import datetime, timedelta, timezone

from quota_burndown.store import Sample

UTC = timezone.utc
T0 = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
RESET = T0 + timedelta(hours=3)


def s(minutes, used, key="claude:5h", source="api", resets_at=RESET):
    provider, window = key.split(":")
    return Sample(T0 + timedelta(minutes=minutes), provider, window, used, resets_at, 300, source)


def test_append_dedupes_unchanged_readings_within_window(store):
    assert store.append([s(0, 10.0), s(1, 10.0), s(2, 10.0)]) == 1
    assert store.append([s(3, 11.0)]) == 1
    assert store.append([s(20, 11.0)]) == 1  # 17 min later: kept as a heartbeat
    loaded = store.load()
    assert [x.used for x in loaded] == [10.0, 11.0, 11.0]
    assert store.latest()["claude:5h"].ts == T0 + timedelta(minutes=20)


def test_append_keeps_reset_change_and_other_keys(store):
    store.append([s(0, 50.0)])
    n = store.append([s(1, 50.0, resets_at=RESET + timedelta(hours=5)), s(1, 50.0, key="codex:7d")])
    assert n == 2
    assert set(store.latest()) == {"claude:5h", "codex:7d"}


def test_backfill_older_samples_do_not_move_latest(store):
    store.append([s(60, 40.0)])
    store.append([s(10, 5.0), s(20, 9.0)])
    assert store.latest()["claude:5h"].used == 40.0
    assert [x.used for x in store.load()] == [5.0, 9.0, 40.0]


def test_load_since_and_malformed_lines(store):
    store.append([s(0, 1.0), s(30, 2.0)])
    with open(store.paths.samples, "a", encoding="utf-8") as fh:
        fh.write("not json\n{\"ts\": \"bad\"}\n")
    assert [x.used for x in store.load(since=T0 + timedelta(minutes=15))] == [2.0]
    assert len(store.load()) == 2


def test_load_drops_exact_duplicates(store):
    store.append([s(0, 1.0)])
    line = open(store.paths.samples, encoding="utf-8").read()
    with open(store.paths.samples, "a", encoding="utf-8") as fh:
        fh.write(line)
    assert len(store.load()) == 1


def test_prune_by_age_and_source_rebuilds_latest(store):
    store.append([s(0, 1.0), s(120, 2.0), s(130, 3.0, source="statusline")])
    assert store.latest()["claude:5h"].used == 3.0
    assert store.prune(drop_source="statusline") == 1
    assert store.latest()["claude:5h"].used == 2.0
    assert store.prune(keep_since=T0 + timedelta(minutes=60)) == 1
    assert [x.used for x in store.load()] == [2.0]


def test_rebuild_latest_from_samples(store):
    store.append([s(0, 1.0), s(30, 2.0, key="codex:7d")])
    store.paths.latest.unlink()
    assert store.latest() == {}
    rebuilt = store.rebuild_latest()
    assert {k: v.used for k, v in rebuilt.items()} == {"claude:5h": 1.0, "codex:7d": 2.0}
    assert store.latest() == rebuilt


def test_roundtrip_without_reset():
    sample = Sample(T0, "codex", "7d", 12.5, None, 10080, "rollout")
    assert Sample.from_dict(sample.to_dict()) == sample
    assert Sample.from_dict({"ts": "nope"}) is None
