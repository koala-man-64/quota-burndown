import io
import json
import os
import time
import urllib.error

import pytest

from quota_burndown.capacity import CapacityState, freshness_seconds
from quota_burndown.integrations import run_claude_oauth
from quota_burndown.providers import claude_oauth

TOKEN = "sk-ant-oat-secret-value"
USAGE = {
    "five_hour": {"utilization": 4.0, "resets_at": "2026-10-06T22:00:00.493471+00:00"},
    "seven_day": {"utilization": 40.0, "resets_at": "2026-10-09T07:00:00.493492+00:00"},
    "seven_day_opus": None,
    "limits": [{"kind": "weekly_all", "percent": 40}],
}


def write_credentials(home, expires_in_s=3600, token=TOKEN):
    path = home / ".credentials.json"
    oauth = {"accessToken": token, "refreshToken": "refresh-secret", "expiresAt": int((time.time() + expires_in_s) * 1000)}
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return path


class Stop:
    """Records each wait and stops the loop after `limit` waits."""

    def __init__(self, limit):
        self.limit, self.waits = limit, []

    def is_set(self):
        return len(self.waits) >= self.limit

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.is_set()


def http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError(claude_oauth.ENDPOINT, code, "error", headers, io.BytesIO(b""))


def run(home, fetch, limit=1):
    published, health, stop = [], [], Stop(limit)
    run_claude_oauth(stop, published.extend, lambda *args: health.append(args), claude_home=home, fetch=fetch)
    return published, health, stop.waits


def test_read_token_reports_safe_reasons(tmp_path):
    path = tmp_path / ".credentials.json"
    with pytest.raises(claude_oauth.Unavailable, match="not found"):
        claude_oauth.read_token(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(claude_oauth.Unavailable, match="unreadable"):
        claude_oauth.read_token(path)
    path.write_text(json.dumps({"claudeAiOauth": {}}), encoding="utf-8")
    with pytest.raises(claude_oauth.Unavailable, match="no Claude subscription"):
        claude_oauth.read_token(path)
    write_credentials(tmp_path, expires_in_s=10)
    with pytest.raises(claude_oauth.Unavailable, match="expired") as raised:
        claude_oauth.read_token(path)
    assert TOKEN not in str(raised.value)
    write_credentials(tmp_path)
    assert claude_oauth.read_token(path) == TOKEN


def test_quota_maps_reported_windows_and_drops_malformed():
    assert claude_oauth.quota(USAGE) == {
        "five_hour": {"used_percentage": 4.0, "resets_at": "2026-10-06T22:00:00.493471+00:00"},
        "seven_day": {"used_percentage": 40.0, "resets_at": "2026-10-09T07:00:00.493492+00:00"},
    }
    assert claude_oauth.quota({"five_hour": {"utilization": 130}, "seven_day": {"utilization": True}}) == {
        "five_hour": {"used_percentage": 100.0}}
    assert claude_oauth.quota({"five_hour": None, "seven_day": {"utilization": -1}}) == {}


def test_poll_publishes_into_the_shared_claude_pool(tmp_path):
    write_credentials(tmp_path)
    tokens = []
    published, health, waits = run(tmp_path, lambda token: tokens.append(token) or USAGE)
    assert tokens == [TOKEN]
    assert waits == [300.0]
    assert health[-1] == ("claude_oauth", "healthy", None)
    assert {(o.window, o.used_pct, o.source, o.reset_provenance) for o in published} == {
        ("300m", 4.0, "oauth-usage", "reported"), ("10080m", 40.0, "oauth-usage", "reported")}

    state = CapacityState(tmp_path / "state")
    state.ingest(published)
    pool = next(p for p in state.publish()["pools"] if p["provider"] == "claude")
    # Same scope as the status-line and desktop readings, so it replaces them in one pool.
    assert pool["id"] == "claude:acct-25bf8e1a2393f110:claude"
    weekly = next(w for w in pool["windows"] if w["window"] == "10080m")
    assert weekly["used_pct"] == 40.0 and weekly["freshness"] == "fresh" and weekly["freshness_ttl_s"] == 600
    assert weekly["resets_at"] == "2026-10-09T07:00:00Z"
    assert freshness_seconds("oauth-usage") == 600


def test_expired_token_is_not_used_or_refreshed(tmp_path):
    path = write_credentials(tmp_path, expires_in_s=-60)
    before = path.read_text(encoding="utf-8")
    published, health, waits = run(tmp_path, lambda token: pytest.fail("expired token was sent"))
    assert published == [] and waits == [60.0]
    assert health[-1][1] == "unavailable" and "expired" in health[-1][2]
    assert path.read_text(encoding="utf-8") == before


def test_rejected_token_waits_for_new_credentials(tmp_path):
    path = write_credentials(tmp_path)
    calls = []

    def fetch(token):
        calls.append(token)
        if len(calls) == 1:
            raise http_error(401)
        return USAGE

    class RefreshAfterTwoWaits(Stop):
        def wait(self, seconds):
            if len(self.waits) == 2:
                stat = path.stat()
                write_credentials(tmp_path, token="sk-ant-oat-refreshed")
                os.utime(path, (stat.st_atime, stat.st_mtime + 5))
            return super().wait(seconds)

    published, health, stop = [], [], RefreshAfterTwoWaits(4)
    run_claude_oauth(stop, published.extend, lambda *args: health.append(args), claude_home=tmp_path, fetch=fetch)
    # Rejected once, then idle until the file changes, then polled with the new token.
    assert calls == [TOKEN, "sk-ant-oat-refreshed"]
    assert stop.waits == [60.0, 60.0, 60.0, 300.0]
    assert len(published) == 2
    assert all(TOKEN not in str(item) for item in health)


def test_rate_limit_and_errors_back_off(tmp_path):
    write_credentials(tmp_path)
    errors = iter([http_error(429, "900"), http_error(503), OSError("offline"), http_error(429)])

    def fetch(token):
        raise next(errors)

    published, health, waits = run(tmp_path, fetch, limit=4)
    assert published == []
    assert waits == [900.0, 600.0, 1200.0, 1800.0]
    assert [h[2] for h in health[1:]] == ["HTTP 429", "HTTP 503", "OSError", "HTTP 429"]


def test_redirects_are_not_followed_with_the_token():
    import http.server
    import threading
    import urllib.request

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/usage", headers={"Authorization": "Bearer " + TOKEN})
        with pytest.raises(urllib.error.HTTPError) as raised:
            claude_oauth._OPENER.open(request, timeout=5)
        assert raised.value.code == 302
        assert seen == [("/usage", "Bearer " + TOKEN)]
    finally:
        server.shutdown()
        server.server_close()


def test_non_finite_retry_after_uses_backoff():
    assert claude_oauth.retry_after(http_error(429, "nan"), 300.0) == 300.0
    assert claude_oauth.retry_after(http_error(429, "inf"), 300.0) == 300.0


def test_protocol_errors_back_off_instead_of_killing_the_poller(tmp_path):
    import http.client
    write_credentials(tmp_path)

    def fetch(token):
        raise http.client.IncompleteRead(b"")

    published, health, waits = run(tmp_path, fetch, limit=2)
    assert waits == [300.0, 600.0]
    assert health[-1] == ("claude_oauth", "degraded", "IncompleteRead")


def test_missing_reset_is_not_labeled_reported(tmp_path):
    write_credentials(tmp_path)
    published, _, _ = run(tmp_path, lambda token: {"seven_day": {"utilization": 40}})
    assert [(o.resets_at, o.reset_provenance) for o in published] == [(None, "unknown")]
