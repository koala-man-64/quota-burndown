import json
import sqlite3
from datetime import datetime, timedelta, timezone

from quota_burndown.providers import antigravity
from quota_burndown.store import Sample
from quota_burndown.util import iso

UTC = timezone.utc
T0 = datetime(2026, 9, 8, 14, 30, tzinfo=UTC)  # Tuesday


def setup_db(db_path, rows):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE events ("
        "provider TEXT NOT NULL, kind TEXT NOT NULL, event_key TEXT NOT NULL, "
        "ts TEXT NOT NULL, input_tokens_inferred INTEGER, model TEXT NOT NULL DEFAULT '', "
        "effort TEXT NOT NULL DEFAULT '', PRIMARY KEY (provider, kind, event_key))"
    )
    for i, (ts, tokens) in enumerate(rows):
        conn.execute(
            "INSERT INTO events (provider, kind, event_key, ts, input_tokens_inferred) VALUES (?, ?, ?, ?, ?)",
            ("antigravity", "request", f"ev-{i}", iso(ts), tokens),
        )
    conn.commit()
    conn.close()


def test_weekly_cycle_bounds_aligns_to_monday():
    # Tuesday 2026-09-08 -> cycle start Monday 2026-09-07 00:00, reset Monday 2026-09-14 00:00
    start, reset = antigravity.weekly_cycle_bounds(T0)
    assert start == datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    assert reset == datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
    assert reset - start == timedelta(days=7)


def test_to_samples_empty():
    assert antigravity.to_samples([], 50_000_000) == []


def test_to_samples_builds_baseline_and_burn_percentages():
    events = [
        {"ts": T0, "tokens": 5_000_000, "model": "gemini-3.8-flash", "effort": "medium"},
        {"ts": T0 + timedelta(hours=2), "tokens": 15_000_000, "model": "gemini-3.8-flash", "effort": "medium"},
    ]
    budget = 100_000_000
    samples = antigravity.to_samples(events, budget)
    assert len(samples) == 3  # baseline (0%) + 2 events
    cycle_start = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    reset = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)

    assert samples[0] == Sample(cycle_start, "antigravity", "7d:gemini", 0.0, reset, 10080, "ledger")
    assert samples[1] == Sample(T0, "antigravity", "7d:gemini", 5.0, reset, 10080, "ledger")
    assert samples[2] == Sample(T0 + timedelta(hours=2), "antigravity", "7d:gemini", 20.0, reset, 10080, "ledger")


def test_to_samples_respects_after_ts():
    events = [
        {"ts": T0, "tokens": 5_000_000},
        {"ts": T0 + timedelta(hours=2), "tokens": 15_000_000},
    ]
    samples = antigravity.to_samples(events, 100_000_000, after_ts=T0)
    assert len(samples) == 1
    assert samples[0].ts == T0 + timedelta(hours=2)
    assert samples[0].used == 20.0  # Still tracks full cumulative burn from start of cycle


def test_collect_missing_db(tmp_path):
    state = tmp_path / "state.json"
    db = tmp_path / "missing.sqlite"
    samples, warnings, stats = antigravity.collect(state, db)
    assert samples == []
    assert warnings == []
    assert stats["present"] is False


def test_collect_reads_events_and_persists_state(tmp_path):
    db = tmp_path / "usage.sqlite"
    state = tmp_path / "state.json"
    setup_db(db, [
        (T0, 10_000_000),
        (T0 + timedelta(hours=1), 15_000_000),
    ])

    samples, warnings, stats = antigravity.collect(state, db, budget=100_000_000, now=T0 + timedelta(hours=3))
    assert warnings == []
    assert stats["present"] is True
    assert stats["events"] == 2
    assert len(samples) == 3
    assert state.is_file()

    # Second collect without new events produces 0 new samples
    samples2, _, stats2 = antigravity.collect(state, db, budget=100_000_000, now=T0 + timedelta(hours=3))
    assert samples2 == []
    assert stats2["new"] == 0

