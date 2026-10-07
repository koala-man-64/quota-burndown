from datetime import datetime, timezone
from decimal import Decimal

from quota_burndown.paid_credits import codex, claude

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def test_codex_shared_balance_is_not_summed_and_zero_is_reported():
    item = {"credits": {"balance": "0", "hasCredits": False}}
    rows = codex({"rateLimitsByLimitId": {"codex": item, "spark": item}}, "account", NOW)
    assert len(rows) == 1
    assert rows[0].balance == Decimal(0)
    assert rows[0].enabled is False
    assert rows[0].cumulative_spent is None


def test_codex_omitted_notification_is_no_news_but_complete_missing_is_unknown():
    assert codex({}, "account", NOW) == []
    row = codex({}, "account", NOW, complete=True)[0]
    assert row.balance is None and row.enabled is None
    rows = codex({"rateLimitsByLimitId": {
        "codex": {"credits": {"balance": "1"}},
        "spark": {"credits": {"balance": "2"}},
    }}, "account", NOW)
    assert rows[0].balance is None


def test_claude_requires_currency_scale_and_verified_account():
    extra = {"is_enabled": True, "used_credits": "1234", "balance": "2345",
             "currency": "USD", "decimal_places": 2, "resets_at": "2026-11-01T00:00:00Z"}
    row = claude({"extra_usage": extra}, "account", NOW, account_verified=True)[0]
    assert row.cumulative_spent == Decimal("12.34")
    assert row.balance == Decimal("23.45")
    assert row.counter_period == extra["resets_at"]
    assert claude({"extra_usage": extra}, "local", NOW)[0].cumulative_spent is None
    for field in ("currency", "decimal_places"):
        missing = dict(extra)
        missing.pop(field)
        row = claude({"extra_usage": missing}, "account", NOW, account_verified=True)[0]
        assert row.cumulative_spent is None and row.balance is None


def test_disabled_and_malformed_never_manufacture_spend():
    row = claude({"extra_usage": {"is_enabled": False, "used_credits": None}}, "local", NOW)[0]
    assert row.enabled is False and row.cumulative_spent is None
    for value in (True, "NaN", "Infinity", "-1", {}):
        assert codex({"credits": {"balance": value}}, "account", NOW)[0].balance is None


def test_verified_claude_quota_and_spending_share_account_scope(tmp_path):
    import json
    import time
    from quota_burndown.integrations import run_claude_oauth
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, _):
            self.stopped = True
            return True
    (tmp_path / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
        'accessToken': 'fixture-token', 'expiresAt': (time.time()+3600)*1000}}))
    data = dict(five_hour={'utilization': 10, 'resets_at': '2026-10-08T00:00:00Z'},
                account_id="account-123", extra_usage={
        "is_enabled": True, "used_credits": 10, "currency": "USD", "decimal_places": 0})
    quotas, paid = [], []
    run_claude_oauth(Stop(), quotas.extend, lambda *args: None, tmp_path, lambda _: data,
                    publish_credits=paid.extend)
    assert paid[0].account_verified
    assert paid[0].account_scope == quotas[0].account_scope

