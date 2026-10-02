import json
from datetime import datetime, timedelta, timezone

from quota_burndown.providers import claude_desktop

UTC = timezone.utc
T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def reading(minutes, fh, sd, org="4b479e7c-0000-0000-0000-000000000000"):
    return {"t": int((T0 + timedelta(minutes=minutes)).timestamp() * 1000), "org": org, "u": {"fh": fh, "sd": sd}}


def write_history(path, readings):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "samples": readings}), encoding="utf-8")


def by_window(samples):
    out = {}
    for s in samples:
        out.setdefault(s.window, []).append(s)
    return out


def test_read_history_drops_malformed_and_sorts(tmp_path):
    path = tmp_path / "plan-usage-history.json"
    write_history(path, [reading(10, 5, 40), {"t": "nope", "u": {"fh": 1}}, {"t": 1, "u": "x"}, reading(0, 0, 40), {"t": 5, "u": {"fh": True}}])
    history = claude_desktop.read_history(path)
    assert [h["values"] for h in history] == [{"5h": 0.0, "7d": 40.0}, {"5h": 5.0, "7d": 40.0}]
    assert claude_desktop.read_history(tmp_path / "missing.json") == []


def test_five_hour_window_start_is_first_nonzero_after_zero():
    readings = [reading(0, 0, 40), reading(15, 2, 40), reading(30, 4, 41), reading(320, 9, 42), reading(330, 0, 42), reading(345, 1, 42)]
    items = [{"t": r["t"], "ts": datetime.fromtimestamp(r["t"] / 1000, tz=UTC), "org": r["org"], "values": {"5h": float(r["u"]["fh"]), "7d": float(r["u"]["sd"])}} for r in readings]
    samples = by_window(claude_desktop.to_samples(items))
    five = samples["5h"]
    assert [s.used for s in five] == [0.0, 2.0, 4.0, 9.0, 0.0, 1.0]
    assert five[0].resets_at is None  # nothing active
    start = T0 + timedelta(minutes=15)
    assert five[1].resets_at == start + timedelta(hours=5) and five[2].resets_at == start + timedelta(hours=5)
    assert five[3].resets_at is None  # five hours have passed since the inferred start; do not project a stale reset
    assert five[4].resets_at is None
    assert five[5].resets_at == T0 + timedelta(minutes=345, hours=5)
    assert all(s.source == "desktop" and s.provider == "claude" and s.window_min == 300 for s in five)


def test_seven_day_reset_is_inferred_from_the_last_drop_to_zero():
    readings = [reading(0, 0, 70), reading(15, 0, 71), reading(30, 0, 0), reading(45, 0, 1), reading(60, 0, 2)]
    items = [{"t": r["t"], "ts": datetime.fromtimestamp(r["t"] / 1000, tz=UTC), "org": r["org"], "values": {"7d": float(r["u"]["sd"])}} for r in readings]
    week = by_window(claude_desktop.to_samples(items))["7d"]
    assert week[0].resets_at is None and week[1].resets_at is None  # no drop seen yet: no reset to pace against
    assert week[2].resets_at == T0 + timedelta(minutes=30) + timedelta(days=7)
    assert week[4].resets_at == T0 + timedelta(minutes=30) + timedelta(days=7)


def test_history_path_prefers_newest_existing_copy(tmp_path, monkeypatch):
    import os
    import time

    import pytest

    from quota_burndown import config

    if os.name != "nt":
        pytest.skip("Windows AppData layout")
    monkeypatch.delenv("QUOTA_BURNDOWN_CLAUDE_DESKTOP_HISTORY", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    plain = tmp_path / "Roaming" / "Claude" / "plan-usage-history.json"
    packaged = tmp_path / "Local" / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude" / "plan-usage-history.json"
    assert config.claude_desktop_history() == plain  # nothing exists yet: the plain location
    write_history(packaged, [reading(0, 0, 1)])
    assert config.claude_desktop_history() == packaged  # only the package copy exists
    write_history(plain, [reading(0, 0, 1)])
    old = time.time() - 3600
    os.utime(plain, (old, old))
    assert config.claude_desktop_history() == packaged  # both exist: the newer one wins
    os.utime(plain, None)
    assert config.claude_desktop_history() == plain
    monkeypatch.setenv("QUOTA_BURNDOWN_CLAUDE_DESKTOP_HISTORY", str(tmp_path / "x.json"))
    assert config.claude_desktop_history() == tmp_path / "x.json"


def test_collect_is_incremental(tmp_path):
    path = tmp_path / "Claude" / "plan-usage-history.json"
    state = tmp_path / "state.json"
    write_history(path, [reading(0, 0, 40), reading(15, 3, 40)])
    samples, warnings, stats = claude_desktop.collect(state, path)
    assert warnings == [] and stats == {"present": True, "readings": 2, "new": 4}
    assert sorted((s.window, s.used) for s in samples) == [("5h", 0.0), ("5h", 3.0), ("7d", 40.0), ("7d", 40.0)]

    samples, _, stats = claude_desktop.collect(state, path)
    assert samples == [] and stats["new"] == 0

    write_history(path, [reading(0, 0, 40), reading(15, 3, 40), reading(30, 5, 41)])
    samples, _, stats = claude_desktop.collect(state, path)
    assert [(s.window, s.used) for s in samples] == [("5h", 5.0), ("7d", 41.0)]
    assert samples[0].resets_at == T0 + timedelta(minutes=15, hours=5)  # start remembered from the walk over older readings

    samples, warnings, stats = claude_desktop.collect(state, tmp_path / "nope.json")
    assert samples == [] and warnings == [] and stats["present"] is False and stats["path"].endswith("nope.json")
    write_history(path, [])
    samples, warnings, stats = claude_desktop.collect(tmp_path / "s2.json", path)
    assert samples == [] and "no readings" in warnings[0]


# The weekly window resets on a fixed schedule; T0 (Thursday 12:00Z) is five hours after it.
SCHEDULED = T0 - timedelta(hours=5)


def items_of(readings):
    return [{"t": r["t"], "ts": datetime.fromtimestamp(r["t"] / 1000, tz=UTC), "org": r["org"],
             "values": {"7d": float(r["u"]["sd"])}} for r in readings]


def write_handoff(home, observed, resets_at, name="acct-x-1.json"):
    directory = home / "quota-burndown-statusline"
    directory.mkdir(parents=True, exist_ok=True)
    record = {"session": "s", "account_scope": "acct-x", "digest": name, "observed_at": observed.isoformat(),
              "quota": {"seven_day": {"used_percentage": 1.0, "window_minutes": 10080, "resets_at": resets_at}}}
    (directory / name).write_text(json.dumps(record), encoding="utf-8")
    return directory / name


def test_anchor_projects_reset_through_overnight_gap():
    # Last reading 04:00Z at 98%, app asleep through the 07:00Z reset, next reading 12:00Z at 0%.
    readings = [reading(-480, 0, 98), reading(0, 0, 0), reading(60, 0, 2)]
    week = by_window(claude_desktop.to_samples(items_of(readings), anchor=SCHEDULED - timedelta(days=21)))["7d"]
    assert week[0].resets_at == SCHEDULED  # before the gap: the reset still ahead of it
    assert week[1].resets_at == SCHEDULED + timedelta(days=7)
    assert week[2].resets_at == SCHEDULED + timedelta(days=7)  # not the drop time (12:00Z) plus seven days


def test_reading_after_a_missed_scheduled_reset_points_at_the_next_one():
    # No reading since the reset: the reset shown must already be the next scheduled one,
    # not a reset that has passed.
    week = by_window(claude_desktop.to_samples(items_of([reading(-480, 0, 98)]), anchor=SCHEDULED))["7d"]
    assert week[0].resets_at == SCHEDULED
    assert claude_desktop.project_reset(SCHEDULED, SCHEDULED) == SCHEDULED + timedelta(days=7)
    assert claude_desktop.project_reset(SCHEDULED, SCHEDULED - timedelta(seconds=1)) == SCHEDULED


def test_drop_off_schedule_falls_back_to_inference_until_schedule_agrees():
    # Drop observed two days after the scheduled reset, with readings on both sides: off-schedule.
    off = 2 * 24 * 60
    readings = [reading(off - 15, 0, 60), reading(off, 0, 0), reading(off + 15, 0, 1)]
    week = by_window(claude_desktop.to_samples(items_of(readings), anchor=SCHEDULED))["7d"]
    assert week[0].resets_at == SCHEDULED + timedelta(days=7)
    assert week[1].resets_at == T0 + timedelta(minutes=off, days=7)
    assert week[2].resets_at == T0 + timedelta(minutes=off, days=7)
    # The next drop straddles the scheduled reset again: the anchor resumes.
    nxt = 7 * 24 * 60
    readings += [reading(nxt - 360, 0, 50), reading(nxt, 0, 0)]
    week = by_window(claude_desktop.to_samples(items_of(readings), anchor=SCHEDULED))["7d"]
    assert week[-1].resets_at == SCHEDULED + timedelta(days=14)


def test_weekly_anchor_persists_beyond_handoff_and_takes_newer_reports(tmp_path):
    assert claude_desktop.weekly_anchor(tmp_path) is None
    handoff = write_handoff(tmp_path, T0, int(SCHEDULED.timestamp()))
    assert claude_desktop.weekly_anchor(tmp_path) == SCHEDULED
    handoff.unlink()  # pruned by the status line a day later
    assert claude_desktop.weekly_anchor(tmp_path) == SCHEDULED
    moved = SCHEDULED + timedelta(days=7, hours=3)
    write_handoff(tmp_path, T0 + timedelta(days=1), moved.isoformat(), name="acct-x-2.json")
    write_handoff(tmp_path, T0 - timedelta(days=1), int(SCHEDULED.timestamp()), name="acct-x-0.json")  # older: ignored
    assert claude_desktop.weekly_anchor(tmp_path) == moved


def test_collect_reemits_newest_reading_when_anchor_changes(tmp_path):
    path = tmp_path / "Claude" / "plan-usage-history.json"
    state = tmp_path / "state.json"
    write_history(path, [reading(-360, 0, 2), reading(0, 0, 0), reading(15, 0, 1)])
    samples, _, _ = claude_desktop.collect(state, path)
    assert by_window(samples)["7d"][-1].resets_at == T0 + timedelta(days=7)  # inferred from the drop
    assert claude_desktop.collect(state, path)[0] == []

    write_handoff(tmp_path, T0, int(SCHEDULED.timestamp()))
    samples, _, stats = claude_desktop.collect(state, path)
    assert [(s.window, s.used) for s in samples] == [("5h", 0.0), ("7d", 1.0)] and stats["new"] == 2
    assert by_window(samples)["7d"][0].resets_at == SCHEDULED + timedelta(days=7)
    assert claude_desktop.collect(state, path)[0] == []  # anchor unchanged: nothing new
