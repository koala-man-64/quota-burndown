from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from quota_burndown import ledger
from quota_burndown.plans import (
    Conflict, CreditObservation, PlanState, _categories, _cost_range, _transition_at,
    _required_workload_rate, _simulate_workload,
    forecast_snapshot,
)
from quota_burndown.store import Sample


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def plan(start=NOW, end=NOW + timedelta(days=2), amount="3.50",
         model="gpt-6-luna", speed="standard"):
    return {"amount": amount, "starts_at": start.isoformat(), "ends_at": end.isoformat(),
            "model": model, "speed": speed}


def observe(at=NOW, spent=None, balance=None, account="A", period="period-1",
            provider="codex", verified=True):
    return CreditObservation(provider, account, at, "provider", "credits" if provider == "codex" else "USD",
                             balance, spent, period, True, verified)


def test_revision_atomic_restart_independent_plans(tmp_path):
    state = PlanState(tmp_path, lambda: NOW)
    first = state.apply({"expected_revision": 0, "provider": "codex", "plan": plan()})
    assert first["revision"] == 1
    assert first["plans"]["codex"]["remaining"] == "3.50"
    assert first["plans"]["codex"]["confirmed_spent"] is None
    claude = plan(amount="1.125", model="claude-opus-5-5", speed="fast",
                  end=NOW + timedelta(hours=3))
    second = state.apply({"expected_revision": 1, "provider": "claude", "plan": claude})
    assert second["plans"]["claude"]["unit"] == "USD"
    assert second["plans"]["claude"]["required_rate_per_hour"] == "0.375"
    with pytest.raises(Conflict):
        state.apply({"expected_revision": 1, "provider": "codex", "plan": None})
    restored = PlanState(tmp_path, lambda: NOW)
    assert restored.snapshot()["plans"] == second["plans"]
    assert restored.apply({"expected_revision": 2, "provider": "codex", "plan": None})["plans"]["codex"] is None
    assert restored.snapshot()["plans"]["claude"]["amount"] == "1.125"


def test_plan_validation_future_and_expired(tmp_path):
    state = PlanState(tmp_path, lambda: NOW)
    future = state.apply({"expected_revision": 0, "provider": "codex",
                          "plan": plan(start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2))})
    assert future["plans"]["codex"]["status"] == "scheduled"
    assert future["plans"]["codex"]["required_rate_per_hour"] == "3.5"
    with pytest.raises(ValueError):
        state.apply({"expected_revision": 1, "provider": "codex", "plan": plan(start=NOW-timedelta(days=1))})
    with pytest.raises(ValueError):
        state.apply({"expected_revision": 1, "provider": "codex", "plan": plan(amount="NaN")})
    with pytest.raises(ValueError):
        state.apply({"expected_revision": 1, "provider": "codex", "plan": plan(model="unknown")})
    later = PlanState(tmp_path, lambda: NOW + timedelta(days=3))
    assert later.snapshot()["plans"]["codex"]["status"] == "expired"
    assert later.snapshot()["plans"]["codex"]["required_rate_per_hour"] is None


def test_paid_counter_only_duplicates_resets_and_account_switch(tmp_path):
    state = PlanState(tmp_path, lambda: NOW + timedelta(minutes=7))
    state.apply({"expected_revision": 0, "provider": "codex", "plan": plan()})
    assert state.ingest([observe(spent=None, balance="10")])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] is None
    assert state.ingest([observe(NOW + timedelta(minutes=1), spent="1.20", balance="9")])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] == "0"
    assert not state.ingest([observe(NOW + timedelta(minutes=1), spent="9", balance="0")])
    state.ingest([observe(NOW + timedelta(minutes=2), spent="1.45", balance="50")])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] == "0.25"
    state.ingest([observe(NOW + timedelta(minutes=3), spent="0.10", period="period-2")])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] == "0.25"
    state.ingest([observe(NOW + timedelta(minutes=4), spent="0.30", period="period-2")])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] == "0.45"
    state.ingest([observe(NOW + timedelta(minutes=5), spent="8", account="B")])
    state.ingest([observe(NOW + timedelta(minutes=6), spent="8.25", account="B")])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] == "0.70"
    assert state.snapshot()["plans"]["codex"]["progress_known"] is False
    assert PlanState(tmp_path, lambda: NOW + timedelta(minutes=7)).snapshot()["plans"]["codex"]["confirmed_spent"] == "0.70"


def test_unverified_scope_and_missing_counter_do_not_establish_spend(tmp_path):
    state = PlanState(tmp_path, lambda: NOW + timedelta(minutes=5))
    state.apply({"expected_revision": 0, "provider": "codex", "plan": plan()})
    state.ingest([observe(spent="4", verified=False)])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] is None
    state.ingest([observe(NOW + timedelta(minutes=1), spent="4", verified=True)])
    state.ingest([observe(NOW + timedelta(minutes=2), spent=None, balance="0")])
    state.ingest([observe(NOW + timedelta(minutes=3), spent="7")])
    state.ingest([observe(NOW + timedelta(minutes=4), spent="7.5")])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] == "0.5"
    assert state.snapshot()["plans"]["codex"]["progress_known"] is False


def test_zero_balance_is_distinct_from_missing_and_stale(tmp_path):
    state = PlanState(tmp_path, lambda: NOW)
    state.ingest([observe(balance="0")])
    observation = state.snapshot()["observations"]["codex"]
    assert observation["balance"] == "0"
    assert observation["cumulative_spent"] is None
    stale = PlanState(tmp_path, lambda: NOW + timedelta(hours=1))
    assert stale.snapshot()["observations"]["codex"]["freshness"] == "stale"


def test_missing_later_counter_preserves_confirmed_spending_and_deadline_freezes(tmp_path):
    clock = [NOW]
    state = PlanState(tmp_path, lambda: clock[0])
    state.apply({"expected_revision": 0, "provider": "codex",
                 "plan": plan(end=NOW+timedelta(hours=1))})
    state.ingest([observe(spent="10")])
    clock[0] = NOW+timedelta(minutes=10)
    state.ingest([observe(clock[0], spent="10.5")])
    clock[0] = NOW+timedelta(minutes=20)
    state.ingest([observe(clock[0], balance="20")])
    report = state.snapshot()["plans"]["codex"]
    assert report["confirmed_spent"] == "0.5"
    assert not report["progress_known"]
    clock[0] = NOW+timedelta(hours=2)
    state.ingest([observe(clock[0], spent="12")])
    assert state.snapshot()["plans"]["codex"]["confirmed_spent"] == "0.5"


def test_dates_need_explicit_offset_and_decimal_precision_is_bounded(tmp_path):
    state = PlanState(tmp_path, lambda: NOW)
    for raw in (dict(plan(), starts_at="2026-10-07T12:00:00"), plan(amount="1e-10000000")):
        with pytest.raises(ValueError):
            state.apply({"expected_revision": 0, "provider": "codex", "plan": raw})


def test_repace_budget_edit_completion(tmp_path):
    clock = [NOW]
    state = PlanState(tmp_path, lambda: clock[0])
    state.apply({"expected_revision": 0, "provider": "codex",
                 "plan": plan(amount="2", end=NOW + timedelta(hours=2))})
    clock[0] = NOW + timedelta(minutes=30)
    state.ingest([observe(spent="10")])
    state.ingest([observe(NOW + timedelta(minutes=30), spent="10.5")])
    clock[0] = NOW + timedelta(hours=1)
    assert state.snapshot()["plans"]["codex"]["required_rate_per_hour"] == "1.5"
    edited = state.apply({"expected_revision": 1, "provider": "codex",
                          "plan": plan(amount="1", end=NOW + timedelta(hours=2))})
    assert edited["plans"]["codex"]["confirmed_spent"] == "0.5"
    state.ingest([observe(NOW + timedelta(hours=1), spent="11.2")])
    assert state.snapshot()["plans"]["codex"]["status"] == "complete"
    assert state.snapshot()["plans"]["codex"]["required_rate_per_hour"] == "0"


def test_pricing_preserves_categories_and_precision():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t(provider,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens,reasoning_tokens,input_tokens_inferred)")
    conn.execute("INSERT INTO t VALUES ('codex',100,30,5,20,10,NULL)")
    row = conn.execute("SELECT * FROM t").fetchone()
    assert _categories(row) == (65, 30, 5, 20)
    low, high = _cost_range("codex", "gpt-6-luna", "fast", _categories(row))
    assert low == high == (Decimal(70)*Decimal("2.5") + Decimal(30)*Decimal(".25") + Decimal(20)*Decimal("12.5"))*2/1_000_000
    assert _cost_range("claude", "claude-opus-5-5", "standard", (100, 10, 10, 20)) == (
        Decimal("0.000852"), Decimal("0.000882"))
    assert _cost_range("claude", "claude-opus-5-5", "fast", (100, 10, 10, 20))[0] == Decimal("0.001704")
    conn.execute("INSERT INTO t VALUES ('claude',100,30,5,20,10,NULL)")
    claude_row = conn.execute("SELECT * FROM t WHERE provider='claude'").fetchone()
    assert _categories(claude_row) == (100, 30, 5, 20)


def test_forecast_selected_mix_multiple_resets_and_disabled(tmp_path):
    state = PlanState(tmp_path, lambda: NOW)
    state.apply({"expected_revision": 0, "provider": "codex",
                 "plan": plan(model="gpt-6-luna", end=NOW+timedelta(hours=12))})
    state.ingest([CreditObservation("codex", "A", NOW, "provider", "credits", "2", None, None, False)])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ledger.SCHEMA)
    conn.execute("""INSERT INTO events(provider,tool,kind,event_key,ts,model,input_tokens,cache_read_tokens,output_tokens,total_tokens)
                    VALUES('codex','codex','request','one',?,'gpt-6-luna',100,20,50,150)""",
                 ((NOW-timedelta(minutes=30)).isoformat(),))
    samples = [
        Sample(NOW-timedelta(hours=1), "codex", "300m:codex", 20, NOW+timedelta(hours=1), 300, "test"),
        Sample(NOW, "codex", "300m:codex", 30, NOW+timedelta(hours=1), 300, "test"),
        Sample(NOW-timedelta(hours=1), "codex", "10080m:codex", 40, NOW+timedelta(days=7), 10080, "test"),
        Sample(NOW, "codex", "10080m:codex", 50, NOW+timedelta(days=7), 10080, "test"),
    ]
    capacity = {"pools": [{"provider": "codex", "limit_id": "codex", "windows": [{
        "window": "300m", "window_min": 300, "remaining_pct": 70, "freshness": "fresh",
        "resets_at": (NOW+timedelta(hours=1)).isoformat(),
    }, {
        "window": "10080m", "window_min": 10080, "remaining_pct": 50, "freshness": "fresh",
        "resets_at": (NOW+timedelta(days=7)).isoformat(),
    }]}]}
    out = forecast_snapshot(state.snapshot(), conn, samples, capacity, NOW)["plans"]["codex"]["forecast"]
    assert out["available"]
    assert out["hypothetical"]
    assert out["mix_source"] == "model"
    assert out["windows"][0]["projected_resets"] == 3
    assert out["windows"][0]["remaining_included_tokens"] == 1050
    assert out["windows"][0]["projected_replenishment_tokens"] == 4500
    assert out["windows"][0]["key"] == "300m:codex"
    assert {w["key"] for w in out["windows"]} == {"300m:codex", "10080m:codex"}
    assert out["token_capacity_low"] > 0
    assert out["required_tokens_per_hour_low"] > out["required_paid_tokens_per_hour_low"]
    assert out["windows"][0]["transition_at"]
    assert forecast_snapshot(state.snapshot(), conn, [], capacity, NOW)["plans"]["codex"]["forecast"]["available"] is False
    long_snapshot = state.snapshot()
    long_snapshot["plans"]["codex"]["ends_at"] = (NOW+timedelta(days=3650)).isoformat()
    long_forecast = forecast_snapshot(long_snapshot, conn, samples, capacity, NOW)["plans"]["codex"]["forecast"]
    assert long_forecast["available"] is False
    assert "bounded" in long_forecast["note"]
    capacity["pools"][0]["windows"][0]["freshness"] = "stale"
    stale = forecast_snapshot(state.snapshot(), conn, samples, capacity, NOW)["plans"]["codex"]["forecast"]
    assert not stale["available"]
    assert stale["token_capacity_low"] > 0


def test_joint_windows_and_intervening_reset_require_more_total_work():
    windows = [
        {"remaining": 10.0, "renewed": 50.0, "reset": NOW+timedelta(hours=1), "period_minutes": 60},
        {"remaining": 70.0, "renewed": 1000.0, "reset": NOW+timedelta(days=7), "period_minutes": 10080},
    ]
    deadline = NOW + timedelta(hours=3)
    rate, transition = _required_workload_rate(100, windows, NOW, deadline)
    paid, observed_transition = _simulate_workload(rate, windows, NOW, deadline)
    assert rate > 100 / 3
    assert paid == pytest.approx(100, rel=1e-9)
    assert transition == observed_transition
    assert transition < NOW + timedelta(hours=1)


def test_transition_respects_replenishment():
    reset = NOW + timedelta(hours=1)
    # Five tokens an hour never consumes a renewed hundred-token allowance.
    assert _transition_at(NOW, NOW+timedelta(hours=12), reset, 60,
                          Decimal(5), Decimal(100), Decimal(5)) is None
    assert _transition_at(NOW, NOW+timedelta(hours=12), reset, 60,
                          Decimal(5), Decimal(100), Decimal(10)) == "2026-10-07T12:30:00Z"
