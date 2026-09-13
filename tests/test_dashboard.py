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


def grouped_snapshot():
    data = snapshot()
    data["pools"][0]["id"] = "codex-general"
    window = data["pools"][0]["windows"][0]
    window.update({"used_pct": 70, "window": "7d", "whole_window_rate_pph": 0.24, "recent_rate_pph": 1.5})
    unknown = {"used_pct": None, "remaining_pct": None, "usable_pct": None,
               "resets_at": None, "runway_minutes": None, "freshness": "unknown",
               "source_age_s": None, "source": "unavailable"}
    data["provider_groups"] = [
        {"provider": "codex", "label": "Codex", "limits": [
            {"id": "general-weekly", "label": "General weekly", "pool_id": "codex-general",
             "account_scope": "me", "models": ["Codex"], "mapping_confidence": "observed",
             "constraining_window": "7d", "window": window},
            {"id": "spark-5h", "label": "Spark 5-hour", "pool_id": None,
             "account_scope": None, "models": ["Spark"], "mapping_confidence": "unknown",
             "constraining_window": None, "window": unknown, "availability_reason": "not reported"},
        ]},
        {"provider": "antigravity", "label": "Antigravity", "limits": [
            {"id": "gemini-5h", "label": "Gemini 5-hour", "pool_id": None,
             "account_scope": None, "models": ["Gemini"], "mapping_confidence": "unknown",
             "constraining_window": None, "window": unknown, "availability_reason": "quota unsupported"},
        ]},
        {"provider": "claude", "label": "Claude", "limits": [
            {"id": "fable-weekly", "label": "Fable weekly", "pool_id": None,
             "account_scope": None, "models": ["Fable"], "mapping_confidence": "unknown",
             "constraining_window": None, "window": unknown, "availability_reason": "quota unsupported"},
        ]},
    ]
    data["pools"][0]["windows"].append({"window": "5h", "used_pct": 20, "remaining_pct": 80,
                                             "usable_pct": 70, "resets_at": "2026-09-05T15:00:00Z",
                                             "source": "app-server"})
    return data


def test_capacity_groups_render_known_slots_and_unknown_slots_without_fake_values():
    html = render.capacity_matrix_html(grouped_snapshot(), live=True)
    assert html.index(">Codex<") < html.index(">Antigravity<") < html.index(">Claude<")
    assert "General weekly" in html and "70%" in html and "30%" in html and "20%" in html
    assert "whole: 0.24 pp/hour; last hour: 1.50 pp/hour" in html
    assert "Gemini 5-hour" in html and "Fable weekly" in html
    assert "quota unsupported" in html and "unreported" in html
    assert "Other observed windows" in html and "· 5h<" in html and "80%" in html


def test_capacity_matrix_renders_shared_pool_unknowns_and_escapes():
    html = render.capacity_matrix_html(snapshot(), live=True)
    assert "Pools are shared allowances" in html and "30%" in html and "20%" in html
    assert "stale ·" in html and 'data-source-age="120.000"' in html and "Codex &lt;pool&gt;" in html and "gpt&lt;x&gt;" in html
    assert 'data-reserve="10" class="on"' in html
    assert "unknown" in render.capacity_matrix_html(None)


def test_live_page_uses_sse_without_refresh_and_static_page_is_dated(store):
    live = render.render_html(store, now=NOW, capacity=snapshot(), live=True)
    assert '<section id="capacity"' not in live
    static = render.render_html(store, now=NOW, capacity=snapshot())
    assert '<section id="capacity"' not in static
    assert 'http-equiv="refresh"' in static and "static fallback generated" in static


def test_live_page_omits_capacity_markup_even_with_exhausted_snapshot(store):
    data = snapshot()
    data["provider_states"] = {"codex": {"state": "observed"}, "claude": {"state": "unknown"}, "antigravity": {"state": "unknown"}}
    data["pools"][0]["windows"][0]["remaining_pct"] = 0
    data["pools"][0]["windows"][0]["allowance_state"] = "exhausted"
    html = render.render_html(store, now=NOW, capacity=data, live=True)
    assert '<section id="capacity"' not in html
    assert "Provider observations:" not in html and "stream disconnected · reconnecting" not in html


def test_static_write_omits_the_persisted_capacity_snapshot(store, paths):
    paths.home.mkdir(parents=True, exist_ok=True)
    (paths.home / "capacity.json").write_text(json.dumps(snapshot()), encoding="utf-8")
    render.write_html(store, paths.html, now=NOW)
    html = paths.html.read_text(encoding="utf-8")
    assert "Codex &lt;pool&gt;" not in html and '<section id="capacity"' not in html
    assert "static fallback generated" in html


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


def test_capacity_matrix_renders_all_requested_slots_from_capacity_state(tmp_path):
    from datetime import timedelta
    from quota_burndown.capacity import CapacityState, Observation
    state = CapacityState(tmp_path, clock=lambda: NOW)
    state.ingest([Observation("codex", "scope", "codex", "10080m", 10080, 35,
                  NOW + timedelta(days=7), NOW - timedelta(seconds=12), NOW, "app-server")])
    html = render.capacity_matrix_html(state.publish(), live=True)
    for label in ("General weekly", "GPT-5.3-Codex-Spark · 5-hour", "GPT-5.3-Codex-Spark · weekly",
                  "Gemini · 5-hour", "Gemini · weekly", "Current session · 5-hour",
                  "All models · weekly", "Fable · weekly"):
        assert label in html
    assert "Configured Antigravity sources report activity" in html
    assert "Supported Claude status-line and desktop history sources do not report Fable" in html
    assert "data-freshness-deadline=" in html and "reset: reported; observation: reported" in html
