import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quota_burndown.config import antigravity_ls_params
from quota_burndown.integrations import run_antigravity
from quota_burndown.model import canonical_sample
from quota_burndown.providers.antigravity_client import (
    fetch_quota_summary,
    observations_from_summary,
)
from quota_burndown.store import Sample

UTC = timezone.utc


SAMPLE_QUOTA_SUMMARY = {
    "response": {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "window": "5h",
                        "remainingFraction": 0.40,
                        "resetTime": "2026-09-10T16:32:15Z",
                    },
                    {
                        "bucketId": "gemini-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "window": "weekly",
                        "remainingFraction": 0.785,
                        "resetTime": "2026-09-15T17:48:44Z",
                    },
                ],
            }
        ]
    }
}


def test_observations_from_summary():
    now = datetime(2026, 9, 10, 11, 0, 0, tzinfo=UTC)
    obs = observations_from_summary(SAMPLE_QUOTA_SUMMARY, observed_at=now)
    assert len(obs) == 2

    # 5h window
    obs_5h = next(o for o in obs if o.window_min == 300)
    assert obs_5h.provider == "antigravity"
    assert obs_5h.limit_id == "gemini"
    assert obs_5h.window == "300m"
    assert obs_5h.used_pct == 60.0  # (1 - 0.40) * 100
    assert obs_5h.resets_at == datetime(2026, 9, 10, 16, 32, 15, tzinfo=UTC)
    assert obs_5h.source == "app-server"
    assert obs_5h.reset_provenance == "reported"

    # weekly window
    obs_weekly = next(o for o in obs if o.window_min == 10080)
    assert obs_weekly.provider == "antigravity"
    assert obs_weekly.limit_id == "gemini"
    assert obs_weekly.window == "10080m"
    assert obs_weekly.used_pct == 21.5  # (1 - 0.785) * 100 = 21.5
    assert obs_weekly.resets_at == datetime(2026, 9, 15, 17, 48, 44, tzinfo=UTC)
    assert obs_weekly.source == "app-server"


def test_observations_from_empty_summary():
    assert observations_from_summary({}) == []
    assert observations_from_summary({"userQuotaSummaries": []}) == []
    assert observations_from_summary({"userQuotaSummaries": [{"name": "unknown-quota"}]}) == []


def test_antigravity_ls_params_env_override(monkeypatch):
    monkeypatch.setenv("QUOTA_BURNDOWN_ANTIGRAVITY_PORT", "12345")
    monkeypatch.setenv("QUOTA_BURNDOWN_ANTIGRAVITY_CSRF_TOKEN", "test-token-xyz")
    port, token = antigravity_ls_params()
    assert port == 12345
    assert token == "test-token-xyz"


def test_antigravity_ls_params_from_log(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOTA_BURNDOWN_ANTIGRAVITY_PORT", raising=False)
    monkeypatch.delenv("QUOTA_BURNDOWN_ANTIGRAVITY_CSRF_TOKEN", raising=False)

    log_file = tmp_path / "main.log"
    log_file.write_text(
        "[2026-09-09 10:00:00.000] [info] Spawning: language_server.exe --csrf_token old-token\n"
        "[2026-09-09 10:00:01.000] [info] [LanguageServerProcessManager] Local: https://127.0.0.1:40001/\n"
        "[2026-09-10 08:00:00.000] [info] Spawning: language_server.exe --csrf_token active-token-abc\n"
        "[2026-09-10 08:00:02.000] [info] [LanguageServerProcessManager] Local: https://127.0.0.1:54094/\n",
        encoding="utf-8",
    )

    port, token = antigravity_ls_params([log_file])
    assert port == 54094
    assert token == "active-token-abc"


def test_antigravity_ls_params_missing_log():
    port, token = antigravity_ls_params([Path("non_existent_file.log")])
    assert port is None
    assert token is None


def test_fetch_quota_summary():
    fake_body = json.dumps(SAMPLE_QUOTA_SUMMARY).encode("utf-8")
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = fake_body
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        res = fetch_quota_summary(54094, "my-token")
        assert res == SAMPLE_QUOTA_SUMMARY
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-codeium-csrf-token") == "my-token"
        assert req.full_url == "https://127.0.0.1:54094/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"


def test_observations_from_flat_summary():
    flat = {
        "userQuotaSummaries": [
            {
                "name": "gemini-5h",
                "remainingFraction": 0.50,
                "resetTime": "2026-09-10T16:00:00Z",
            }
        ]
    }
    obs = observations_from_summary(flat)
    assert len(obs) == 1
    assert obs[0].window_min == 300
    assert obs[0].used_pct == 50.0


def test_canonical_sample_antigravity():
    s_5h = Sample(
        ts=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
        provider="antigravity",
        window="300m:gemini",
        used=60.0,
        resets_at=datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
        window_min=300,
        source="app-server",
    )
    canon_5h = canonical_sample(s_5h)
    assert canon_5h.window == "5h:gemini"

    s_weekly = Sample(
        ts=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
        provider="antigravity",
        window="10080m:gemini",
        used=22.0,
        resets_at=datetime(2026, 9, 15, 17, 0, tzinfo=UTC),
        window_min=10080,
        source="app-server",
    )
    canon_weekly = canonical_sample(s_weekly)
    assert canon_weekly.window == "7d:gemini"


def test_run_antigravity_lifecycle():
    stop = threading.Event()
    published = []
    health_reports = []

    def mock_publish(obs):
        published.append(obs)
        stop.set()

    def mock_health(provider, status, err=None):
        health_reports.append((provider, status, err))

    with patch("quota_burndown.config.antigravity_ls_params", return_value=(54094, "token-123")), \
         patch("quota_burndown.providers.antigravity_client.fetch_quota_summary", return_value=SAMPLE_QUOTA_SUMMARY):
        run_antigravity(stop, mock_publish, mock_health, active=lambda: False)

    assert len(published) == 1
    assert len(published[0]) == 2
    assert ("antigravity", "healthy", None) in health_reports
