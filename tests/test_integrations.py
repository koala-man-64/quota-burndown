from datetime import datetime, timezone
import io
import json
import threading
from pathlib import Path

from quota_burndown import integrations
from quota_burndown.integrations import missing_windows, observations_from_limits


UTC = timezone.utc
T0 = datetime(2026, 9, 5, tzinfo=UTC)


def test_limit_ids_define_shared_pools_and_invalid_percent_is_unknown():
    payload = {"rateLimitsByLimitId": {
        "codex": {"models": ["gpt-5.6-sol", "gpt-5.6-terra"], "primary": {"used_percent": 75, "windowDurationMins": 10080, "resets_at": 1_789_000_000},
                    "secondary": {"used_percent": "no", "window_minutes": 300}},
        "spark": {"models": ["gpt-5.3-codex-spark"], "primary": {"usedPercent": 10, "windowMinutes": 300}},
    }}
    items = observations_from_limits(payload, provider="codex", account_scope="acct-one", source="app-server", observed_at=T0)
    assert {(item.limit_id, item.window_min, item.used_pct) for item in items} == {("codex", 10080, 75.0), ("codex", 300, None), ("spark", 300, 10.0)}
    assert items[0].models == ("gpt-5.6-sol", "gpt-5.6-terra")


def test_missing_secondary_window_becomes_explicitly_unknown_once():
    initial = observations_from_limits({"codex": {"primary": {"used_percent": 10, "window_minutes": 10080}, "secondary": {"used_percent": 2, "window_minutes": 300}}},
                                       provider="codex", account_scope="acct-one", source="app-server", observed_at=T0)
    previous = { (item.provider, item.account_scope, item.limit_id, item.window): item for item in initial }
    current = observations_from_limits({"codex": {"primary": {"used_percent": 11, "window_minutes": 10080}}},
                                       provider="codex", account_scope="acct-one", source="app-server", observed_at=T0)
    unknown = missing_windows(previous, current)
    assert len(unknown) == 1 and unknown[0].window_min == 300 and unknown[0].used_pct is None
    assert missing_windows(previous, current) == []


def test_out_of_range_and_missing_windows_do_not_invent_capacity():
    payload = {"rateLimitsByLimitId": {"codex": {"primary": {"used_percent": 101, "window_minutes": 300}, "secondary": {"used_percent": 10}}}}
    items = observations_from_limits(payload, provider="codex", account_scope="acct-one", source="app-server", observed_at=T0)
    assert len(items) == 1 and items[0].used_pct is None and items[0].window_min == 300


def test_documented_claude_statusline_windows_have_implicit_duration():
    items = observations_from_limits({"five_hour": {"used_percentage": 31, "resets_at": "2026-09-05T14:00:00Z"},
                                      "seven_day": {"used_percentage": 49}}, provider="claude", account_scope="acct-one",
                                      source="statusline", observed_at=T0)
    assert {(item.window_min, item.used_pct) for item in items} == {(300, 31.0), (10080, 49.0)}
    assert {item.limit_id for item in items} == {"claude"}
    assert {item.window for item in items} == {"300m", "10080m"}


def test_native_adapter_publishes_notification_and_uses_jsonrpc_initialized(monkeypatch):
    init = {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}
    account = {"jsonrpc": "2.0", "id": 2, "result": {"account": {"id": "a"}}}
    update = {"jsonrpc": "2.0", "method": "account/rateLimits/updated", "params": {
        "rateLimitsByLimitId": {"codex": {"primary": {"used_percent": 20, "window_minutes": 300}}}}}

    class Process:
        def __init__(self):
            class Input(io.StringIO):
                def close(self): pass
            self.stdin = Input()
            self.stdout = io.StringIO(json.dumps(init) + "\n" + json.dumps(account) + "\n" + json.dumps(update) + "\n")
            self.done = False
        def poll(self): return None
        def terminate(self): self.done = True
        def wait(self, timeout=None): return 0
        def kill(self): self.done = True
    process = Process()
    monkeypatch.setattr(integrations, "_command", lambda: ["fake", "app-server"])
    monkeypatch.setattr(integrations.subprocess, "Popen", lambda *args, **kwargs: process)
    published, states = [], []
    integrations._run_native(threading.Event(), published.append, lambda *args: states.append(args), lambda: True)
    assert published[0][0].used_pct == 20
    sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert any(item["method"] == "initialized" and "id" not in item for item in sent)
    assert states[0] == ("codex", "starting", None)


def test_active_rollout_tail_keeps_partial_line_and_actual_limit_id(tmp_path):
    from quota_burndown.integrations import ActiveRolloutTail, AdapterContext
    day = datetime.now().date()
    path = tmp_path / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / "rollout-x.jsonl"
    path.parent.mkdir(parents=True)
    context = AdapterContext("acct-one", T0)
    turn = {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "turn_id": "turn"}}
    event = {"timestamp": datetime.now(UTC).isoformat(), "type": "event_msg", "payload": {"rate_limits": {"limit_id": "codex_bengalfox", "primary": {"used_percent": 12, "window_minutes": 300}}}}
    path.write_text(json.dumps(turn) + "\n" + json.dumps(event) + "\n{" , encoding="utf-8")
    tail = ActiveRolloutTail(tmp_path, context)
    rows = tail.poll()
    assert len(rows) == 1 and rows[0].limit_id == "codex_bengalfox" and rows[0].models == ("gpt-5.6-sol",)
    assert tail.poll() == []
    with path.open("a", encoding="utf-8") as out: out.write("}\n")
    assert tail.poll() == []  # completed partial is malformed and never advances capacity


def test_tail_skips_giant_lines_without_unbounded_reads_and_handles_truncation(tmp_path):
    from quota_burndown.integrations import ActiveRolloutTail, AdapterContext
    day = datetime.now(UTC).date()
    path = tmp_path / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / "rollout-large.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'x' * 3_000_000)
    tail = ActiveRolloutTail(tmp_path, AdapterContext("acct", T0))
    assert tail.poll() == []
    assert len(tail.state[path]["partial"]) <= 65_536
    offset = tail.state[path]["offset"]
    with path.open('ab') as handle: handle.write(b'x' * 2_000_000)
    assert tail.poll() == []
    assert tail.state[path]["offset"] - offset <= 1_048_576
    path.write_text('{}\n', encoding='utf-8')
    assert tail.poll() == [] and tail.state[path]["offset"] == path.stat().st_size
