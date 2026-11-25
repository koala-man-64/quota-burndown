import csv
import json
from datetime import datetime, timedelta, timezone

from quota_burndown import ledger, usage_report
from quota_burndown.ledger import Event

UTC = timezone.utc
NOW = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)


def seed(conn):
    def req(provider, tool, key, delta, model, effort, inp, cr, cw, out, **kw):
        return Event(provider, tool, ledger.REQUEST, key, NOW - delta, session_id="sess-1", model=model, effort=effort, input_tokens=inp, cache_read_tokens=cr, cache_write_tokens=cw, output_tokens=out, reasoning_tokens=1, total_tokens=inp + cr + cw + out, **kw)

    ledger.upsert(conn, [
        Event("claude", "claude-code", ledger.PROMPT, "p1", NOW - timedelta(minutes=5), session_id="sess-1", model="claude-fable-5-1", effort="xhigh"),
        req("claude", "claude-code", "r1", timedelta(minutes=4), "claude-fable-5-1", "xhigh", 10, 1000, 200, 50),
        req("claude", "claude-code", "r2", timedelta(days=2), "claude-opus-5", "high", 20, 500, 0, 30),
        req("claude", "claude-code", "r3", timedelta(days=10), "claude-opus-5", "high", 20, 500, 0, 30),
        req("claude", "claude-code", "r4", timedelta(days=40), "claude-opus-5", "high", 999, 0, 0, 1),
        Event("codex", "codex-desktop", ledger.PROMPT, "c0", NOW - timedelta(hours=1), session_id="thread-1", model="gpt-5.6-luna", effort="low"),
        req("codex", "codex-desktop", "c1", timedelta(hours=1), "gpt-5.6-luna", "low", 1000, 800, 0, 40),
        Event("antigravity", "antigravity", ledger.PROMPT, "a0", NOW - timedelta(minutes=30), session_id="conv-1", model="gemini-3.8-flash", effort="high"),
        Event("antigravity", "antigravity", ledger.REQUEST, "a1", NOW - timedelta(minutes=29), session_id="conv-1", model="gemini-3.8-flash", effort="high", input_tokens_inferred=28806, context_window_inferred=256000),
    ])


def test_summary_periods_and_status_lines(paths):
    conn = ledger.connect(paths.usage_db)
    seed(conn)
    s = usage_report.summary(conn, NOW)
    assert set(s) == {"claude", "codex"}
    claude = s["claude"]
    assert (claude["today"].requests, claude["7d"].requests, claude["30d"].requests) == (1, 2, 3)
    assert claude["today"].prompts == 1 and claude["today"].total == 1260
    lines = usage_report.status_lines(conn, NOW)
    assert lines[0].startswith("Claude usage today: 1 prompts, 1 requests, 1K tokens; 7d: 1 prompts, 2 requests")
    assert all("Antigravity" not in line for line in lines)
    d = usage_report.summary_dict(s)
    assert d["codex"]["today"]["total"] == 1840
    conn.close()


def test_render_text_tables_and_ranges(paths):
    conn = ledger.connect(paths.usage_db)
    seed(conn)
    text = usage_report.render_text(conn, NOW, days=7)
    assert text.startswith("Usage ledger - since 2026-08-27 (UTC): 2 prompts, 3 requests, 5 rows")
    assert "Antigravity" not in text and "antigravity" not in text
    assert "== by provider ==" in text and "== by model x effort ==" in text and "== by day (UTC, most recent first) ==" in text
    assert "claude claude-fable-5-1 @ xhigh" in text
    raw = usage_report.render_text(conn, NOW, days=None, dimensions=["provider"], raw=True)
    assert "all time" in raw and "3,360" in raw and "1,840" in raw and "exact tokens 5,200" in raw
    ranged = usage_report.render_text(conn, NOW, since="2026-09-03", until="2026-09-03", dimensions=["day"])
    assert "2026-09-03 -> 2026-09-03" in ranged and "2026-09-01" not in ranged
    assert usage_report.render_text(conn, NOW, since="2020-01-01", until="2020-01-02").startswith("no usage recorded")
    top = usage_report.render_text(conn, NOW, days=None, dimensions=["model_effort"], top=1)
    assert "(+2 more)" in top
    conn.close()


def test_export_rows_csv_and_payload(paths, tmp_path):
    conn = ledger.connect(paths.usage_db)
    seed(conn)
    rows = usage_report.export_rows(conn, NOW, days=1)
    assert len(rows) == 4 and {r["provider"] for r in rows} == {"claude", "codex"}
    out = tmp_path / "u.csv"
    usage_report.write_csv(out, rows)
    with open(out, encoding="utf-8", newline="") as fh:
        read = list(csv.DictReader(fh))
    assert len(read) == 4 and read[0]["provider"] and "inferred" in read[0]
    p = usage_report.payload(conn, NOW, days=7, recent=3)
    assert p["generated_at"] == "2026-09-03T18:00:00Z" and set(p["summary"]) == {"claude", "codex"}
    assert p["by_model_effort"][0]["key"] == "codex gpt-5.6-luna @ low"
    assert [r["event_key"] for r in p["recent"]] == ["r1", "c1", "r2"]
    assert "Antigravity" not in p["note"]
    assert json.dumps(p)
    conn.close()


def test_fmt_n():
    assert [usage_report.fmt_n(v) for v in (5, 1500, 2_500_000, 3_000_000_000)] == ["5", "2K", "2.5M", "3.00B"]
    assert usage_report.fmt_n(1500, raw=True) == "1,500"
