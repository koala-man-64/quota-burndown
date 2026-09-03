import json
import os
import time
from datetime import datetime, timezone

from quota_burndown.providers import codex


def event(ts, used, window_minutes=10080, resets_at=1788747992, secondary=None):
    limits = {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": window_minutes, "resets_at": resets_at}, "secondary": secondary, "plan_type": "pro"}
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": limits}})


def write_rollout(path, lines, trailing_partial=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")
        fh.write(trailing_partial)


def test_samples_from_line_primary_and_secondary():
    line = event("2026-09-02T23:21:33.504Z", 99.0, secondary={"used_percent": 12.0, "window_minutes": 300, "resets_at": 1788743999})
    samples = codex.samples_from_line(line)
    assert [(s.window, s.used, s.window_min) for s in samples] == [("7d", 99.0, 10080), ("5h", 12.0, 300)]
    assert samples[0].ts == datetime(2026, 9, 2, 23, 21, 33, 504000, tzinfo=timezone.utc)
    assert samples[0].resets_at == datetime.fromtimestamp(1788747992, tz=timezone.utc)
    assert samples[0].source == "rollout" and samples[0].provider == "codex"
    assert codex.samples_from_line('{"type":"event_msg","payload":{"type":"token_count","rate_limits":null}}') == []
    assert codex.samples_from_line("garbage") == []


def test_samples_from_line_drops_readings_for_already_ended_windows():
    stale = event("2026-09-02T23:21:33Z", 72.0, resets_at=1786166604)  # window ended weeks before this line was written
    assert codex.samples_from_line(stale) == []


def test_scan_file_stops_before_partial_line(tmp_path):
    path = tmp_path / "r.jsonl"
    write_rollout(path, [event("2026-09-02T10:00:00Z", 10), '{"type":"response_item","payload":{}}', event("2026-09-02T10:05:00Z", 11)], trailing_partial='{"timestamp":"2026-09-02T10:06:00Z","payload":{"rate_limits":')
    samples, offset = codex.scan_file(path, 0)
    assert [s.used for s in samples] == [10.0, 11.0]
    # complete the partial line and rescan from the returned offset
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"primary":{"used_percent":12,"window_minutes":10080,"resets_at":1788747992}}}}\n')
    more, end = codex.scan_file(path, offset)
    assert [s.used for s in more] == [12.0]
    assert end == os.path.getsize(path)


def test_collect_is_incremental_and_respects_since_days(tmp_path):
    home = tmp_path / "codex"
    recent = home / "archived_sessions" / "rollout-2026-09-02T10-00-00-a.jsonl"
    old = home / "sessions" / "2026" / "06" / "08" / "rollout-old.jsonl"
    write_rollout(recent, [event("2026-09-02T10:00:00Z", 10)])
    write_rollout(old, [event("2026-06-08T10:00:00Z", 50)])
    old_time = time.time() - 40 * 86400
    os.utime(old, (old_time, old_time))
    state = tmp_path / "state.json"

    samples, warnings, stats = codex.collect(state, home=home, since_days=14)
    assert [s.used for s in samples] == [10.0] and warnings == []
    assert stats == {"files_seen": 1, "files_scanned": 1}

    samples, _, stats = codex.collect(state, home=home, since_days=14)
    assert samples == [] and stats["files_scanned"] == 0

    with open(recent, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(event("2026-09-02T10:10:00Z", 11) + "\n")
    samples, _, stats = codex.collect(state, home=home, since_days=14)
    assert [s.used for s in samples] == [11.0] and stats["files_scanned"] == 1

    samples, _, stats = codex.collect(state, home=home, full=True)
    assert sorted(s.used for s in samples) == [10.0, 11.0, 50.0]
    assert stats["files_seen"] == 2


def test_collect_handles_missing_home(tmp_path):
    samples, warnings, stats = codex.collect(tmp_path / "state.json", home=tmp_path / "nope")
    assert samples == [] and warnings == [] and stats["files_seen"] == 0
