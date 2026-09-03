import json
from datetime import datetime, timedelta, timezone

from quota_burndown import cli, install, render, statusline
from quota_burndown.store import Sample

UTC = timezone.utc
NOW = datetime(2026, 9, 2, 23, 0, tzinfo=UTC)
RESET = NOW + timedelta(hours=2)


def seed(store):
    start = RESET - timedelta(minutes=300)
    samples = [
        Sample(start + timedelta(minutes=30), "claude", "5h", 8.0, RESET, 300, "api"),
        Sample(start + timedelta(minutes=120), "claude", "5h", 40.0, RESET, 300, "statusline"),
        Sample(NOW, "claude", "5h", 60.0, RESET, 300, "api"),
        Sample(NOW, "claude", "7d", 27.0, NOW + timedelta(days=1, hours=8), 10080, "api"),
        Sample(NOW - timedelta(hours=3), "codex", "7d", 95.0, NOW + timedelta(days=3), 10080, "rollout"),
        Sample(NOW, "codex", "7d", 99.0, NOW + timedelta(days=3), 10080, "rollout"),
        Sample(NOW, "codex", "5h", 0.0, None, 300, "rollout"),
    ]
    store.append(samples)


def test_render_html_contains_cards_and_charts(store):
    seed(store)
    html = render.render_html(store, now=NOW, warnings=["claude: test warning"])
    assert "<title>Quota Burndown</title>" in html
    assert html.count("<article") == 4
    assert "status-over" in html and "5-hour session" in html and "7-day (all models)" in html
    assert "claude: test warning" in html
    assert 'class="proj"' in html and 'class="ideal"' in html
    assert "no active window" in html
    assert "<script" not in html


def test_render_empty_store(store):
    html = render.render_html(store, now=NOW)
    assert "No samples yet" in html


def test_write_html(store, paths):
    seed(store)
    out = render.write_html(store, paths.html, now=NOW)
    assert out.exists() and out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_statusline_records_and_prints(store):
    payload = {"rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": int((NOW + timedelta(hours=1)).timestamp())}, "seven_day": {"used_percentage": 10, "resets_at": int((NOW + timedelta(days=2)).timestamp())}}}
    line = statusline.run(json.dumps(payload), store, now=NOW, color=False)
    assert line.startswith("Claude 5h 30% p80 ▼50 · 7d 10% p71 ▼61")
    assert store.latest()["claude:5h"].source == "statusline"
    assert "\x1b[" in statusline.run("", store, now=NOW, color=True)
    assert statusline.run("{not json", store, now=NOW, color=False).startswith("Claude")


def test_statusline_empty_store(store):
    assert "no samples" in statusline.run("{}", store, now=NOW)


def test_statusline_skips_ended_windows(store):
    store.append([Sample(NOW - timedelta(hours=1), "codex", "5h", 100.0, NOW - timedelta(minutes=30), 300, "rollout")])
    assert statusline.run("{}", store, now=NOW, color=False) == "quota: no active windows"
    store.append([Sample(NOW, "codex", "7d", 50.0, NOW + timedelta(days=3), 10080, "rollout")])
    line = statusline.run("{}", store, now=NOW, color=False)
    assert line.startswith("Codex 7d 50%") and "5h" not in line


def test_status_lines_text(store):
    seed(store)
    from quota_burndown.model import current

    out = cli.status_lines(current(store.load(), store.latest(), NOW))
    assert out[0].startswith("Claude 5-hour session: 60% used vs 60% pace")
    assert any("Codex 7-day (all models): 99% used" in line for line in out)
    assert any("no active window" in line for line in out)


def test_cli_status_json_and_render(paths, store, capsys):
    seed(store)
    assert cli.main(["--home", str(paths.home), "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {d["window"] for d in data} == {"5h", "7d"}
    assert cli.main(["--home", str(paths.home), "render"]) == 0
    assert paths.html.exists()


def test_install_statusline_dry_run_apply_and_guard(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus", "hooks": {}}), encoding="utf-8")
    msg = install.install_statusline(settings, apply=False)
    assert msg.startswith("would set statusLine")
    assert json.loads(settings.read_text(encoding="utf-8")) == {"model": "opus", "hooks": {}}
    msg = install.install_statusline(settings, apply=True)
    assert "statusLine set" in msg and "backup" in msg
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus" and "quota-burndown.py" in data["statusLine"]["command"]
    assert data["statusLine"]["refreshInterval"] == 60
    assert install.install_statusline(settings, apply=True).startswith("statusLine already installed")
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "other"}}), encoding="utf-8")
    assert install.install_statusline(settings, apply=True).startswith("skip: a different statusLine")
    assert "statusLine set" in install.install_statusline(settings, apply=True, force=True)
    assert "removed" in install.uninstall_statusline(settings, apply=True)
    assert "statusLine" not in json.loads(settings.read_text(encoding="utf-8"))


def test_install_codex_skill_substitutes_launcher(tmp_path):
    msg = install.install_codex_skill(tmp_path, apply=True)
    target = tmp_path / "skills" / "quota-burndown" / "SKILL.md"
    assert target.exists() and "written" in msg
    text = target.read_text(encoding="utf-8")
    assert "__QB_LAUNCHER__" not in text and "quota-burndown.py" in text
    assert install.install_codex_skill(tmp_path, apply=True).startswith("codex skill already installed")


def test_task_command_prefers_windowless_python():
    cmd = install.task_command()
    assert "quota-burndown.py" in cmd and cmd.endswith("collect --render --quiet")


def test_render_usage_section(store, paths):
    from quota_burndown import ledger
    from quota_burndown.ledger import Event

    seed(store)
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, [
        Event("claude", "claude-code", ledger.PROMPT, "p1", NOW - timedelta(minutes=3), model="claude-fable-5-1", effort="xhigh"),
        Event(
            "claude", "claude-code", ledger.REQUEST, "r1", NOW - timedelta(minutes=2), model="claude-fable-5-1", effort="xhigh",
            input_tokens=10, cache_read_tokens=1000, cache_write_tokens=200, output_tokens=50, reasoning_tokens=1, total_tokens=1260,
        ),
        Event(
            "antigravity", "antigravity", ledger.REQUEST, "a1", NOW - timedelta(minutes=1), model="gemini-3.8-flash", effort="high",
            input_tokens_inferred=28806, context_window_inferred=256000,
        ),
    ])
    conn.close()
    html = render.render_html(store, now=NOW, usage_db=paths.usage_db)
    assert "Token usage" in html and html.count('class="card usage-card') == 3
    assert "claude-fable-5-1 @ xhigh" in html and "~29K" in html and "no usage in 30 days" in html
    assert "Recent requests" in html and "unlabeled context counter" in html and "tokens inferred" in html
    assert "<script" not in html
    assert "Token usage" not in render.render_html(store, now=NOW)


def test_cli_usage_round_trip(paths, monkeypatch, tmp_path, capsys):
    from test_usage_antigravity import make_home

    home, db = make_home(tmp_path)
    monkeypatch.setenv("QUOTA_BURNDOWN_ANTIGRAVITY_HOME", str(home))
    assert cli.main(["--home", str(paths.home), "collect", "--provider", "antigravity", "--quiet"]) == 0
    args = ["--home", str(paths.home), "usage", "--days", "3650", "--by", "provider,model_effort", "--json", str(tmp_path / "u.json"), "--csv", str(tmp_path / "u.csv")]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "antigravity gemini-3.8-flash @ high" in out and "wrote 4 rows" in out
    data = json.loads((tmp_path / "u.json").read_text(encoding="utf-8"))
    assert len(data["rows"]) == 4 and (tmp_path / "u.csv").exists()
    assert cli.main(["--home", str(paths.home), "status"]) == 0 and "page:" in capsys.readouterr().out
    assert cli.main(["--home", str(paths.home), "where"]) == 0 and "usage db:" in capsys.readouterr().out
    assert cli.main(["--home", str(paths.home), "backfill", "--usage", "--provider", "antigravity", "--rescan"]) == 0
    out = capsys.readouterr().out
    assert "forgot 1 scan records" in out and '"files_parsed": 1' in out and paths.html.exists()
    assert cli.main(["--home", str(paths.home), "collect", "--provider", "antigravity", "--no-usage", "--quiet"]) == 0
    import pytest

    with pytest.raises(SystemExit):
        cli.main(["--home", str(paths.home), "usage", "--by", "bogus"])
