import json
import time
from datetime import datetime, timezone

from quota_burndown.providers import claude

NOW = datetime(2026, 9, 2, 23, 0, tzinfo=timezone.utc)


def test_normalize_usage_prefers_limits_list(fixture_dir):
    payload = json.loads((fixture_dir / "claude_usage.json").read_text(encoding="utf-8"))
    samples = claude.normalize_usage(payload, NOW)
    by_window = {s.window: s for s in samples}
    assert set(by_window) == {"5h", "7d", "7d:fable"}
    assert by_window["5h"].used == 44.0 and by_window["5h"].window_min == 300
    assert by_window["7d"].used == 27.0 and by_window["7d"].window_min == 10080
    assert by_window["7d:fable"].used == 43.0
    assert by_window["5h"].resets_at == datetime(2026, 9, 3, 1, 19, 59, 900009, tzinfo=timezone.utc)
    assert all(s.source == "api" and s.provider == "claude" and s.ts == NOW for s in samples)


def test_normalize_usage_falls_back_to_legacy_fields():
    payload = {"five_hour": {"utilization": 12.0, "resets_at": "2026-09-03T01:19:59Z"}, "seven_day": {"utilization": 3.5, "resets_at": None}, "seven_day_opus": None}
    samples = claude.normalize_usage(payload, NOW)
    assert [(s.window, s.used) for s in samples] == [("5h", 12.0), ("7d", 3.5)]
    assert samples[1].resets_at is None


def test_normalize_statusline():
    rl = {"five_hour": {"used_percentage": 44, "resets_at": 1788743999}, "seven_day": {"used_percentage": 27.5, "resets_at": 1788850799}, "spend_limit": {"used_percentage": 0}}
    samples = claude.normalize_statusline(rl, NOW)
    assert [(s.window, s.used, s.source) for s in samples] == [("5h", 44.0, "statusline"), ("7d", 27.5, "statusline")]
    assert samples[0].resets_at == datetime.fromtimestamp(1788743999, tz=timezone.utc)
    assert claude.normalize_statusline(None, NOW) == []
    assert claude.normalize_statusline({"five_hour": {}}, NOW) == []


def test_read_access_token_states(tmp_path):
    missing = tmp_path / "none.json"
    assert claude.read_access_token(missing)[0] is None
    creds = tmp_path / "c.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "abc", "expiresAt": int((time.time() - 10) * 1000)}}), encoding="utf-8")
    tok, warn = claude.read_access_token(creds)
    assert tok is None and "expired" in warn
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "abc", "expiresAt": int((time.time() + 3600) * 1000)}}), encoding="utf-8")
    tok, warn = claude.read_access_token(creds)
    assert tok == "abc" and warn is None


def test_collect_reports_warning_without_network(tmp_path):
    samples, warning = claude.collect(NOW, credentials_path=tmp_path / "missing.json")
    assert samples == [] and "credentials" in warning
