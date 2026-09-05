import json
from datetime import datetime, timezone

from quota_burndown import render


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def snapshot():
    return {
        "schema_version": 1, "instance_id": "test", "revision": 8,
        "generated_at": "2026-09-05T12:00:00Z", "policy": {"reserve_pct": 10, "presets": [5, 10, 20]},
        "collector_health": {"codex": {"state": "ok"}}, "unreported_in_flight_usage": "unknown",
        "pools": [{"provider": "codex", "account_scope": "me", "limit_id": "shared", "label": "Codex <pool>", "models": ["gpt<x>", "spark"], "mapping_confidence": "observed", "windows": [{
            "window": "7d", "remaining_pct": 30, "usable_pct": 20, "resets_at": "2026-09-10T12:00:00Z",
            "runway_minutes": 90, "freshness": "stale", "source_age_s": 120, "source": "app<server>", "allowance_state": "available",
        }]}],
    }


def test_capacity_matrix_renders_shared_pool_unknowns_and_escapes():
    html = render.capacity_matrix_html(snapshot(), live=True)
    assert "Pools are shared allowances" in html and "30%" in html and "20%" in html
    assert "stale ·" in html and 'data-source-age="120.000"' in html and "Codex &lt;pool&gt;" in html and "gpt&lt;x&gt;" in html
    assert 'data-reserve="10" class="on"' in html
    assert "unknown" in render.capacity_matrix_html(None)


def test_live_page_uses_sse_without_refresh_and_static_page_is_dated(store):
    live = render.render_html(store, now=NOW, capacity=snapshot(), live=True)
    assert "EventSource('/v1/capacity/events')" in live and "fetch('/v1/policy'" in live
    assert 'http-equiv="refresh"' not in live and "static fallback" not in live
    static = render.render_html(store, now=NOW, capacity=snapshot())
    assert 'http-equiv="refresh"' in static and "static fallback generated" in static


def test_live_markup_polls_efficiency_and_keeps_observed_zero_remaining(store):
    data = snapshot()
    data["provider_states"] = {"codex": {"state": "observed"}, "claude": {"state": "unknown"}, "antigravity": {"state": "unknown"}}
    data["pools"][0]["windows"][0]["remaining_pct"] = 0
    data["pools"][0]["windows"][0]["allowance_state"] = "exhausted"
    html = render.render_html(store, now=NOW, capacity=data, live=True)
    assert "Provider observations: codex: observed, claude: unknown, antigravity: unknown" in html
    assert "fetch('/v1/usage'" in html and "setInterval(efficiency, 30000)" in html
    assert "stream disconnected · reconnecting" in html and "0%" in html


def test_static_write_reads_the_persisted_capacity_snapshot(store, paths):
    paths.home.mkdir(parents=True, exist_ok=True)
    (paths.home / "capacity.json").write_text(json.dumps(snapshot()), encoding="utf-8")
    render.write_html(store, paths.html, now=NOW)
    html = paths.html.read_text(encoding="utf-8")
    assert "Codex &lt;pool&gt;" in html and "static fallback generated" in html


def test_capacity_matrix_treats_malformed_optional_content_as_unknown():
    html = render.capacity_matrix_html({"pools": ["bad", {"windows": ["bad"]}], "policy": "bad", "collector_health": {"codex": "bad"}})
    assert "unknown" in html and "codex: unknown" in html


def test_capacity_matrix_uses_real_snapshot_provider_states(tmp_path):
    from datetime import timedelta
    from quota_burndown.capacity import CapacityState, Observation
    state = CapacityState(tmp_path, clock=lambda: NOW)
    state.ingest([Observation("codex", "scope", "codex", "300m", 300, 12,
                  NOW + timedelta(hours=3), NOW, NOW, "app-server")])
    html = render.capacity_matrix_html(state.publish(), live=True)
    assert "Provider observations: codex: observed, claude: unknown" in html
    assert "constraining: 300m" in html
