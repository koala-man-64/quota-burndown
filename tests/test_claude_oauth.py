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


def no_refresh():
    pytest.fail("a valid token asked the CLI to refresh")


def run(home, fetch, limit=1, refresh=no_refresh):
    published, health, stop = [], [], Stop(limit)
    run_claude_oauth(stop, published.extend, lambda *args: health.append(args), claude_home=home, fetch=fetch, refresh=refresh)
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


def test_expired_token_is_not_sent_and_is_handed_to_the_cli_once(tmp_path):
    path = write_credentials(tmp_path, expires_in_s=-60)
    before = path.read_text(encoding="utf-8")
    refreshes = []
    published, health, waits = run(tmp_path, lambda token: pytest.fail("expired token was sent"), limit=2,
                                   refresh=lambda: refreshes.append(1))
    assert published == [] and waits == [60.0, 60.0]
    assert refreshes == [1]
    assert health[1] == ("claude_oauth", "unavailable", "sign-in token expired; asking Claude Code to refresh it")
    assert health[-1][1] == "unavailable" and "Claude Code did not refresh it" in health[-1][2]
    # The poller itself never writes the credentials file.
    assert path.read_text(encoding="utf-8") == before


def test_cli_refresh_lets_the_next_poll_use_the_new_token(tmp_path):
    write_credentials(tmp_path, expires_in_s=-60)
    tokens = []
    published, health, waits = run(tmp_path, lambda token: tokens.append(token) or USAGE,
                                   refresh=lambda: write_credentials(tmp_path, token="sk-ant-oat-refreshed") and None)
    assert tokens == ["sk-ant-oat-refreshed"]
    assert waits == [300.0] and len(published) == 2
    assert health[-1] == ("claude_oauth", "healthy", None)


def test_failed_cli_refresh_is_reported_and_retried_after_cooldown(tmp_path, monkeypatch):
    import quota_burndown.integrations as integrations
    write_credentials(tmp_path, expires_in_s=-60)
    clock = [1000.0]
    monkeypatch.setattr(integrations.time, "monotonic", lambda: clock[0])

    class Clocked(Stop):
        def wait(self, seconds):
            clock[0] += seconds
            return super().wait(seconds)

    refreshes = []
    published, health, stop = [], [], Clocked(31)
    run_claude_oauth(stop, published.extend, lambda *args: health.append(args), claude_home=tmp_path,
                     fetch=lambda token: pytest.fail("expired token was sent"),
                     refresh=lambda: refreshes.append(clock[0]) or "claude CLI not found")
    # One attempt, 30 one-minute checks during the cooldown, then a second attempt.
    assert refreshes == [1000.0, 2800.0]
    assert health[2][2] == ("sign-in token expired; Claude Code refresh failed (claude CLI not found); "
                            "next try in 30 min, or run `claude` in a terminal to sign in")
    # The second failure doubles the wait.
    assert "next try in 60 min" in health[-1][2]
    assert all(TOKEN not in str(item) for item in health)


def test_a_raising_refresh_does_not_stop_the_poller(tmp_path):
    write_credentials(tmp_path, expires_in_s=-60)

    def refresh():
        raise ValueError("embedded null character")

    published, health, waits = run(tmp_path, lambda token: pytest.fail("expired token was sent"), limit=2, refresh=refresh)
    assert waits == [60.0, 60.0]
    assert "Claude Code refresh failed (claude CLI failed (ValueError))" in health[-1][2]


def test_cli_environment_drops_auth_overrides_but_keeps_what_the_cli_needs():
    environ = {"PATH": "bin", "CLAUDE_CONFIG_DIR": "cfg", "CLAUDE_CODE_GIT_BASH_PATH": "bash.exe", "CLAUDECODE": "1",
               "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH": "1", "CLAUDE_CODE_OAUTH_TOKEN": "host-token",
               "ANTHROPIC_API_KEY": "key", "anthropic_base_url": "x"}
    # The CLI runs from another directory, so a relative config dir is resolved here.
    assert claude_oauth.cli_environment(environ) == {"PATH": "bin", "CLAUDE_CONFIG_DIR": os.path.abspath("cfg"),
                                                     "CLAUDE_CODE_GIT_BASH_PATH": "bash.exe"}


def test_cli_command_never_comes_from_the_current_directory(tmp_path, monkeypatch):
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    monkeypatch.setattr(claude_oauth.Path, "home", lambda: home)
    # Windows lookups answer with a relative name when the hit is in the current directory.
    monkeypatch.setattr(claude_oauth.shutil, "which", lambda name, path=None: ".\\claude.EXE")
    assert claude_oauth.cli_command({"PATH": ""}) is None
    absolute = str(elsewhere / "claude.exe")
    monkeypatch.setattr(claude_oauth.shutil, "which", lambda name, path=None: absolute)
    assert claude_oauth.cli_command({"PATH": ""}) == [absolute, *claude_oauth.REFRESH_ARGS]
    native = home / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    native.parent.mkdir(parents=True)
    native.write_bytes(b"")
    assert claude_oauth.cli_command({"PATH": ""})[0] == str(native)
    # The empty tools list survives Windows argument quoting.
    import subprocess
    assert '--tools ""' in subprocess.list2cmdline(claude_oauth.REFRESH_ARGS)


def child(monkeypatch, code):
    import sys
    monkeypatch.setattr(claude_oauth, "cli_command", lambda environ: [sys.executable, "-c", code])


def test_refresh_via_cli_runs_in_its_workdir_without_auth_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    child(monkeypatch, "import os, sys; open('seen.txt', 'w').write(os.getcwd()); "
                       "sys.exit(3 if 'ANTHROPIC_API_KEY' in os.environ else 0)")
    workdir = tmp_path / "claude-refresh"
    assert claude_oauth.refresh_via_cli(workdir) is None
    assert os.path.samefile((workdir / "seen.txt").read_text(), workdir)
    child(monkeypatch, "import sys; sys.exit(3)")
    assert claude_oauth.refresh_via_cli(workdir) == "claude CLI exited 3"


def test_refresh_via_cli_reports_missing_hung_and_unstartable_clis(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(claude_oauth, "cli_command", lambda environ: None)
    assert claude_oauth.refresh_via_cli(tmp_path) == "claude CLI not found"
    child(monkeypatch, "import time; time.sleep(5)")
    assert claude_oauth.refresh_via_cli(tmp_path, timeout=0.5) == "claude CLI timed out"
    stop = threading.Event()
    stop.set()
    assert claude_oauth.refresh_via_cli(tmp_path, stop) == "service stopping"
    monkeypatch.setattr(claude_oauth, "cli_command", lambda environ: [str(tmp_path / "missing.exe")])
    assert claude_oauth.refresh_via_cli(tmp_path) == "claude CLI failed to start (FileNotFoundError)"


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
