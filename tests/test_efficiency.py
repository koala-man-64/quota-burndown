from datetime import datetime, timedelta, timezone

from quota_burndown import ledger, usage_report
from quota_burndown.ledger import Event


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def test_efficiency_normalizes_codex_cache_and_preserves_unknown_antigravity(paths):
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, [
        Event("codex", "desktop", ledger.REQUEST, "one", NOW - timedelta(minutes=2), session_id="s<1>", model="gpt<x>", effort="high", input_tokens=100, cache_read_tokens=60, cache_write_tokens=10, output_tokens=20, reasoning_tokens=10, total_tokens=120),
        Event("codex", "desktop", ledger.REQUEST, "two", NOW - timedelta(minutes=1), session_id="s<1>", model="gpt<x>", effort="high", input_tokens=50, cache_read_tokens=10, output_tokens=30, reasoning_tokens=5, total_tokens=90),
        Event("antigravity", "app", ledger.REQUEST, "three", NOW, session_id="a", model="gemini", effort="high", input_tokens_inferred=99),
    ])
    data = usage_report.efficiency_payload(conn, NOW)
    conn.close()
    codex = next(row for row in data["by_model_effort"] if row["provider"] == "codex")
    assert (codex["uncached_input"], codex["cached_input"], codex["output"]) == (70, 80, 50)
    assert codex["median_tokens_per_request"] == 105
    assert codex["reasoning_share_of_output_pct"] == 30.0
    unknown = next(row for row in data["by_model_effort"] if row["provider"] == "antigravity")
    assert unknown["exact"] is False and unknown["uncached_input"] is None and unknown["median_tokens_per_request"] is None


def test_efficiency_keeps_all_missing_exact_values_unknown(paths):
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, [Event("claude", "cli", ledger.REQUEST, "missing", NOW, session_id="<session>", model="<model>", effort="low")])
    row = usage_report.efficiency_payload(conn, NOW)["by_session"][0]
    conn.close()
    assert row["uncached_input"] is None and row["cached_input"] is None and row["output"] is None
    assert row["median_tokens_per_request"] is None and row["reasoning_share_of_output_pct"] is None
    assert row["anchor"].startswith("efficiency-session-")
