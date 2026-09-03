import json

from test_usage_antigravity import make_home as make_antigravity_home
from test_usage_claude import assistant, make_home as make_claude_home
from test_usage_codex import desktop_rollout

from quota_burndown import ledger, usage


def homes(tmp_path):
    claude_home, main, sub, wf = make_claude_home(tmp_path)
    desktop_rollout(tmp_path)
    antigravity_home, db = make_antigravity_home(tmp_path)
    return {"claude": claude_home, "codex": tmp_path / "codex", "antigravity": antigravity_home}, main


def test_collect_parses_changed_files_only(tmp_path, paths):
    h, main = homes(tmp_path)
    conn = ledger.connect(paths.usage_db)
    stats, warnings = usage.collect(conn, homes=h)
    assert warnings == []
    assert stats["claude"] == {"files_seen": 3, "files_parsed": 3, "events_written": 4 + 2 + 1, "files_deferred": 0}
    assert stats["codex"]["files_parsed"] == 1 and stats["codex"]["events_written"] == 5
    assert stats["antigravity"]["files_parsed"] == 1 and stats["antigravity"]["events_written"] == 4
    total = ledger.count(conn)

    stats, warnings = usage.collect(conn, homes=h)
    assert all(s["files_parsed"] == 0 for s in stats.values()) and ledger.count(conn) == total

    with open(main, "a", encoding="utf-8") as fh:
        fh.write(assistant("m9", "2026-09-03T15:00:00.000Z", 3) + "\n")
    stats, _ = usage.collect(conn, homes=h)
    assert stats["claude"]["files_parsed"] == 1 and ledger.count(conn) == total + 1
    conn.close()


def test_collect_budget_defers_and_reports(tmp_path, paths):
    h, main = homes(tmp_path)
    conn = ledger.connect(paths.usage_db)
    stats, warnings = usage.collect(conn, providers=["claude"], homes=h, budget_s=1e-9)
    assert stats["claude"]["files_parsed"] == 0 and stats["claude"]["files_deferred"] == 3
    assert warnings and "deferred" in warnings[0]
    stats, warnings = usage.collect(conn, providers=["claude"], homes=h, budget_s=30)
    assert stats["claude"]["files_parsed"] == 3 and warnings == []
    conn.close()


def test_collect_turns_unreadable_sources_into_warnings(tmp_path, paths):
    h, main = homes(tmp_path)
    broken = h["antigravity"] / "conversations" / "broken.db"
    broken.write_text("this is not a sqlite database", encoding="utf-8")
    conn = ledger.connect(paths.usage_db)
    stats, warnings = usage.collect(conn, providers=["antigravity"], homes=h)
    assert stats["antigravity"]["files_parsed"] == 1
    assert len(warnings) == 1 and warnings[0].startswith("antigravity: broken.db")
    assert ledger.file_changed(conn, broken, *[x for p, x, _ in usage.provider_module("antigravity").discover(h["antigravity"], None) if p == broken][:1], 0.0) is True
    conn.close()


def test_progress_callback(tmp_path, paths, monkeypatch):
    h, main = homes(tmp_path)
    conn = ledger.connect(paths.usage_db)
    seen = []
    monkeypatch.setattr(usage.ledger, "upsert", lambda c, e: 1)
    for i in range(100):
        (h["codex"] / "sessions" / f"rollout-{i:03d}.jsonl").write_text(json.dumps({"timestamp": "2026-09-03T00:00:00Z", "ordinal": 0, "type": "session_meta", "payload": {"id": f"s{i}"}}) + "\n", encoding="utf-8")
    usage.collect(conn, providers=["codex"], homes=h, progress=seen.append)
    assert seen and seen[0].startswith("codex: parsed 100 files")
    conn.close()
