from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json

import pytest

from quota_burndown import ledger, render, usage_report
from quota_burndown.ledger import Event

NOW = datetime(2026, 9, 8, 18, tzinfo=timezone.utc)


def request(key, model="gpt-6-astra", total=100, **kwargs):
    return Event("codex", "codex-desktop", ledger.REQUEST, key, NOW,
                 model=model, total_tokens=total, **kwargs)


def test_daily_models_totals_coverage_and_model_switches(paths, monkeypatch):
    monkeypatch.setattr(usage_report, "to_local", lambda ts: ts.astimezone(timezone.utc))
    conn = ledger.connect(paths.usage_db)
    events = [
        request("1", effort="low", input_tokens=900, cache_read_tokens=800, reasoning_tokens=50),
        request("2", effort="high"),
        request("3", "gpt-5.3-codex-spark", 60),
        request("4", "", 40), request("5", "   ", None),
        replace(request("6", total=20), ts=NOW - timedelta(days=6)),
        replace(request("7", total=9000), ts=NOW - timedelta(days=7)),
        replace(request("8", total=9000), ts=NOW + timedelta(seconds=1)),
        replace(request("9", total=9000), kind=ledger.PROMPT),
        replace(request("10", total=55), provider="claude"),
        replace(request("11", "gemini", None, input_tokens_inferred=999), provider="antigravity"),
    ]
    ledger.upsert(conn, events)
    ledger.upsert(conn, events)  # ingesting the same request twice cannot inflate bars
    data = usage_report.efficiency_payload(conn, NOW)["daily_models"]
    assert data["dates"] == [f"2026-09-{day:02}" for day in range(2, 9)]
    groups = {group["provider"]: group for group in data["providers"]}
    codex = groups["codex"]
    models = {item["model"]: item for item in codex["models"]}
    assert models["gpt-6-astra"]["total_tokens"] == 220
    assert models["gpt-6-astra"]["tokens"] == [20, 0, 0, 0, 0, 0, 200]
    assert models[""]["label"] == "Unknown model"
    assert models[""]["excluded_requests"] == 1
    assert codex["daily_tokens"] == [20, 0, 0, 0, 0, 0, 300]
    assert (codex["total_tokens"], codex["recorded_requests"], codex["excluded_requests"]) == (320, 5, 1)
    assert groups["claude"]["total_tokens"] == 55
    assert groups["antigravity"]["recorded_requests"] == 0
    assert groups["antigravity"]["excluded_requests"] == 1
    old_color = models["gpt-6-astra"]["color"]
    ledger.upsert(conn, [request("12", "a-new-model", 1)])
    refreshed = usage_report.daily_model_payload(conn, NOW)
    assert next(model["color"] for group in refreshed["providers"] for model in group["models"] if model["model"] == "gpt-6-astra") == old_color
    assert json.loads(json.dumps(data)) == data
    conn.close()


@pytest.mark.parametrize("now,boundary,offset_before,offset_after,transition", [
    (datetime(2026, 3, 10, 18, tzinfo=timezone.utc), datetime(2026, 3, 4, 6, tzinfo=timezone.utc), -6, -5, datetime(2026, 3, 8, 8, tzinfo=timezone.utc)),
    (datetime(2026, 11, 3, 18, tzinfo=timezone.utc), datetime(2026, 10, 28, 5, tzinfo=timezone.utc), -5, -6, datetime(2026, 11, 1, 7, tzinfo=timezone.utc)),
])
def test_local_calendar_bounds_across_dst(paths, monkeypatch, now, boundary, offset_before, offset_after, transition):
    # Explicit US Central offsets keep this test portable without an external tzdata package.
    def local(ts):
        offset = offset_before if ts < transition else offset_after
        return ts.astimezone(timezone(timedelta(hours=offset)))
    monkeypatch.setattr(usage_report, "to_local", local)
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, [
        replace(request("outside", total=999), ts=boundary - timedelta(seconds=1)),
        replace(request("inside", total=17), ts=boundary),
        replace(request("today", total=23), ts=now),
    ])
    data = usage_report.daily_model_payload(conn, now)
    group = next(group for group in data["providers"] if group["provider"] == "codex")
    assert group["daily_tokens"] == [17, 0, 0, 0, 0, 0, 23]
    assert data["dates"][0] == local(boundary).date().isoformat()
    conn.close()


def test_render_models_escaped_complete_and_unavailable(store, paths):
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, [request("1", '<script>alert("x")</script>', 1250), request("2", "unknown-tokens", None)])
    data = usage_report.daily_model_payload(conn, NOW)
    content = render.daily_models_html(data)
    assert '<script>alert' not in content
    assert '&lt;script&gt;alert' in content
    assert 'Incomplete data: 1 requests excluded' in content
    assert 'Recorded token totals unavailable.' in content
    assert '<caption>Codex recorded tokens' in content and 'scope="row"' in content
    assert '1,250' in content and 'unknown-tokens' in content
    static = render.render_html(store, now=NOW, usage_db=paths.usage_db)
    live = render.render_html(store, now=NOW, usage_db=paths.usage_db, live=True)
    assert content in static and content in live
    assert static.index('No historical charts to show.') < static.index('Recorded tokens by model') < static.index('<h2>Token usage')
    assert 'dailyModels(data.daily_models_html)' in live
    conn.close()


def test_empty_and_reported_zero_are_distinct(paths):
    conn = ledger.connect(paths.usage_db)
    empty = usage_report.daily_model_payload(conn, NOW)
    assert all(group["recorded_requests"] == 0 for group in empty["providers"])
    assert render.daily_models_html(empty).count('Recorded token totals unavailable.') == 3
    ledger.upsert(conn, [request("zero", total=0)])
    content = render.daily_models_html(usage_report.daily_model_payload(conn, NOW))
    assert content.count('Recorded token totals unavailable.') == 2
    assert '<svg class="model-token-chart"' in content
    conn.close()
