from datetime import timedelta

import pytest

from quota_burndown import client
from quota_burndown.capacity import CapacityState, Observation
from quota_burndown.util import now_utc


def test_disconnected_separate_from_source_freshness(paths, monkeypatch):
    now = now_utc()
    state = CapacityState(paths.home)
    state.ingest([Observation("codex", "a", "codex", "300m", 300, 30,
                  now+timedelta(hours=1), now-timedelta(seconds=10), now, "app-server")])
    state.publish(persist=True)
    def offline(_): raise OSError("offline")
    monkeypatch.setattr(client, "_open", offline)
    snapshot = client.read_capacity(paths.home)
    assert snapshot["service_state"] == "disconnected"
    pool = next(p for p in snapshot["pools"] if p["provider"] == "codex")
    assert pool["freshness"] == "fresh"
    monkeypatch.setattr(client, "now_utc", lambda: now+timedelta(hours=2))
    pool = next(p for p in client.read_capacity(paths.home)["pools"] if p["provider"] == "codex")
    assert pool["freshness"] == "stale"
    assert pool["allowance_state"] == "unknown"
    assert pool["windows"][0]["usable_pct"] is None


@pytest.mark.parametrize("url", ["https://127.0.0.1", "http://example.com", "http://127.0.0.1/path", "http://user@localhost"])
def test_client_rejects_nonlocal_or_credential_urls(paths, url):
    with pytest.raises(ValueError): client.service_url(paths.home, url)
