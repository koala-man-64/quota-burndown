from datetime import timedelta

from quota_burndown.capacity import CapacityState, provider_groups
from test_capacity import NOW, reading


def test_empty_state_has_eight_unknown_slots_without_fake_pools(tmp_path):
    snapshot = CapacityState(tmp_path, clock=lambda: NOW).read()
    groups = snapshot["provider_groups"]
    assert [g["provider"] for g in groups] == ["codex", "antigravity", "claude"]
    assert [len(g["limits"]) for g in groups] == [3, 2, 3]
    for group in groups:
        for limit in group["limits"]:
            assert limit["display_only"] and limit["pool_id"] is None
            window = limit["window"]
            assert window["used_pct"] is None and window["remaining_pct"] is None
            assert window["observed_at"] is None and window["resets_at"] is None
            assert window["freshness"] == "unknown"
    assert len(snapshot["pools"]) == 1  # pre-existing unsupported Antigravity marker only


def test_grouped_slots_reference_shared_pools_and_live_readings(tmp_path):
    state = CapacityState(tmp_path, clock=lambda: NOW)
    state.ingest([
        reading(window="10080m", window_min=10080, used_pct=37, models=("gpt-6-astra", "gpt-5.6-luna")),
        reading(limit_id="codex_bengalfox", used_pct=0),
        reading(limit_id="codex_bengalfox", window="10080m", window_min=10080, used_pct=100),
        reading(provider="claude", limit_id="claude", source="desktop-history", used_pct=72),
        reading(provider="claude", limit_id="claude", window="10080m", window_min=10080, used_pct=25),
    ])
    snapshot = state.publish()
    codex, antigravity, claude = snapshot["provider_groups"]
    assert [limit["window"]["used_pct"] for limit in codex["limits"]] == [37, 0, 100]
    assert codex["limits"][1]["pool_id"] == codex["limits"][2]["pool_id"]
    assert codex["limits"][1]["constraining_window"] == "10080m"
    assert claude["limits"][0]["pool_id"] == claude["limits"][1]["pool_id"]
    assert claude["limits"][2]["pool_id"] is None
    assert "Fable" in claude["limits"][2]["availability_reason"]
    assert all(limit["window"]["used_pct"] is None for limit in antigravity["limits"])
    assert len([p for p in snapshot["pools"] if p["provider"] == "codex"]) == 2


def test_other_observed_windows_are_preserved_and_groups_do_not_refresh_age(tmp_path):
    clock = [NOW]
    state = CapacityState(tmp_path, clock=lambda: clock[0])
    state.ingest([reading(limit_id="future-limit", window="60m", window_min=60, used_pct=18)])
    first = state.publish()
    assert first["provider_groups"][0]["limits"][-1]["window"]["used_pct"] == 18
    clock[0] += timedelta(seconds=5)
    assert state.publish()["revision"] == first["revision"]
    clock[0] += timedelta(seconds=60)
    snapshot = state.publish()
    assert snapshot["provider_groups"][0]["limits"][-1]["window"]["freshness"] == "stale"


def test_group_copy_cannot_modify_authoritative_window(tmp_path):
    state = CapacityState(tmp_path, clock=lambda: NOW)
    state.ingest([reading(window="10080m", window_min=10080)])
    snapshot = state.publish()
    groups = provider_groups(snapshot["pools"])
    groups[0]["limits"][0]["window"]["used_pct"] = 1
    assert next(p for p in snapshot["pools"] if p["provider"] == "codex")["windows"][0]["used_pct"] == 40
