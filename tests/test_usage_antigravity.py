import json
import sqlite3
from datetime import datetime, timezone

import pytest

from quota_burndown import ledger
from quota_burndown.proto import LENGTH_DELIMITED, VARINT
from quota_burndown.proto import encode_field as f
from quota_burndown.proto import encode_message as m
from quota_burndown.usage import antigravity

CONV = "c7ccd21b-102b-4118-a024-d997e26206c1"
UTC = timezone.utc


def gen_blob(context_tokens, model="gemini-3.8-flash"):
    inner = m(f(1, VARINT, context_tokens), f(4, VARINT, 256000))
    gen = m(f(10, LENGTH_DELIMITED, inner), f(2, LENGTH_DELIMITED, model), f(3, LENGTH_DELIMITED, "model_enum"))
    return m(f(1, LENGTH_DELIMITED, m(f(9, LENGTH_DELIMITED, gen), f(3, LENGTH_DELIMITED, f"request_id-{context_tokens}"))))


def executor_blob(variant="gemini-3.8-flash-high"):
    return m(f(1, LENGTH_DELIMITED, variant), f(2, LENGTH_DELIMITED, "jetski-autonomous-mode"), f(7, VARINT, 3))


def step(index, kind, ts, content=""):
    return json.dumps({"step_index": index, "source": "USER_EXPLICIT" if kind == "USER_INPUT" else "MODEL", "type": kind, "status": "DONE", "created_at": ts, "content": content})


SETTING = "<USER_REQUEST>hi</USER_REQUEST>\n<USER_SETTINGS_CHANGE>changed setting `Model Selection` from None to Gemini 3.8 Flash (High).</USER_SETTINGS_CHANGE>"


def make_home(tmp_path, gens=(28806, 30358), executor=executor_blob(), transcript=True):
    home = tmp_path / "antigravity"
    db_dir = home / "conversations"
    db_dir.mkdir(parents=True)
    db = db_dir / f"{CONV}.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
    conn.execute("CREATE TABLE executor_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    for i, tokens in enumerate(gens):
        blob = gen_blob(tokens)
        conn.execute("INSERT INTO gen_metadata VALUES (?, ?, ?)", (i, blob, len(blob)))
    if executor is not None:
        conn.execute("INSERT INTO executor_metadata VALUES (0, ?)", (executor,))
    conn.commit()
    conn.close()
    if transcript:
        path = antigravity.transcript_path(home, CONV)
        path.parent.mkdir(parents=True)
        path.write_text("\n".join([
            step(0, "USER_INPUT", "2026-09-03T14:29:52Z", SETTING),
            step(1, "PLANNER_RESPONSE", "2026-09-03T14:30:01Z", "thinking..."),
            step(2, "GENERIC", "2026-09-03T14:30:05Z"),
            step(3, "USER_INPUT", "2026-09-03T14:31:00Z", "<USER_REQUEST>more</USER_REQUEST>"),
            step(4, "PLANNER_RESPONSE", "2026-09-03T14:31:20Z", "done"),
            "garbage line",
        ]) + "\n", encoding="utf-8")
    return home, db


def test_parse_conversation_matches_generations_to_planner_steps(tmp_path):
    home, db = make_home(tmp_path)
    warnings = []
    events = antigravity.parse_file(db, warnings)
    assert warnings == []
    requests = [e for e in events if e.kind == ledger.REQUEST]
    prompts = [e for e in events if e.kind == ledger.PROMPT]
    assert [e.event_key for e in requests] == [f"{CONV}:gen:0", f"{CONV}:gen:1"]
    assert [e.input_tokens_inferred for e in requests] == [28806, 30358]
    assert {e.context_window_inferred for e in requests} == {256000}
    assert [e.ts for e in requests] == [datetime(2026, 9, 3, 14, 30, 1, tzinfo=UTC), datetime(2026, 9, 3, 14, 31, 20, tzinfo=UTC)]
    assert {(e.model, e.effort, e.tool, e.provider, e.session_id) for e in requests} == {("gemini-3.8-flash", "high", "antigravity", "antigravity", CONV)}
    assert all(e.input_tokens is None and e.total_tokens is None and e.inferred for e in requests)
    assert [(p.event_key, p.model, p.effort) for p in prompts] == [(f"{CONV}:step:0", "gemini-3.8-flash", "high"), (f"{CONV}:step:3", "gemini-3.8-flash", "high")]


def test_count_mismatch_falls_back_to_db_mtime_with_one_warning(tmp_path):
    home, db = make_home(tmp_path, gens=(1, 2, 3))
    warnings = []
    events = antigravity.parse_file(db, warnings)
    requests = [e for e in events if e.kind == ledger.REQUEST]
    assert len(requests) == 3 and len(warnings) == 1 and "3 generations but 2 planner steps" in warnings[0]
    assert len({e.ts for e in requests}) == 1 and requests[0].ts.tzinfo is not None


def test_model_and_effort_fallbacks(tmp_path):
    home, db = make_home(tmp_path, executor=None)
    events = antigravity.parse_file(db)
    assert {(e.model, e.effort) for e in events} == {("gemini-3.8-flash", "high")}  # model from gen row, effort from the transcript setting
    assert antigravity.model_from_setting(SETTING) == ("gemini-3.8-flash", "high")
    assert antigravity.model_from_setting("nothing here") == ("", "")
    assert antigravity.split_variant(["x", "gemini-3.8-flash-high"]) == ("gemini-3.8-flash", "high")
    assert antigravity.split_variant(["gemini-3.8-flash"]) == ("", "")
    assert antigravity.bare_model(["model_enum", "gemini-3.8-flash-high", "gemini-3.8-flash"]) == "gemini-3.8-flash"


def test_missing_transcript_and_locked_db(tmp_path, monkeypatch):
    home, db = make_home(tmp_path, transcript=False)
    warnings = []
    events = antigravity.parse_file(db, warnings)
    assert len([e for e in events if e.kind == ledger.REQUEST]) == 2 and not [e for e in events if e.kind == ledger.PROMPT]
    assert len(warnings) == 1 and "0 planner steps" in warnings[0]

    real_connect = sqlite3.connect

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", locked)
    with pytest.raises(sqlite3.OperationalError):
        antigravity.parse_file(db)
    monkeypatch.setattr(sqlite3, "connect", real_connect)


def test_discover_folds_transcript_into_change_signal(tmp_path):
    home, db = make_home(tmp_path)
    found = antigravity.discover(home, since_days=None)
    assert [p for p, _, _ in found] == [db]
    size = found[0][1]
    transcript = antigravity.transcript_path(home, CONV)
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(step(5, "USER_INPUT", "2026-09-03T15:00:00Z", "again") + "\n")
    assert antigravity.discover(home, since_days=None)[0][1] > size
    assert antigravity.discover(tmp_path / "nope") == []


def test_ledger_rescan_is_idempotent(tmp_path, paths):
    home, db = make_home(tmp_path)
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, antigravity.parse_file(db))
    ledger.upsert(conn, antigravity.parse_file(db))
    assert ledger.count(conn) == 4
    totals = ledger.rollup(ledger.rows(conn), lambda r: r["provider"])["antigravity"]
    assert (totals.prompts, totals.requests, totals.inferred_requests, totals.inferred_input, totals.total) == (2, 2, 2, 28806 + 30358, 0)
    conn.close()
