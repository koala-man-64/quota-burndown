import json
import os
import time

from quota_burndown import ledger
from quota_burndown.usage import claude


def assistant(mid, ts, output, block="text", model="claude-fable-5-1", effort="xhigh", session="s1", thinking=5):
    return json.dumps({
        "type": "assistant", "uuid": f"a-{mid}-{block}", "sessionId": session, "timestamp": ts, "effort": effort,
        "requestId": f"req_{mid}", "isSidechain": False,
        "message": {
            "id": mid, "model": model, "role": "assistant", "content": [{"type": block}],
            "usage": {
                "input_tokens": 2, "cache_creation_input_tokens": 34187, "cache_read_input_tokens": 39565,
                "output_tokens": output, "output_tokens_details": {"thinking_tokens": thinking}, "service_tier": "standard",
            },
        },
    })


def user(uuid, ts, text, session="s1"):
    return json.dumps({"type": "user", "uuid": uuid, "sessionId": session, "timestamp": ts, "message": {"role": "user", "content": text}})


def tool_result(uuid, ts):
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        "toolUseResult": {"stdout": "ok"},
    })


def write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_home(tmp_path):
    home = tmp_path / "claude"
    main = home / "projects" / "C--proj" / "s1.jsonl"
    write(main, [
        user("u1", "2026-09-03T14:00:00.000Z", "fix the bug"),
        assistant("m1", "2026-09-03T14:00:05.000Z", 100, block="thinking"),
        assistant("m1", "2026-09-03T14:00:05.000Z", 300, block="text"),
        json.dumps({"type": "system", "subtype": "stop_hook_summary", "timestamp": "2026-09-03T14:00:06Z"}),
        tool_result("u2", "2026-09-03T14:00:07.000Z"),
        "{not json",
        assistant("m2", "2026-09-03T14:00:09.000Z", 50, block="tool_use", effort="high"),
        user("u3", "2026-09-03T14:05:00.000Z", "thanks"),
    ])
    sub = home / "projects" / "C--proj" / "s1" / "subagents" / "agent-abc.jsonl"
    write(sub, [user("u4", "2026-09-03T14:00:08.000Z", "explore"), assistant("m3", "2026-09-03T14:00:09.000Z", 20, model="claude-sonnet-5", effort="medium")])
    wf = home / "projects" / "C--proj" / "s1" / "subagents" / "workflows" / "wf_1" / "agent-x.jsonl"
    write(wf, [assistant("m4", "2026-09-03T14:00:10.000Z", 1)])
    return home, main, sub, wf


def test_parse_file_dedupes_requests_and_counts_prompts(tmp_path):
    home, main, sub, wf = make_home(tmp_path)
    events = claude.parse_file(main)
    requests = {e.event_key: e for e in events if e.kind == ledger.REQUEST}
    prompts = [e for e in events if e.kind == ledger.PROMPT]
    assert set(requests) == {"m1", "m2"}
    m1 = requests["m1"]
    assert (m1.output_tokens, m1.input_tokens, m1.cache_read_tokens, m1.cache_write_tokens, m1.reasoning_tokens) == (300, 2, 39565, 34187, 5)
    assert m1.total_tokens == 2 + 39565 + 34187 + 300
    assert (m1.model, m1.effort, m1.thread, m1.session_id, m1.tool, m1.provider) == ("claude-fable-5-1", "xhigh", "main", "s1", "claude-code", "claude")
    assert [p.event_key for p in prompts] == ["u1", "u3"]
    assert (prompts[0].model, prompts[0].effort) == ("claude-fable-5-1", "xhigh")
    assert (prompts[1].model, prompts[1].effort) == ("", "")  # no request followed it yet
    assert all(p.input_tokens is None and p.total_tokens is None for p in prompts)

    sub_events = claude.parse_file(sub)
    assert {e.thread for e in sub_events} == {"subagent"}
    assert {e.kind for e in sub_events} == {ledger.REQUEST, ledger.PROMPT}
    assert claude.parse_file(wf)[0].thread == "workflow"


def test_discover_respects_since_days_and_missing_home(tmp_path):
    home, main, sub, wf = make_home(tmp_path)
    old = time.time() - 40 * 86400
    os.utime(wf, (old, old))
    found = {p.name for p, _, _ in claude.discover(home, since_days=30)}
    assert found == {"s1.jsonl", "agent-abc.jsonl"}
    assert {p.name for p, _, _ in claude.discover(home, since_days=None)} == {"s1.jsonl", "agent-abc.jsonl", "agent-x.jsonl"}
    assert claude.discover(tmp_path / "nope") == []


def test_rescan_is_idempotent_in_ledger(tmp_path, paths):
    home, main, sub, wf = make_home(tmp_path)
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, claude.parse_file(main))
    ledger.upsert(conn, claude.parse_file(main))
    assert ledger.count(conn) == 4
    with open(main, "a", encoding="utf-8") as fh:
        fh.write(assistant("m5", "2026-09-03T14:06:00.000Z", 9) + "\n")
    ledger.upsert(conn, claude.parse_file(main))
    assert ledger.count(conn) == 5
    row = conn.execute("SELECT model FROM events WHERE kind = 'prompt' AND event_key = 'u3'").fetchone()
    assert row["model"] == "claude-fable-5-1"
    conn.close()
