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


def test_seven_day_reset_prefers_known_then_last_drop():
    readings = [reading(0, 0, 70), reading(15, 0, 71), reading(30, 0, 0), reading(45, 0, 1), reading(60, 0, 2)]
    items = [{"t": r["t"], "ts": datetime.fromtimestamp(r["t"] / 1000, tz=UTC), "org": r["org"], "values": {"7d": float(r["u"]["sd"])}} for r in readings]
    known = T0 + timedelta(minutes=20)
    week = by_window(claude_desktop.to_samples(items, known_7d_reset=known))["7d"]
    assert week[0].resets_at == known and week[1].resets_at == known
    assert week[2].resets_at == T0 + timedelta(minutes=30) + timedelta(days=7)  # known reset is now behind us, use the drop
    assert week[4].resets_at == T0 + timedelta(minutes=30) + timedelta(days=7)
    assert by_window(claude_desktop.to_samples(items))["7d"][0].resets_at is None  # nothing known, no drop yet


def test_collect_is_incremental(tmp_path):
    path = tmp_path / "Claude" / "plan-usage-history.json"
    state = tmp_path / "state.json"
    write_history(path, [reading(0, 0, 40), reading(15, 3, 40)])
    samples, warnings, stats = claude_desktop.collect(state, None, path)
    assert warnings == [] and stats == {"present": True, "readings": 2, "new": 4}
    assert sorted((s.window, s.used) for s in samples) == [("5h", 0.0), ("5h", 3.0), ("7d", 40.0), ("7d", 40.0)]

    samples, _, stats = claude_desktop.collect(state, None, path)
    assert samples == [] and stats["new"] == 0

    write_history(path, [reading(0, 0, 40), reading(15, 3, 40), reading(30, 5, 41)])
    samples, _, stats = claude_desktop.collect(state, None, path)
    assert [(s.window, s.used) for s in samples] == [("5h", 5.0), ("7d", 41.0)]
    assert samples[0].resets_at == T0 + timedelta(minutes=15, hours=5)  # start remembered from the walk over older readings

    samples, warnings, stats = claude_desktop.collect(state, None, tmp_path / "nope.json")
    assert samples == [] and warnings == [] and stats["present"] is False and stats["path"].endswith("nope.json")
    write_history(path, [])
    samples, warnings, stats = claude_desktop.collect(tmp_path / "s2.json", None, path)
    assert samples == [] and "no readings" in warnings[0]
