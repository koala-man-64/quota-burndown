from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import pytest

from quota_burndown import cli, render
from quota_burndown.model import compute, WindowInstance
from quota_burndown.store import Sample
from quota_burndown.util import to_local

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=4)


def seed_store(store):
    samples = [
        Sample(NOW - timedelta(days=3), "codex", "7d", 20.0, RESET, 10080, "rollout"),
        Sample(NOW, "codex", "7d", 45.0, RESET, 10080, "rollout"),
        Sample(NOW - timedelta(days=2), "claude", "7d", 15.0, RESET, 10080, "desktop"),
        Sample(NOW, "claude", "7d", 30.0, RESET, 10080, "desktop"),
    ]
    store.append(samples)


def test_target_bar_html_default():
    html = render.target_bar_html(now=NOW)
    assert 'id="target-bar"' in html
    assert 'id="target-date-toggle"' in html
    assert 'checked' not in html
    assert 'id="target-date-input"' in html
    assert 'id="target-summary"' in html
    assert 'id="target-date-clear"' in html
    assert "Standard reset pace active" in html
    assert 'data-days="1"' in html
    assert 'data-days="2"' in html
    assert 'data-days="3"' in html
    assert 'data-days="5"' in html
    assert 'data-preset="friday"' in html
    assert 'data-preset="monday"' in html


def test_target_bar_html_with_future_target_date():
    target = NOW + timedelta(days=3)
    html = render.target_bar_html(target_date=target, now=NOW)
    assert 'id="target-date-toggle" checked' in html
    assert 'Targeting 100% quota by' in html
    expected_val = to_local(target).strftime("%Y-%m-%dT%H:%M")
    assert f'value="{expected_val}"' in html


def test_target_bar_html_with_iso_string():
    target = NOW + timedelta(days=2)
    html = render.target_bar_html(target_date=target.isoformat(), now=NOW)
    assert 'id="target-date-toggle" checked' in html
    assert 'Targeting 100% quota by' in html


def test_target_bar_html_with_past_date():
    past = NOW - timedelta(hours=2)
    html = render.target_bar_html(target_date=past, now=NOW)
    assert 'Target date is in the past' in html


def test_card_html_contains_target_tracking_attributes(store):
    seed_store(store)
    samples = store.load(since=NOW - timedelta(days=7))
    codex_samples = [s for s in samples if s.key == "codex:7d"]
    inst = WindowInstance("codex:7d", "codex", "7d", 10080, RESET, codex_samples)
    bd = compute(inst, NOW)
    html = render.card_html(bd, codex_samples, NOW, "c1")

    assert 'data-card-id="c1"' in html
    assert 'data-provider="codex"' in html
    assert 'data-window="7d"' in html
    assert 'data-used="45.00"' in html
    assert f'data-start="{bd.start.isoformat()}"' in html
    assert f'data-resets="{RESET.isoformat()}"' in html
    assert 'data-rate=' in html
    assert 'data-std-pace=' in html
    assert 'data-std-status=' in html
    assert 'data-std-badge=' in html
    assert 'data-std-left-val=' in html
    assert 'data-std-proj-val=' in html
    assert 'class="badge status-badge"' in html
    assert 'class="stat-used"' not in html
    assert '<div class="stats"><div><b>45%</b>' in html


def test_render_html_includes_target_bar_and_script(store):
    seed_store(store)
    html = render.render_html(store, now=NOW)

    assert '<section class="target-bar" id="target-bar"' in html
    assert '<script id="target-script">' in html
    assert html.count("<script>") == 1
    assert 'class="reset"' not in html
    assert 'id="legend-target-item"' in html
    assert ".pace-target" in html
    assert ".target-tick" in html
    assert ".target-lbl" in html


def test_render_html_with_target_date_parameter(store):
    seed_store(store)
    target = NOW + timedelta(days=2)
    html = render.render_html(store, now=NOW, target_date=target)

    assert 'id="target-date-toggle" checked' in html
    assert 'Targeting 100% quota by' in html


def test_cli_render_with_target_flag(paths, store, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "now_utc", lambda: NOW)
    seed_store(store)
    target_str = (NOW + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    ret = cli.main(["--home", str(paths.home), "render", "--target", target_str])
    assert ret == 0

    html = paths.html.read_text(encoding="utf-8")
    assert 'id="target-date-toggle" checked' in html
    assert 'Targeting 100% quota by' in html
