from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from quota_burndown.capacity import CapacityState, Observation

NOW = datetime(2026, 9, 5, 18, tzinfo=timezone.utc)


def reading(**kw):
    values = dict(provider="codex", account_scope="account-a", limit_id="codex", window="300m",
                  window_min=300, used_pct=40, resets_at=NOW + timedelta(hours=2),
                  observed_at=NOW, received_at=NOW, source="app-server")
    return Observation(**(values | kw))


def pool(state):
    return next(p for p in state.publish()["pools"] if p["provider"] == "codex")


def test_models_share_pool_weekly_exhaustion_constrains(tmp_path):
    state = CapacityState(tmp_path, clock=lambda: NOW)
    state.ingest([reading(models=("gpt-5.6",)), reading(models=("gpt-6",)),
                  reading(window="10080m", window_min=10080, used_pct=100)])
    actual = pool(state)
    assert actual["models"] == ["gpt-5.6", "gpt-6"]
    assert actual["allowance_state"] == "exhausted"
    assert actual["constraining_window"] == "10080m"
    assert len(actual["windows"]) == 2


def test_duplicate_and_out_of_order_do_not_freshen(tmp_path):
    clock = [NOW]
    state = CapacityState(tmp_path, clock=lambda: clock[0])
    first = reading(observation_id="one")
    state.ingest([first]); before = state.publish()
    state.ingest([replace(first, received_at=NOW + timedelta(seconds=4)),
                  reading(observed_at=NOW-timedelta(minutes=2), used_pct=1)])
    clock[0] += timedelta(seconds=4)
    after = state.publish()
    assert after["revision"] == before["revision"]
    assert pool(state)["windows"][0]["observed_at"] == first.to_dict()["observed_at"]
    clock[0] += timedelta(seconds=60)
    assert pool(state)["freshness"] == "stale"
    assert state.read()["revision"] == before["revision"] + 1


def test_restart_full_read_reconciles_omissions_and_account(tmp_path):
    state = CapacityState(tmp_path, clock=lambda: NOW)
    state.ingest([reading(complete_snapshot=True), reading(window="10080m", window_min=10080)])
    old_instance = state.publish(persist=True)["instance_id"]
    state = CapacityState(tmp_path, clock=lambda: NOW + timedelta(seconds=1))
    assert state.instance_id != old_instance
    state.ingest([reading(observed_at=NOW+timedelta(seconds=1), complete_snapshot=True)])
    assert pool(state)["allowance_state"] == "unknown"
    omitted = next(w for w in pool(state)["windows"] if w["window"] == "10080m")
    assert omitted["used_pct"] is None and omitted["usable_pct"] is None
    state.ingest([reading(account_scope="account-b", observed_at=NOW+timedelta(seconds=1), complete_snapshot=True)])
    assert [p["account_scope"] for p in state.publish()["pools"] if p["provider"] == "codex"] == ["account-b"]


def test_expiry_unknown_and_desktop_freshness_different(tmp_path):
    clock = [NOW]
    state = CapacityState(tmp_path, clock=lambda: clock[0])
    state.ingest([reading(source="desktop-history", resets_at=NOW+timedelta(minutes=5))])
    clock[0] += timedelta(minutes=2)
    assert pool(state)["freshness"] == "fresh"
    clock[0] += timedelta(minutes=4)
    window = pool(state)["windows"][0]
    assert window["allowance_state"] == "unknown"
    assert window["remaining_pct"] is None
    assert window["freshness_ttl_s"] == 1200


def test_forecasts_need_three_recent_points_and_restart_after_corrections(tmp_path):
    state = CapacityState(tmp_path, clock=lambda: NOW)
    state.ingest([reading(observed_at=NOW-timedelta(minutes=20), used_pct=20),
                  reading(observed_at=NOW-timedelta(minutes=10), used_pct=25)])
    assert pool(state)["windows"][0]["conservative_rate_pph"] is None
    state.ingest([reading(used_pct=30)])
    win = pool(state)["windows"][0]
    assert win["recent_rate_pph"] == pytest.approx(30)
    assert win["conservative_rate_pph"] == max(win["recent_rate_pph"], win["whole_window_rate_pph"])
    state.ingest([reading(observed_at=NOW+timedelta(seconds=1), used_pct=15)])
    assert pool(state)["windows"][0]["conservative_rate_pph"] is None
    state.ingest([reading(observed_at=NOW+timedelta(seconds=2), resets_at=NOW+timedelta(hours=5), used_pct=1)])
    assert pool(state)["windows"][0]["recent_rate_pph"] is None


@pytest.mark.parametrize("value", [-1, 101, float("nan"), float("inf"), True])
def test_invalid_percent_never_available(tmp_path, value):
    state = CapacityState(tmp_path, clock=lambda: NOW)
    assert not state.ingest([reading(used_pct=value)])
    assert state.publish()["provider_states"]["codex"] == "unknown"


def test_unknown_and_shared_reserve_policy(tmp_path):
    state = CapacityState(tmp_path, clock=lambda: NOW)
    state.ingest([reading(used_pct=None)])
    assert pool(state)["allowance_state"] == "unknown"
    state.ingest([reading(used_pct=85, observed_at=NOW+timedelta(seconds=1))])
    state.set_policy(20)
    assert pool(state)["allowance_state"] == "reserve_reached"
    assert pool(state)["windows"][0]["usable_pct"] == 0
    with pytest.raises(ValueError): state.set_policy(True)


def claude_statusline(**kw):
    values = dict(provider="claude", account_scope="acct-local", limit_id="claude", window="300m",
                  window_min=300, used_pct=4, resets_at=NOW + timedelta(hours=3),
                  observed_at=NOW, received_at=NOW, source="statusline", reset_provenance="reported")
    return Observation(**(values | kw))


def claude_pool(state):
    return next(p for p in state.publish()["pools"] if p["provider"] == "claude")


def test_desktop_history_does_not_downgrade_a_valid_statusline_reading(tmp_path):
    clock = [NOW]
    state = CapacityState(tmp_path, clock=lambda: clock[0])
    state.ingest([claude_statusline()])
    # The desktop collector runs on its own cadence, so its reading always looks newer.
    stale_desktop = claude_statusline(source="desktop-history", used_pct=5, resets_at=None,
                                      reset_provenance="unknown", observed_at=NOW + timedelta(seconds=30),
                                      received_at=NOW + timedelta(seconds=30))
    clock[0] = NOW + timedelta(seconds=30)
    assert state.ingest([stale_desktop]) is False
    window = claude_pool(state)["windows"][0]
    assert (window["source"], window["reset_provenance"]) == ("statusline", "reported")
    assert (window["used_pct"], window["remaining_pct"], window["usable_pct"]) == (4.0, 96.0, 86.0)
    assert window["allowance_state"] == "available"

    # An inferred reset is still a downgrade of a reported one.
    clock[0] = NOW + timedelta(seconds=45)
    assert state.ingest([replace(stale_desktop, resets_at=NOW + timedelta(hours=5), reset_provenance="inferred",
                                 observed_at=clock[0], received_at=clock[0])]) is False
    assert claude_pool(state)["windows"][0]["source"] == "statusline"

    # Once the status-line reading expires the weaker source takes over again.
    clock[0] = NOW + timedelta(seconds=90)
    assert state.ingest([replace(stale_desktop, observed_at=clock[0], received_at=clock[0])]) is True
    window = claude_pool(state)["windows"][0]
    assert (window["source"], window["used_pct"], window["reset_provenance"]) == ("desktop-history", 5.0, "unknown")
    assert window["remaining_pct"] is None and window["allowance_state"] == "unknown"


def test_equal_reset_provenance_sources_still_take_over_by_recency(tmp_path):
    """Codex rollout readings report resets as well as the app server, so they are not held back."""
    state = CapacityState(tmp_path, clock=lambda: NOW + timedelta(seconds=10))
    state.ingest([reading(source="app-server")])
    assert state.ingest([reading(source="rollout", used_pct=41, observed_at=NOW + timedelta(seconds=5),
                                 received_at=NOW + timedelta(seconds=5))]) is True
    assert pool(state)["windows"][0]["source"] == "rollout"
