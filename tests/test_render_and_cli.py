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
