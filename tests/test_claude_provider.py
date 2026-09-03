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


def test_read_access_token_from_cli_credentials(tmp_path):
    no_file = tmp_path / "no_oauth"
    missing = tmp_path / "none.json"
    tok, warn, origin = claude.read_access_token(missing, no_file, env={})
    assert tok is None and "setup-token" in warn and origin == ""
    creds = tmp_path / "c.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "abc", "expiresAt": int((time.time() - 10) * 1000)}}), encoding="utf-8")
    tok, warn, origin = claude.read_access_token(creds, no_file, env={})
    assert tok is None and "expired" in warn and "setup-token" in warn
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "abc", "expiresAt": int((time.time() + 3600) * 1000)}}), encoding="utf-8")
    tok, warn, origin = claude.read_access_token(creds, no_file, env={})
    assert (tok, warn, origin) == ("abc", None, "credentials")


def test_read_access_token_prefers_env_then_long_lived_file(tmp_path):
    creds = tmp_path / "c.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "cli", "expiresAt": int((time.time() + 3600) * 1000)}}), encoding="utf-8")
    saved = tmp_path / "claude_oauth"
    saved.write_text("  long-lived-value\n", encoding="utf-8")
    assert claude.read_access_token(creds, saved, env={}) == ("long-lived-value", None, "file")
    assert claude.read_access_token(creds, saved, env={"QUOTA_BURNDOWN_CLAUDE_OAUTH": "from-env"}) == ("from-env", None, "env")
    saved.write_text("\n", encoding="utf-8")  # blank file falls through to the CLI credentials
    assert claude.read_access_token(creds, saved, env={}) == ("cli", None, "credentials")


def test_oauth_file_lives_in_the_data_home(paths):
    assert claude.oauth_file() == paths.home / "claude_oauth"


def test_save_long_lived_session_writes_and_restricts(tmp_path, monkeypatch):
    import os
    import subprocess

    calls = []

    class Result:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Result())
    target = tmp_path / "home" / "claude_oauth"
    path, warning = claude.save_long_lived_session("  long-lived-value\n", target)
    assert path == target and target.read_text(encoding="utf-8") == "long-lived-value"
    if os.name == "nt":
        assert warning is None and calls and calls[0][0] == "icacls" and calls[0][-1].endswith(":F")
    else:
        assert warning is None and oct(target.stat().st_mode & 0o777) == "0o600"
    for bad in ("", "   ", "two words", "<paste>"):
        try:
            claude.save_long_lived_session(bad, target)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert target.read_text(encoding="utf-8") == "long-lived-value"  # untouched by rejected input


def test_collect_reports_warning_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOTA_BURNDOWN_CLAUDE_OAUTH", raising=False)
    samples, warning = claude.collect(NOW, credentials_path=tmp_path / "missing.json", oauth_path=tmp_path / "no_oauth")
    assert samples == [] and "credentials" in warning
