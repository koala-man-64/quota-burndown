from datetime import datetime, timedelta, timezone

from quota_burndown import ledger
from quota_burndown.ledger import Event

UTC = timezone.utc
T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def req(key, minutes=0, provider="claude", tool="claude-code", output=10, **kw):
    base = dict(
        input_tokens=5, cache_read_tokens=100, cache_write_tokens=20, output_tokens=output, reasoning_tokens=3,
        total_tokens=125 + output, model="claude-fable-5-1", effort="xhigh", session_id="s1",
    )
    base.update(kw)
    return Event(provider, tool, ledger.REQUEST, key, T0 + timedelta(minutes=minutes), **base)


def prompt(key, minutes=0, provider="claude", tool="claude-code", **kw):
    return Event(provider, tool, ledger.PROMPT, key, T0 + timedelta(minutes=minutes), session_id="s1", **kw)


def test_connect_is_idempotent_and_creates_schema(paths):
    conn = ledger.connect(paths.usage_db)
    conn.close()
    conn = ledger.connect(paths.usage_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"events", "scan_state"} <= tables
    assert ledger.count(conn) == 0
    conn.close()


def test_upsert_is_idempotent_and_keeps_fuller_record(paths):
    conn = ledger.connect(paths.usage_db)
    assert ledger.upsert(conn, [req("m1", output=10), prompt("u1")]) == 2
    ledger.upsert(conn, [req("m1", output=10), prompt("u1")])
    assert ledger.count(conn) == 2
    ledger.upsert(conn, [req("m1", output=50, model="claude-opus-5")])
    row = conn.execute("SELECT output_tokens, model FROM events WHERE event_key = 'm1'").fetchone()
    assert (row["output_tokens"], row["model"]) == (50, "claude-opus-5")
    ledger.upsert(conn, [req("m1", output=7, model="thin")])
    row = conn.execute("SELECT output_tokens, model FROM events WHERE event_key = 'm1'").fetchone()
    assert (row["output_tokens"], row["model"]) == (50, "claude-opus-5")
    ledger.upsert(conn, [prompt("u1", model="claude-opus-5", effort="high")])
    row = conn.execute("SELECT model, effort FROM events WHERE event_key = 'u1'").fetchone()
    assert (row["model"], row["effort"]) == ("claude-opus-5", "high")
    assert ledger.upsert(conn, []) == 0
    conn.close()


def test_same_key_in_different_providers_or_kinds_does_not_collide(paths):
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, [req("k"), req("k", provider="codex", tool="codex-desktop"), prompt("k")])
    assert ledger.count(conn) == 3
    conn.close()


def test_scan_state_round_trip(paths, tmp_path):
    conn = ledger.connect(paths.usage_db)
    path = tmp_path / "a.jsonl"
    assert ledger.file_changed(conn, path, 10, 1.5)
    ledger.mark_scanned(conn, path, 10, 1.5, now=T0)
    assert not ledger.file_changed(conn, path, 10, 1.5)
    assert ledger.file_changed(conn, path, 11, 1.5)
    assert ledger.file_changed(conn, path, 10, 2.0)
    ledger.mark_scanned(conn, tmp_path / "b.jsonl", 1, 1.0, now=T0)
    assert ledger.forget_scans(conn, "a.jsonl") == 1
    assert ledger.file_changed(conn, path, 10, 1.5)
    assert ledger.forget_scans(conn) == 1
    conn.close()


def test_rows_filters_and_rollup_keeps_inferred_apart(paths):
    conn = ledger.connect(paths.usage_db)
    events = [
        prompt("u1", 0),
        req("m1", 1, output=10),
        req("m2", 61, output=20, effort="high"),
        prompt("u2", 60, provider="codex", tool="codex-cli"),
        req(
            "c1", 62, provider="codex", tool="codex-cli", model="gpt-5.6-luna", effort="low",
            input_tokens=1000, cache_read_tokens=800, cache_write_tokens=0, output_tokens=40, reasoning_tokens=5, total_tokens=1040,
        ),
        Event(
            "antigravity", "antigravity", ledger.REQUEST, "g1", T0 + timedelta(minutes=70),
            model="gemini-3.8-flash", effort="high", input_tokens_inferred=28806, context_window_inferred=256000,
        ),
        Event("antigravity", "antigravity", ledger.PROMPT, "s0", T0 + timedelta(minutes=69)),
    ]
    ledger.upsert(conn, events)

    assert len(ledger.rows(conn)) == 7
    assert len(ledger.rows(conn, since=T0 + timedelta(minutes=60))) == 5
    assert len(ledger.rows(conn, until=T0 + timedelta(minutes=60))) == 2
    assert [r["event_key"] for r in ledger.rows(conn, provider="codex", kind=ledger.REQUEST)] == ["c1"]
    assert [r["event_key"] for r in ledger.recent_requests(conn, 2)] == ["g1", "c1"]

    by_provider = ledger.rollup(ledger.rows(conn), lambda r: r["provider"])
    claude = by_provider["claude"]
    assert (claude.prompts, claude.requests, claude.input, claude.cache_read, claude.cache_write) == (1, 2, 10, 200, 40)
    assert (claude.output, claude.reasoning, claude.total) == (30, 6, 280)
    codex = by_provider["codex"]
    assert (codex.prompts, codex.requests, codex.total) == (1, 1, 1040)
    anti = by_provider["antigravity"]
    assert (anti.prompts, anti.requests, anti.inferred_requests, anti.inferred_input, anti.total) == (1, 1, 1, 28806, 0)
    assert claude.has_exact and not anti.has_exact
    by_model = ledger.rollup(ledger.rows(conn, kind=ledger.REQUEST), lambda r: f"{r['model']}@{r['effort']}")
    assert by_model["claude-fable-5-1@high"].requests == 1
    assert by_model["gemini-3.8-flash@high"].to_dict()["inferred_input"] == 28806

    first = ledger.rows(conn)[0]
    assert ledger.day_utc(first) == "2026-09-03"
    assert len(ledger.day_local(first)) == 10
    conn.close()


def test_event_helpers():
    e = prompt("u1")
    assert not e.inferred
    assert e.with_model("m", "e").model == "m" and e.with_model("", "").model == ""
    assert e.to_row()[ledger.COLUMNS.index("ts")] == "2026-09-03T12:00:00Z"
