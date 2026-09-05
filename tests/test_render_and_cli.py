import json
import re
from datetime import datetime, timedelta, timezone

from quota_burndown import cli, install, render, statusline
from quota_burndown.store import Sample, Store

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
    assert html.count('<figure class="chart-figure"') == 4 and html.count('<svg class="chart"') == 4
    assert html.count('data-domain-start=') == 4 and html.count('data-domain-end=') == 4
    assert html.count('class="pace"') == 3 and html.count('class="proj"') >= 1  # the idle codex:5h card draws its 0% reading but no pace
    assert 'class="ideal"' not in html and 'class="reset"' not in html
    assert html.count('class="used"') >= 4 and html.count("<details") == 4
    assert "no active window" in html
    assert 'class="range"' not in html and 'data-span="24h"' not in html and "quota-burndown.range" not in html
    payloads = re.findall(r'<script type="application/json" class="chart-data" data-for="c\d+">(.*?)</script>', html)
    assert len(payloads) == 4
    first = json.loads(payloads[0])
    assert first["points"] and {"x", "y", "t", "v"} <= set(first["points"][0]) and first["points"][-1]["v"] == "60%"
    assert first["reset"].startswith("resets ")
    assert html.count("<script>") == 1 and "src=" not in html and 'id="chart-tooltip"' in html
    assert "window reset" in html and "linear pace" in html


def test_render_collapses_legacy_gpt_versions_into_three_allowance_cards(store):
    weekly_reset = NOW + timedelta(days=3)
    store.append([
        Sample(NOW - timedelta(minutes=5), "codex", "7d:gpt-5.6", 27.0, weekly_reset, 10080, "rollout"),
        Sample(NOW, "codex", "7d:gpt-6", 28.0, weekly_reset, 10080, "rollout"),
        Sample(NOW, "codex", "5h:gpt-5.6", 9.0, NOW + timedelta(hours=2), 300, "rollout"),
        Sample(NOW, "codex", "5h:spark", 12.0, NOW + timedelta(hours=2), 300, "rollout"),
        Sample(NOW, "codex", "7d:spark", 50.0, NOW + timedelta(days=4), 10080, "rollout"),
        Sample(NOW, "codex", "10080m:Codex", 28.0, weekly_reset, 10080, "app-server"),
        Sample(NOW, "codex", "300m:Codex_bengalfox", 12.0, NOW + timedelta(hours=2), 300, "app-server"),
        Sample(NOW, "codex", "10080m:Codex_bengalfox", 50.0, NOW + timedelta(days=4), 10080, "app-server"),
    ])

    html = render.render_html(store, now=NOW)

    assert html.count("<article") == 3
    assert html.count("<h3>7-day (Codex)</h3>") == 1
    assert html.count("<h3>5-hour session (Spark)</h3>") == 1
    assert html.count("<h3>7-day (Spark)</h3>") == 1
    assert "7-day (GPT-5.6)" not in html and "7-day (GPT-6)" not in html


def test_claude_alias_cards_keep_newest_state_and_all_history_without_rewriting_store(store):
    store.append([
        Sample(NOW - timedelta(hours=9), "claude", "5h", 13, NOW - timedelta(hours=7), 300, "desktop"),
        Sample(NOW - timedelta(hours=8), "claude", "300m:claude", 27, NOW - timedelta(hours=7), 300, "desktop-history"),
        Sample(NOW - timedelta(minutes=30), "claude", "5h", 100, RESET, 300, "desktop"),
        Sample(NOW, "claude", "300m:claude", 0, None, 300, "desktop-history"),
        Sample(NOW - timedelta(minutes=30), "claude", "7d", 24, RESET, 10080, "desktop"),
        Sample(NOW, "claude", "10080m:Claude", 25, RESET, 10080, "desktop-history"),
    ])
    raw_before = store.paths.samples.read_bytes(), store.paths.latest.read_bytes()
    html = render.render_html(store, now=NOW)
    assert html.count("<article") == 2
    assert html.count("<h3>5-hour session</h3>") == 1
    assert html.count("<h3>7-day (all models)</h3>") == 1
    assert "300m (Claude)" not in html and "10080m (Claude)" not in html
    cards = re.findall(r"<article.*?</article>", html, re.S)
    assert '<div class="stats"><div><b>0%</b><span>used</span>' in cards[0]
    assert '<div class="stats"><div><b>25%</b>' in cards[1]
    chart = re.search(r'<script type="application/json" class="chart-data" data-for="c1">(.*?)</script>', html)
    points = json.loads(chart.group(1))["points"]
    assert {p["v"] for p in points} >= {"13%", "27%", "100%", "0%"}
    assert raw_before == (store.paths.samples.read_bytes(), store.paths.latest.read_bytes())


def test_cli_quota_backfill_respects_since_days_and_rescan(paths, monkeypatch, tmp_path, capsys):
    import os
    import time

    from test_codex_provider import event, turn, write_rollout

    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    recent = home / "sessions" / "2026" / "09" / "02" / "rollout-2026-09-02T10-00-00-a.jsonl"
    old = home / "archived_sessions" / "rollout-old.jsonl"
    write_rollout(recent, [turn("2026-09-02T09:59:00Z", "gpt-5.6-sol"), event("2026-09-02T10:00:00Z", 10)])
    write_rollout(old, [turn("2026-06-08T09:59:00Z", "gpt-5.3-codex-spark"), event("2026-06-08T10:00:00Z", 50)])
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))

    assert cli.main(["--home", str(paths.home), "backfill", "--since-days", "30"]) == 0
    assert '"files_scanned": 1' in capsys.readouterr().out
    assert set(Store(paths).latest()) == {"codex:7d:codex"}
    assert cli.main(["--home", str(paths.home), "backfill", "--since-days", "30"]) == 0
    assert '"files_scanned": 0' in capsys.readouterr().out  # state says the file is already read
    assert cli.main(["--home", str(paths.home), "backfill", "--since-days", "30", "--rescan"]) == 0
    text = capsys.readouterr().out
    assert "forgot the Codex quota scan state" in text and '"files_scanned": 1' in text
    assert cli.main(["--home", str(paths.home), "backfill"]) == 0  # no range: everything, archive included
    assert set(Store(paths).latest()) == {"codex:7d:codex", "codex:7d:spark"}


def test_expired_window_is_labelled_as_awaiting_a_sample(store):
    from quota_burndown.model import current

    ended = NOW - timedelta(hours=8)
    store.append([
        Sample(ended - timedelta(days=6), "claude", "7d:fable", 20.0, ended, 10080, "api"),
        Sample(ended - timedelta(hours=30), "claude", "7d:fable", 74.0, ended, 10080, "api"),
    ])
    html = render.render_html(store, now=NOW)
    assert "7-day (Fable)" in html and "no reading for the new window yet" in html and "final use of that window" in html
    assert "window ended · awaiting a fresh sample" in html and "74%" in html
    assert html.count('class="pace"') == 0  # nothing to pace against until a reading for the new window arrives
    assert html.count('<figure class="chart-figure"') == 1
    assert 'data-span="14d"' in html
    lines = cli.status_lines(current(store.load(), store.latest(), NOW))
    assert lines == [f"Claude 7-day (Fable): previous window ended {render.fmt_local(ended)} at 74%; no reading for the new window yet"]


def test_window_titles():
    assert render.window_title("5h") == "5-hour session"
    assert render.window_title("7d:fable") == "7-day (Fable)"
    assert render.window_title("7d:gpt-5.6") == "7-day (GPT-5.6)"
    assert render.window_title("7d:codex") == "7-day (Codex)"
    assert render.window_title("5h:spark") == "5-hour session (Spark)"
    assert render.window_title("1d") == "1d"


def test_render_empty_store(store):
    html = render.render_html(store, now=NOW)
    assert "No samples yet" in html


def test_write_html(store, paths):
    seed(store)
    out = render.write_html(store, paths.html, now=NOW)
    assert out.exists() and out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_statusline_displays_latest_and_never_writes(store):
    seed(store)
    before = (store.latest(), len(store.load()))
    payload = {"rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": int((NOW + timedelta(hours=1)).timestamp())}, "seven_day": {"used_percentage": 10, "resets_at": int((NOW + timedelta(days=2)).timestamp())}}}
    line = statusline.run(json.dumps(payload), store, now=NOW, color=False)
    assert line.startswith("Claude 5h 60% p60 =0 · 7d 27% p81 ▼54") and "Codex" in line
    assert (store.latest(), len(store.load())) == before  # the payload on stdin is display input only
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


def test_task_settings_allow_battery_and_catch_up():
    cmd = install.task_settings_command()
    script = cmd[-1]
    assert cmd[0] == "powershell" and "-NonInteractive" in cmd
    for switch in ("-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries", "-StartWhenAvailable", "-MultipleInstances IgnoreNew", "New-TimeSpan -Minutes 10"):
        assert switch in script
    assert f"-TaskName '{install.TASK_NAME}'" in script
    dry = install.install_task(apply=False)
    assert dry.startswith("would run") and "Set-ScheduledTask" in dry


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
    assert "src=" not in html
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
