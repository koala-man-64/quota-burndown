import json
import os
import sqlite3
import time
from pathlib import Path

from quota_burndown import ledger
from quota_burndown.usage import codex
from quota_burndown.usage.codex import Usage
from quota_burndown.util import parse_iso


def vec(inp, cached, out, reasoning=0, write=0):
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": write, "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def token_count(ts, ordinal, total, last):
    return json.dumps({"timestamp": ts, "ordinal": ordinal, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": last, "model_context_window": 258400}, "rate_limits": {"primary": {"used_percent": 1.0, "window_minutes": 10080, "resets_at": 1788747992}}}})


def turn_context(ts, ordinal, model, effort, turn=None):
    return json.dumps({"timestamp": ts, "ordinal": ordinal, "type": "turn_context", "payload": {"turn_id": turn or f"t{ordinal}", "model": model, "effort": effort, "cwd": "C:\\x"}})


def user_item(ts, ordinal, text):
    return json.dumps({"timestamp": ts, "ordinal": ordinal, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})


def user_event(ts, ordinal, text):
    return json.dumps({"timestamp": ts, "ordinal": ordinal, "type": "event_msg", "payload": {"type": "user_message", "message": text, "images": []}})


def session_meta(ts, sid, originator="Codex Desktop", extra=None):
    payload = {"id": sid, "session_id": sid, "timestamp": ts, "cwd": "C:\\x", "originator": originator, "cli_version": "0.153.0", "thread_source": "agent_created_thread"}
    payload.update(extra or {})
    return json.dumps({"timestamp": ts, "ordinal": 0, "type": "session_meta", "payload": payload})


def write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


SID = "01a05fbc-79a2-7782-abe0-858abb91ca8e"


def desktop_rollout(tmp_path):
    path = tmp_path / "codex" / "sessions" / "2026" / "09" / "01" / f"rollout-2026-09-01T20-29-42-{SID}.jsonl"
    write(path, [
        session_meta("2026-09-02T01:29:42Z", SID),
        turn_context("2026-09-02T01:29:43Z", 1, "gpt-5.6-luna", "low"),
        user_item("2026-09-02T01:29:44Z", 2, "<recommended_plugins>\n...\n</recommended_plugins>"),
        user_item("2026-09-02T01:29:44Z", 3, "fix the flaky test"),
        token_count("2026-09-02T01:29:50Z", 4, vec(46171, 44800, 176, 29), vec(46171, 44800, 176, 29)),
        token_count("2026-09-02T01:29:55Z", 5, vec(92260, 60928, 286, 42), vec(46265, 44800, 110, 13)),
        token_count("2026-09-02T01:29:55Z", 6, vec(92260, 60928, 286, 42), vec(46265, 44800, 110, 13)),  # repeated snapshot
        "not json",
        user_item("2026-09-02T01:30:00Z", 7, "<hook_prompt hook_run_id=\"stop:1\">say OK</hook_prompt>"),
        user_item("2026-09-02T01:30:00Z", 8, "# Files mentioned by the user:\n- a.py"),
        turn_context("2026-09-02T01:31:00Z", 9, "gpt-5.6-terra", "medium"),
        user_item("2026-09-02T01:31:01Z", 10, "now the docs"),
        token_count("2026-09-02T01:31:10Z", 11, vec(139106, 70000, 400, 60, 10), vec(46846, 9072, 114, 18, 10)),
    ])
    return path


def pk(ts, text):
    return codex.prompt_key(parse_iso(ts), text)


def test_parse_desktop_rollout_deltas_prompts_and_context(tmp_path):
    events = codex.parse_file(desktop_rollout(tmp_path))
    requests = [e for e in events if e.kind == ledger.REQUEST]
    prompts = [e for e in events if e.kind == ledger.PROMPT]
    assert [e.event_key for e in requests] == ["t1:46347:176", "t1:92546:286", "t9:139506:400"]
    first, second, third = requests
    assert (first.input_tokens, first.cache_read_tokens, first.output_tokens, first.reasoning_tokens, first.total_tokens) == (46171, 44800, 176, 29, 46347)
    assert (second.input_tokens, second.cache_read_tokens, second.output_tokens, second.reasoning_tokens, second.total_tokens) == (46089, 16128, 110, 13, 46199)
    assert (third.input_tokens, third.cache_write_tokens, third.output_tokens, third.total_tokens) == (46846, 10, 114, 46960)
    assert (first.model, first.effort, third.model, third.effort) == ("gpt-5.6-luna", "low", "gpt-5.6-terra", "medium")
    assert {e.tool for e in events} == {"codex-desktop"} and {e.thread for e in events} == {"root"} and {e.session_id for e in events} == {SID}
    assert [(p.event_key, p.model, p.effort) for p in prompts] == [
        (pk("2026-09-02T01:29:44Z", "fix the flaky test"), "gpt-5.6-luna", "low"),
        (pk("2026-09-02T01:31:01Z", "now the docs"), "gpt-5.6-terra", "medium"),
    ]


def test_parse_exec_rollout_prefers_user_message_events_and_fills_model_later(tmp_path):
    sid = "01a0253d-a744-7c83-9603-94797340ad3c"
    path = tmp_path / "codex" / "archived_sessions" / f"rollout-2026-08-17T18-00-00-{sid}.jsonl"
    write(path, [
        session_meta("2026-08-17T18:00:00Z", sid, originator="codex_exec"),
        user_event("2026-08-17T18:00:01Z", 1, "Reply with exactly: OK"),
        user_item("2026-08-17T18:00:01Z", 2, "Reply with exactly: OK"),
        turn_context("2026-08-17T18:00:02Z", 3, "gpt-5.6-luna", "low"),
        token_count("2026-08-17T18:00:05Z", 4, vec(1000, 0, 5), vec(1000, 0, 5)),
    ])
    events = codex.parse_file(path)
    prompts = [e for e in events if e.kind == ledger.PROMPT]
    assert [(p.event_key, p.tool, p.model, p.effort) for p in prompts] == [(pk("2026-08-17T18:00:01Z", "Reply with exactly: OK"), "codex-cli", "gpt-5.6-luna", "low")]
    assert [(e.event_key, e.total_tokens) for e in events if e.kind == ledger.REQUEST] == [("t3:1005:5", 1005)]


def test_copied_parent_history_collapses_onto_parent_rows(tmp_path, paths):
    home = tmp_path / "codex"
    parent_id = "01a0aaaa-0000-7000-8000-000000000001"
    child_id = "01a0bbbb-0000-7000-8000-000000000002"
    t_parent, t_child = "0199aaaa-1111-7000-8000-000000000001", "0199bbbb-2222-7000-8000-000000000002"
    task_started = lambda ts, ordinal, turn: json.dumps({"timestamp": ts, "ordinal": ordinal, "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}})
    shared = [
        user_item("2026-09-02T10:00:00Z", 1, "start the job"),
        task_started("2026-09-02T10:00:01Z", 2, t_parent),
    ]
    parent = home / "sessions" / "2026" / "09" / "02" / f"rollout-2026-09-02T10-00-00-{parent_id}.jsonl"
    write(parent, [
        session_meta("2026-09-02T10:00:00Z", parent_id),
        *shared,
        turn_context("2026-09-02T10:00:02Z", 3, "gpt-5.6-luna", "low", turn=t_parent),
        token_count("2026-09-02T10:00:05Z", 4, vec(1000, 0, 5), vec(1000, 0, 5)),
        token_count("2026-09-02T10:00:09Z", 5, vec(2000, 900, 10), vec(1000, 900, 5)),
    ])
    child = home / "sessions" / "2026" / "09" / "02" / f"rollout-2026-09-02T10-00-10-{child_id}.jsonl"
    spawn = {"thread_source": "subagent", "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}}}
    write(child, [
        session_meta("2026-09-02T10:00:10Z", child_id, extra=spawn),
        *shared,  # verbatim copy of the parent's history, no turn_context
        token_count("2026-09-02T10:00:05Z", 3, vec(1000, 0, 5), vec(1000, 0, 5)),
        token_count("2026-09-02T10:00:09Z", 4, vec(2000, 900, 10), vec(1000, 900, 5)),
        user_item("2026-09-02T10:00:11Z", 5, "explore the repo"),
        task_started("2026-09-02T10:00:12Z", 6, t_child),
        token_count("2026-09-02T10:00:15Z", 7, vec(3000, 1800, 15), vec(1000, 900, 5)),
        turn_context("2026-09-02T10:00:16Z", 8, "gpt-5.6-terra", "medium", turn=t_child),
        token_count("2026-09-02T10:00:20Z", 9, vec(3500, 2200, 20), vec(500, 400, 5)),
    ])
    child_events = codex.parse_file(child)
    copied = [e for e in child_events if e.kind == ledger.REQUEST and e.event_key.startswith(t_parent)]
    assert len(copied) == 2 and {(e.model, e.effort) for e in copied} == {("", "")}
    own = [e for e in child_events if e.kind == ledger.REQUEST and e.event_key.startswith(t_child)]
    assert len(own) == 2 and {(e.model, e.effort) for e in own} == {("gpt-5.6-terra", "medium")}
    child_prompts = [e for e in child_events if e.kind == ledger.PROMPT]
    assert [(p.model, p.effort) for p in child_prompts] == [("", ""), ("", "")]  # copied one and the spawn instruction stay blank

    for order in ((child, parent), (parent, child)):
        conn = ledger.connect(paths.usage_db)
        conn.execute("DELETE FROM events")
        conn.commit()
        for path in order:
            ledger.upsert(conn, codex.parse_file(path))
        rows = {r["event_key"]: r for r in ledger.rows(conn)}
        requests = [r for r in rows.values() if r["kind"] == ledger.REQUEST]
        assert len(requests) == 4, order
        shared_rows = [r for r in requests if r["event_key"].startswith(t_parent)]
        assert {(r["session_id"], r["thread"], r["model"], r["effort"]) for r in shared_rows} == {(parent_id, "root", "gpt-5.6-luna", "low")}, order
        own_rows = [r for r in requests if r["event_key"].startswith(t_child)]
        assert {(r["session_id"], r["thread"], r["model"]) for r in own_rows} == {(child_id, "subagent", "gpt-5.6-terra")}
        prompts = sorted((r["ts"], r["session_id"], r["model"]) for r in rows.values() if r["kind"] == ledger.PROMPT)
        assert prompts == [("2026-09-02T10:00:00Z", parent_id, "gpt-5.6-luna"), ("2026-09-02T10:00:11Z", child_id, "")], order
        totals = ledger.rollup(ledger.rows(conn), lambda r: r["thread"])
        assert (totals["root"].total, totals["subagent"].total) == (1005 + 1005, 1005 + 505)
        conn.close()


def test_subagent_rollout_uses_last_usage_for_inherited_baseline(tmp_path):
    sid = "01a06499-1659-7a42-8e2d-168e6289a303"
    path = tmp_path / "codex" / "sessions" / "2026" / "09" / "02" / f"rollout-2026-09-02T19-09-09-{sid}.jsonl"
    spawn = {"thread_source": "subagent", "source": {"subagent": {"thread_spawn": {"parent_thread_id": SID, "agent_nickname": "worker", "agent_path": "explore", "depth": 1}}}}
    write(path, [
        session_meta("2026-09-03T00:09:10Z", sid, extra=spawn),
        turn_context("2026-09-03T00:09:11Z", 1, "gpt-5.6-terra", "medium"),
        token_count("2026-09-03T00:09:20Z", 2, vec(232664, 200000, 900, 100), vec(46375, 44800, 110, 13)),  # parent baseline inherited
        token_count("2026-09-03T00:09:30Z", 3, vec(233000, 200100, 950, 110), vec(336, 100, 50, 10)),
        token_count("2026-09-03T00:09:40Z", 4, vec(500, 0, 20, 5), vec(500, 0, 20, 5)),  # counter reset
    ])
    events = codex.parse_file(path)
    requests = [e for e in events if e.kind == ledger.REQUEST]
    assert {e.thread for e in requests} == {"subagent"}
    assert [e.total_tokens for e in requests] == [46485, 386, 520]
    assert [e.input_tokens for e in requests] == [46375, 336, 500]


def test_late_turn_context_and_state_db_fill_missing_model(tmp_path):
    home = tmp_path / "codex"
    late = "01a06499-0000-7a42-8e2d-000000000001"
    path = home / "sessions" / "2026" / "08" / "07" / f"rollout-2026-08-07T01-10-41-{late}.jsonl"
    write(path, [
        session_meta("2026-08-07T01:10:41Z", late, extra={"thread_source": "subagent"}),
        token_count("2026-08-07T01:10:45Z", 1, vec(1000, 0, 5), vec(1000, 0, 5)),
        user_item("2026-08-07T01:10:46Z", 2, "go"),
        turn_context("2026-08-07T01:11:00Z", 3, "gpt-5.6-terra", "medium"),
        token_count("2026-08-07T01:11:05Z", 4, vec(2000, 900, 10), vec(1000, 900, 5)),
    ])
    events = codex.parse_file(path)
    requests = [e for e in events if e.kind == ledger.REQUEST]
    assert len(requests) == 2 and {(e.model, e.effort) for e in requests} == {("gpt-5.6-terra", "medium")}
    assert [(p.model, p.effort) for p in events if p.kind == ledger.PROMPT] == [("", "")]  # subagent prompt before any turn_context stays blank

    none = "01a06499-0000-7a42-8e2d-000000000002"
    path2 = home / "sessions" / "2026" / "08" / "07" / f"rollout-2026-08-07T02-00-00-{none}.jsonl"
    write(path2, [session_meta("2026-08-07T02:00:00Z", none), token_count("2026-08-07T02:00:05Z", 1, vec(10, 0, 1), vec(10, 0, 1))])
    assert {(e.model, e.effort) for e in codex.parse_file(path2)} == {("", "")}
    conn = sqlite3.connect(home / "state_5.sqlite")
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model TEXT, reasoning_effort TEXT, tokens_used INTEGER)")
    conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (none, "gpt-5.6-sol", "high", 11))
    conn.commit()
    conn.close()
    assert {(e.model, e.effort) for e in codex.parse_file(path2)} == {("gpt-5.6-sol", "high")}
    assert codex.home_of(path2) == home and codex.home_of(Path("C:/elsewhere/x.jsonl")).name == ".codex"
    assert codex.state_models(tmp_path / "nope") == {}


def test_usage_increment_cases():
    a = Usage(100, 50, 10, 2, 0, 102)
    b = Usage(250, 120, 25, 5, 0, 255)
    assert codex.usage_increment(a, a, None) == a
    assert codex.usage_increment(a, Usage(999, 0, 0, 0, 0, 999), None) == Usage()  # first snapshot, last invalid
    assert codex.usage_increment(b, None, a) == Usage(150, 70, 15, 3, 0, 153)
    assert codex.usage_increment(a, a, a) == Usage()
    assert codex.usage_increment(a, a, b) == a  # reset with a valid last vector
    assert codex.usage_increment(a, None, b) == Usage()
    assert codex.usage_vector({"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 20}) is None
    assert codex.usage_vector({"input_tokens": 10, "output_tokens": 5}) == Usage(10, 0, 0, 5, 0, 15)
    assert codex.usage_vector("nope") is None


def test_wrapper_detection_and_helpers():
    assert codex.is_wrapper("<hook_prompt hook_run_id=\"x\">hi</hook_prompt>")
    assert codex.is_wrapper("  <environment_context>\n  <cwd>x</cwd>")
    assert codex.is_wrapper("# Files mentioned by the user:\n- a")
    assert codex.is_wrapper("<permissions instructions>")
    assert not codex.is_wrapper("fix the bug in <b>")
    assert not codex.is_wrapper("2 < 3 is true")
    assert codex.input_text([{"type": "input_text", "text": "a"}, {"type": "input_image"}, {"type": "input_text", "text": "b"}]) == "a\nb"
    assert codex.input_text([{"type": "input_image"}]) is None and codex.input_text("x") is None
    assert codex.session_id_from_filename(Path(f"rollout-2026-09-01T20-29-42-{SID}.jsonl")) == SID
    assert codex.session_id_from_filename(Path("weird.jsonl")) == "weird"


def test_discover_and_ledger_rescan(tmp_path, paths):
    path = desktop_rollout(tmp_path)
    home = tmp_path / "codex"
    old = home / "archived_sessions" / "rollout-old.jsonl"
    write(old, [session_meta("2026-06-01T00:00:00Z", "x")])
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))
    assert [p.name for p, _, _ in codex.discover(home, since_days=30)] == [path.name]
    assert {p.name for p, _, _ in codex.discover(home, since_days=None)} == {path.name, "rollout-old.jsonl"}
    assert codex.discover(tmp_path / "nope") == []

    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, codex.parse_file(path))
    ledger.upsert(conn, codex.parse_file(path))
    assert ledger.count(conn) == 5
    totals = ledger.rollup(ledger.rows(conn), lambda r: r["provider"])["codex"]
    assert (totals.prompts, totals.requests, totals.total) == (2, 3, 46347 + 46199 + 46960)
    conn.close()
