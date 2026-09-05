import json
import os
import time
from datetime import datetime, timezone

from quota_burndown.providers import codex


def event(ts, used, window_minutes=10080, resets_at=1788747992, secondary=None):
    limits = {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": window_minutes, "resets_at": resets_at}, "secondary": secondary, "plan_type": "pro"}
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": limits}})


def turn(ts, model):
    return json.dumps({"timestamp": ts, "type": "turn_context", "payload": {"turn_id": "t", "model": model, "effort": "medium"}})


def write_rollout(path, lines, trailing_partial=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")
        fh.write(trailing_partial)


def test_model_family():
    assert codex.model_family("gpt-5.6-sol") == "codex"
    assert codex.model_family("gpt-6-terra") == "codex"
    assert codex.model_family("GPT-5.3-codex-spark") == "spark"
    assert codex.model_family("gpt-oss:20b") == "gpt-oss-20b"
    assert codex.model_family("o3") == "o3"
    assert codex.model_family("") == "" and codex.model_family("  ") == ""


def test_samples_from_line_primary_and_secondary_keyed_by_family():
    line = event("2026-09-02T23:21:33.504Z", 99.0, secondary={"used_percent": 12.0, "window_minutes": 300, "resets_at": 1788743999})
    samples = codex.samples_from_line(line, model="gpt-5.3-codex-spark")
    assert [(s.window, s.used, s.window_min) for s in samples] == [("7d:spark", 99.0, 10080), ("5h:spark", 12.0, 300)]
    assert samples[0].ts == datetime(2026, 9, 2, 23, 21, 33, 504000, tzinfo=timezone.utc)
    assert samples[0].resets_at == datetime.fromtimestamp(1788747992, tz=timezone.utc)
    assert samples[0].source == "rollout" and samples[0].provider == "codex"
    assert [s.window for s in codex.samples_from_line(line, model="gpt-5.6-terra")] == ["7d:codex"]
    assert codex.samples_from_line(line) == []  # no model known yet: not attributable to a pool
    assert codex.samples_from_line('{"type":"event_msg","payload":{"type":"token_count","rate_limits":null}}', model="gpt-5.6-sol") == []
    assert codex.samples_from_line("garbage", model="gpt-5.6-sol") == []
    premium = json.dumps({"timestamp": "2026-09-02T23:21:33Z", "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"limit_id": "premium", "primary": None, "secondary": None}}})
    assert codex.samples_from_line(premium, model="gpt-5.6-sol") == []


def test_samples_from_line_drops_readings_for_already_ended_windows():
    stale = event("2026-09-02T23:21:33Z", 72.0, resets_at=1786166604)  # window ended weeks before this line was written
    assert codex.samples_from_line(stale, model="gpt-5.6-sol") == []


def test_turn_model():
    assert codex.turn_model(turn("2026-09-02T10:00:00Z", "gpt-5.6-sol")) == "gpt-5.6-sol"
    assert codex.turn_model('{"type":"event_msg","payload":{"model":"x"}}') == ""
    assert codex.turn_model("nope") == ""


def test_scan_file_tracks_model_and_stops_before_partial_line(tmp_path):
    path = tmp_path / "r.jsonl"
    write_rollout(path, [
        event("2026-09-02T09:59:00Z", 9),  # before any turn_context: skipped
        turn("2026-09-02T09:59:30Z", "gpt-5.6-terra"),
        event("2026-09-02T10:00:00Z", 10),
        '{"type":"response_item","payload":{}}',
        turn("2026-09-02T10:04:00Z", "gpt-5.3-codex-spark"),
        event("2026-09-02T10:05:00Z", 11),
    ], trailing_partial='{"timestamp":"2026-09-02T10:06:00Z","payload":{"rate_limits":')
    samples, offset, model = codex.scan_file(path, 0)
    assert [(s.window, s.used) for s in samples] == [("7d:codex", 10.0), ("7d:spark", 11.0)]
    assert model == "gpt-5.3-codex-spark"
    # complete the partial line and rescan from the returned offset with the remembered model
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"primary":{"used_percent":12,"window_minutes":10080,"resets_at":1788747992}}}}\n')
    more, end, model = codex.scan_file(path, offset, model)
    assert [(s.window, s.used) for s in more] == [("7d:spark", 12.0)]
    assert end == os.path.getsize(path) and model == "gpt-5.3-codex-spark"


def test_collect_is_incremental_and_persists_model(tmp_path):
    home = tmp_path / "codex"
    recent = home / "archived_sessions" / "rollout-2026-09-02T10-00-00-a.jsonl"
    old = home / "sessions" / "2026" / "06" / "08" / "rollout-old.jsonl"
    write_rollout(recent, [turn("2026-09-02T09:59:00Z", "gpt-5.6-sol"), event("2026-09-02T10:00:00Z", 10)])
    write_rollout(old, [turn("2026-06-08T09:59:00Z", "gpt-5.6-sol"), event("2026-06-08T10:00:00Z", 50)])
    old_time = time.time() - 40 * 86400
    os.utime(old, (old_time, old_time))
    state = tmp_path / "state.json"

    samples, warnings, stats = codex.collect(state, home=home, since_days=14)
    assert [(s.window, s.used) for s in samples] == [("7d:codex", 10.0)] and warnings == []
    assert stats == {"files_seen": 1, "files_scanned": 1}
    assert json.loads(state.read_text(encoding="utf-8"))[str(recent)]["model"] == "gpt-5.6-sol"

    samples, _, stats = codex.collect(state, home=home, since_days=14)
    assert samples == [] and stats["files_scanned"] == 0

    with open(recent, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(event("2026-09-02T10:10:00Z", 11) + "\n")  # no new turn_context: the remembered model applies
    samples, _, stats = codex.collect(state, home=home, since_days=14)
    assert [(s.window, s.used) for s in samples] == [("7d:codex", 11.0)] and stats["files_scanned"] == 1

    samples, _, stats = codex.collect(state, home=home, full=True)
    assert sorted(s.used for s in samples) == [10.0, 11.0, 50.0]
    assert stats["files_seen"] == 2


def test_collect_handles_missing_home(tmp_path):
    samples, warnings, stats = codex.collect(tmp_path / "state.json", home=tmp_path / "nope")
    assert samples == [] and warnings == [] and stats["files_seen"] == 0
