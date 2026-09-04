"""Self-contained HTML page: quota cards with one inline-SVG usage chart each, plus the token
usage section. Everything is in the one file. A small inline script adds a crosshair and
tooltip to the charts; nothing is fetched from anywhere."""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, charts, ledger, usage_report
from .charts import ChartData
from .ledger import Totals
from .model import Burndown, current
from .store import Sample, Store
from .util import atomic_write_text, fmt_local, fmt_minutes, now_utc, to_local

STALE_MIN = 20
PROVIDER_TITLES = {"claude": "Claude", "codex": "Codex"}
WINDOW_TITLES = {"5h": "5-hour session", "7d": "7-day (all models)"}
FAMILY_TITLES = {"gpt": "GPT", "spark": "Spark", "fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}
STATUS_TEXT = {
    "over": "over pace",
    "under": "under pace",
    "on-pace": "on pace",
    "early": "too early to call",
    "exhausted": "budget exhausted",
    "expired": "window ended · awaiting a fresh sample",
    "idle": "no active window",
}
CHART_W, CHART_H = 800, 320
PAD_L, PAD_R, PAD_T, PAD_B = 48, 16, 30, 30


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def family_title(scope: str) -> str:
    head, _, rest = scope.partition("-")
    head = FAMILY_TITLES.get(head.lower(), head.capitalize())
    return f"{head}-{rest}" if rest else head


def window_title(window: str) -> str:
    if window in WINDOW_TITLES:
        return WINDOW_TITLES[window]
    base, _, scope = window.partition(":")
    if scope:
        stem = {"5h": "5-hour session", "7d": "7-day"}.get(base, base)
        return f"{stem} ({family_title(scope)})"
    return window


# -- usage chart -----------------------------------------------------------------------

def _x_of(data: ChartData, ts: datetime) -> float:
    total = (data.span_end - data.span_start).total_seconds() or 1.0
    frac = (ts - data.span_start).total_seconds() / total
    return PAD_L + (CHART_W - PAD_L - PAD_R) * min(max(frac, 0.0), 1.0)


def _y_of(used: float) -> float:
    return PAD_T + (CHART_H - PAD_T - PAD_B) * (1 - min(max(used, 0.0), 100.0) / 100)


def _clip(data: ChartData, line: charts.Line) -> tuple[float, float, float, float] | None:
    """Pixel endpoints of a line clipped to the plotted time span."""
    (t0, v0), (t1, v1) = line.start, line.end
    if t1 <= t0:
        return None
    lo, hi = max(t0, data.span_start), min(t1, data.span_end)
    if hi <= lo:
        return None
    slope = (v1 - v0) / (t1 - t0).total_seconds()
    y_lo = v0 + slope * (lo - t0).total_seconds()
    y_hi = v0 + slope * (hi - t0).total_seconds()
    return _x_of(data, lo), _y_of(y_lo), _x_of(data, hi), _y_of(y_hi)


def chart_points(data: ChartData) -> list[dict]:
    """The plotted points with pixel positions and display strings, for the hover layer and
    the table twin."""
    out = []
    for segment in data.segments:
        for point in segment.points:
            out.append({"x": round(_x_of(data, point.ts), 1), "y": round(_y_of(point.used), 1), "t": charts.local_label(point.ts), "v": f"{point.used:.0f}%"})
    return out


def chart_svg(data: ChartData, chart_id: str, title: str) -> str:
    plot_bottom = _y_of(0)
    parts: list[str] = []
    for value in data.y_ticks:
        y = _y_of(value)
        parts.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" x2="{CHART_W - PAD_R}" y2="{y:.1f}"/>')
        parts.append(f'<text class="lbl" x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end">{value}%</text>')
    if data.active and data.now < data.span_end:
        x_now, x_end = _x_of(data, data.now), _x_of(data, data.span_end)
        parts.append(f'<rect class="future" x="{x_now:.1f}" y="{PAD_T}" width="{x_end - x_now:.1f}" height="{plot_bottom - PAD_T:.1f}"/>')
    for tick in data.x_ticks:
        x = _x_of(data, tick.ts)
        parts.append(f'<line class="axis" x1="{x:.1f}" y1="{plot_bottom:.1f}" x2="{x:.1f}" y2="{plot_bottom + 5:.1f}"/>')
        parts.append(f'<text class="lbl" x="{x:.1f}" y="{CHART_H - 8}" text-anchor="middle">{esc(tick.label)}</text>')
    parts.append(f'<line class="axis" x1="{PAD_L}" y1="{plot_bottom:.1f}" x2="{CHART_W - PAD_R}" y2="{plot_bottom:.1f}"/>')

    for segment in data.segments:
        coords = [(_x_of(data, p.ts), _y_of(p.used)) for p in segment.points]
        if len(coords) == 1:
            coords.append(coords[0])
        line = " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
        parts.append(f'<path class="area" d="M {coords[0][0]:.1f} {plot_bottom:.1f} L {line} L {coords[-1][0]:.1f} {plot_bottom:.1f} Z"/>')
        parts.append(f'<path class="used" d="M {line}"/>')

    if data.pace is not None:
        clipped = _clip(data, data.pace)
        if clipped:
            parts.append('<line class="pace" x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}"/>'.format(*clipped))
    if data.projection is not None:
        clipped = _clip(data, data.projection)
        if clipped:
            parts.append('<line class="proj" x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}"/>'.format(*clipped))
    if data.active:
        x_now = _x_of(data, data.now)
        parts.append(f'<line class="now" x1="{x_now:.1f}" y1="{PAD_T}" x2="{x_now:.1f}" y2="{plot_bottom:.1f}"/>')

    for mark in data.resets:
        x = _x_of(data, mark.ts)
        parts.append(f'<line class="reset-tick" x1="{x:.1f}" y1="{PAD_T - 10}" x2="{x:.1f}" y2="{PAD_T}"/>')
        if mark.current:
            anchor = "end" if x > CHART_W * 0.7 else "start"
            dx = -6 if anchor == "end" else 6
            parts.append(f'<text class="reset-lbl" x="{x + dx:.1f}" y="{PAD_T - 14}" text-anchor="{anchor}">resets {esc(fmt_local(mark.ts))}</text>')

    end = data.end_point
    if end is not None:
        x, y = _x_of(data, end.ts), _y_of(end.used)
        parts.append(f'<circle class="end-ring" cx="{x:.1f}" cy="{y:.1f}" r="6"/>')
        parts.append(f'<circle class="end-dot" cx="{x:.1f}" cy="{y:.1f}" r="4"/>')
        anchor = "end" if x > CHART_W * 0.85 else "start"
        dx = -10 if anchor == "end" else 10
        parts.append(f'<text class="end-lbl" x="{x + dx:.1f}" y="{y - 8:.1f}" text-anchor="{anchor}">{end.used:.0f}%</text>')

    parts.append(f'<line class="crosshair" x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{plot_bottom:.1f}"/>')
    parts.append(f'<circle class="hover-dot" cx="{PAD_L}" cy="{plot_bottom:.1f}" r="5"/>')
    label = f"Percent of the {esc(title)} window used, {esc(data.span_label)}"
    return (
        f'<svg class="chart" viewBox="0 0 {CHART_W} {CHART_H}" role="img" tabindex="0" aria-label="{label}" '
        f'data-chart="{esc(chart_id)}" data-w="{CHART_W}" data-h="{CHART_H}">' + "".join(parts) + "</svg>"
    )


def chart_table_html(points: list[dict]) -> str:
    rows = "".join(f"<tr><td>{esc(p['t'])}</td><td>{esc(p['v'])}</td></tr>" for p in points)
    return (
        f'<details class="chart-table"><summary>{len(points)} plotted readings</summary>'
        f'<table class="usage"><thead><tr><th>time</th><th>% used</th></tr></thead><tbody>{rows}</tbody></table></details>'
    )


def chart_figure_html(data: ChartData, chart_id: str, title: str, auto_key: str, visible: bool) -> str:
    """One span's chart for a card. Every span is rendered; the range control shows one.
    `auto_key` is the span the card shows when the control is on Auto."""
    points = chart_points(data)
    reset = next((f"resets {fmt_local(m.ts)}" for m in data.resets if m.current), None)
    payload = json.dumps({"points": points, "reset": reset}, separators=(",", ":")).replace("</", "<\\/")
    hidden = "" if visible else " hidden"
    return (
        f'<figure class="chart-figure" data-span="{esc(data.span_key)}" data-auto="{esc(auto_key)}"{hidden}>'
        f'<h4>% used, {esc(data.span_label)}</h4>{chart_svg(data, chart_id, title)}'
        f'<script type="application/json" class="chart-data" data-for="{esc(chart_id)}">{payload}</script>'
        f"{chart_table_html(points)}</figure>"
    )


def card_figures_html(bd: Burndown, history: list[Sample], now: datetime, chart_id: str, title: str) -> str:
    """Every span's chart for the card; the card's default span (or the shortest span with
    data) is visible, the rest hidden until the range control asks for them."""
    available = {}
    for key in charts.SPANS:
        data = charts.build(bd, history, now, key)
        if data is not None:
            available[key] = data
    if not available:
        return '<div class="note">No readings in the last 30 days.</div>'
    default_key = charts.default_span_key(bd.window_min)
    shown = default_key if default_key in available else next(iter(available))
    return "".join(chart_figure_html(data, f"{chart_id}-{key}", title, shown, key == shown) for key, data in available.items())


# -- quota cards -----------------------------------------------------------------------

def badge_text(bd: Burndown) -> str:
    if bd.status in ("over", "under"):
        return f"{'▲' if bd.delta > 0 else '▼'} {abs(bd.delta):.0f} pts {STATUS_TEXT[bd.status]}"
    return STATUS_TEXT.get(bd.status, bd.status)


def projection_text(bd: Burndown) -> tuple[str, str]:
    if bd.status == "idle":
        return "—", "no active window"
    if bd.status == "exhausted":
        return fmt_minutes(bd.remaining_min), "until the budget resets"
    if bd.status == "expired":
        return f"{bd.used:.0f}%", "final use for this window"
    if bd.status == "early" or bd.rate_per_hour <= 0:
        return "—", "projection needs more of the window"
    if bd.exhausts_before_reset and bd.exhaust_at is not None:
        return fmt_local(bd.exhaust_at), f"hits 100% at this rate, {fmt_minutes((bd.resets_at - bd.exhaust_at).total_seconds() / 60)} before reset"
    return f"{min(bd.projected_end, 999):.0f}%", "projected use at reset, at this rate"


def card_html(bd: Burndown, history: list[Sample], now: datetime, chart_id: str) -> str:
    stale = bd.age_min is not None and bd.age_min > STALE_MIN
    classes = f"card status-{bd.status}" + (" stale" if stale else "")
    proj_value, proj_label = projection_text(bd)
    title = window_title(bd.window)
    if bd.status == "idle":
        stats = (
            f'<div><b>{bd.used:.0f}%</b><span>used</span></div>'
            f'<div><b>—</b><span>pace</span></div>'
            f'<div><b>—</b><span>time left</span></div>'
            f'<div><b>{esc(proj_value)}</b><span>{esc(proj_label)}</span></div>'
        )
    elif bd.status == "expired":
        # The window has reset but no reading for the new one has arrived: say so rather than
        # showing the old window's final figure as if it were current.
        ended = fmt_local(bd.resets_at) if bd.resets_at else "?"
        stats = (
            f'<div><b>—</b><span>no reading for the new window yet</span></div>'
            f'<div><b>—</b><span>pace</span></div>'
            f'<div><b>{esc(ended)}</b><span>previous window ended</span></div>'
            f'<div><b>{bd.used:.0f}%</b><span>final use of that window</span></div>'
        )
    else:
        resets = fmt_local(bd.resets_at) if bd.resets_at else "?"
        stats = (
            f'<div><b>{bd.used:.0f}%</b><span>used · {bd.remaining_pct:.0f}% left</span></div>'
            f'<div><b>{bd.pace:.0f}%</b><span>linear pace</span></div>'
            f'<div><b>{esc(fmt_minutes(bd.remaining_min))}</b><span>left · resets {esc(resets)}</span></div>'
            f'<div><b>{esc(proj_value)}</b><span>{esc(proj_label)}</span></div>'
        )
    figure = card_figures_html(bd, history, now, chart_id, title)
    age = f"{bd.age_min:.0f} min ago" if bd.age_min is not None else "never"
    foot = f"last sample {esc(age)} via {esc(bd.source or '?')} · {len(bd.samples)} samples this window"
    if stale:
        foot += " · <b>stale</b>: collector has not run recently"
    return (
        f'<article class="{classes}">'
        f'<header><h3>{esc(title)}</h3><span class="badge">{esc(badge_text(bd))}</span></header>'
        f'<div class="stats">{stats}</div>'
        f"{figure}"
        f'<div class="foot">{foot}</div>'
        "</article>"
    )


# -- usage section -------------------------------------------------------------------

USAGE_ROWS = (("prompts", "prompts"), ("requests", "requests"), ("input", "input"), ("cache_read", "cache read"), ("cache_write", "cache write"), ("output", "output"), ("total", "total"))
USAGE_PERIOD_TITLES = {"today": "today", "7d": "7 days", "30d": "30 days"}
ANTIGRAVITY_NOTE = (
    "Antigravity writes no token counts. ~input is an unlabeled context counter read from its conversation "
    "database and is kept out of every total; output tokens are unknown; Google exposes no local limit signal."
)
USAGE_FOOTNOTE = "Today is the local calendar day; 7 and 30 days are rolling windows. Tokens are what each tool recorded locally, not a bill."


def _n(value: int) -> str:
    return usage_report.fmt_n(value)


def usage_card_html(provider: str, periods: dict[str, Totals], top: list[tuple[str, Totals]]) -> str:
    title = ledger.PROVIDER_TITLES.get(provider, provider)
    if not any(t.prompts or t.requests for t in periods.values()):
        return (
            f'<article class="card usage-card muted"><header><h3>{esc(title)}</h3><span class="badge">no usage in 30 days</span></header>'
            '<div class="note">Nothing recorded yet. Run <code>quota-burndown collect</code>, or <code>backfill --usage</code> for history.</div></article>'
        )
    exact = any(t.has_exact for t in periods.values())
    inferred = any(t.inferred_requests for t in periods.values())
    head = "".join(f"<th>{esc(USAGE_PERIOD_TITLES[p])}</th>" for p in usage_report.PERIODS)
    body: list[str] = []
    for attr, label in USAGE_ROWS:
        if not exact and attr not in ("prompts", "requests"):
            continue
        cells = "".join(f"<td>{_n(getattr(periods[p], attr))}</td>" for p in usage_report.PERIODS)
        body.append(f"<tr><td>{esc(label)}</td>{cells}</tr>")
    if inferred:
        cells = "".join(f"<td>~{_n(periods[p].inferred_input)}</td>" for p in usage_report.PERIODS)
        body.append(f'<tr class="inferred"><td>~input (inferred)</td>{cells}</tr>')
    table = f'<table class="usage"><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'
    top_rows: list[str] = []
    for key, t in top[:5]:
        if t.has_exact:
            top_rows.append(f"<tr><td>{esc(key)}</td><td>{t.requests}</td><td>{_n(t.total)}</td></tr>")
        else:
            top_rows.append(f'<tr class="inferred"><td>{esc(key)}</td><td>{t.requests}</td><td>~{_n(t.inferred_input)}</td></tr>')
    top_html = (
        '<h4>Top model × effort, 7 days</h4><table class="usage"><thead><tr><th>model @ effort</th><th>reqs</th><th>tokens</th></tr></thead>'
        f'<tbody>{"".join(top_rows)}</tbody></table>'
    ) if top_rows else ""
    note = f'<div class="note">{esc(ANTIGRAVITY_NOTE)}</div>' if provider == "antigravity" else ""
    badge = "tokens inferred" if not exact else "exact counts"
    return f'<article class="card usage-card"><header><h3>{esc(title)}</h3><span class="badge">{esc(badge)}</span></header>{table}{top_html}{note}</article>'


def recent_table_html(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return ""
    body: list[str] = []
    for row in rows:
        inferred = row["input_tokens_inferred"] is not None
        cells = [fmt_local(ledger.row_ts(row), "%a %d %H:%M"), ledger.PROVIDER_TITLES.get(row["provider"], row["provider"]), f"{row['model'] or '?'} @ {row['effort'] or '?'}"]
        if inferred:
            cells += [f"~{_n(int(row['input_tokens_inferred']))}", "", "", "?", "?"]
        else:
            cells += [_n(int(row[c] or 0)) for c in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "total_tokens")]
        css = ' class="inferred"' if inferred else ""
        body.append(f"<tr{css}>" + "".join(f"<td>{esc(c)}</td>" for c in cells) + "</tr>")
    return (
        '<div class="recent"><h3>Recent requests</h3><table class="usage"><thead><tr><th>time</th><th>provider</th><th>model @ effort</th>'
        f'<th>input</th><th>cache r</th><th>cache w</th><th>output</th><th>total</th></tr></thead><tbody>{"".join(body)}</tbody></table></div>'
    )


def usage_section_html(usage_db: Path, now: datetime) -> str:
    try:
        conn = ledger.connect(usage_db)
    except sqlite3.Error as exc:
        return f'<section class="usage"><h2>Token usage</h2><div class="banner">usage ledger unavailable: {esc(exc.__class__.__name__)}</div></section>'
    try:
        summary = usage_report.summary(conn, now)
        week = ledger.rows(conn, since=now - timedelta(days=7), kind=ledger.REQUEST)
        recent = ledger.recent_requests(conn, 25)
    finally:
        conn.close()
    cards = []
    for provider in ledger.PROVIDERS:
        mine = [r for r in week if r["provider"] == provider]
        top = usage_report.sorted_totals(ledger.rollup(mine, lambda r: f"{r['model'] or '?'} @ {r['effort'] or '?'}"), "model_effort")
        cards.append(usage_card_html(provider, summary[provider], top))
    return (
        f'<section class="usage"><h2>Token usage</h2><div class="cards">{"".join(cards)}</div>'
        f'{recent_table_html(recent)}<div class="note">{esc(USAGE_FOOTNOTE)}</div></section>'
    )


# -- page ------------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#ffffff;--fg:#1c1e21;--muted:#6b7280;--line:#e5e7eb;--grid:#eef0f3;--ideal:#9ca3af;--actual:#2563eb;--over:#dc2626;--under:#16a34a;--warn:#d97706;--now:#111827;
--chart-series:#2a78d6;--chart-grid:#e1e0d9;--chart-axis:#c3c2b7;--chart-muted:#898781;--chart-ink:#0b0b0b}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8eb;--muted:#9aa3ad;--line:#2a2f3a;--grid:#232834;--ideal:#6b7280;--actual:#60a5fa;--over:#f87171;--under:#4ade80;--warn:#fbbf24;--now:#e6e8eb;
--chart-series:#3987e5;--chart-grid:#2c2c2a;--chart-axis:#383835;--chart-muted:#898781;--chart-ink:#ffffff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,"Segoe UI",Roboto,sans-serif}
header.top{display:flex;align-items:baseline;justify-content:space-between;gap:16px;padding:18px 24px 6px;flex-wrap:wrap}
header.top h1{margin:0;font-size:20px}
.meta{color:var(--muted);font-size:12px}
main{padding:8px 24px 24px;max-width:1900px;margin:0 auto}
section.provider>h2{font-size:15px;margin:18px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(620px,1fr));gap:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;border-top:4px solid var(--ideal)}
.card.status-over{border-top-color:var(--over)}
.card.status-under{border-top-color:var(--under)}
.card.status-exhausted{border-top-color:var(--warn)}
.card.status-on-pace{border-top-color:var(--actual)}
.card.stale{opacity:.7}
.card header{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
.card h3{margin:0;font-size:15px}
.card h4{margin:14px 0 4px;font-size:12px;color:var(--muted);font-weight:500}
.badge{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.status-over .badge{color:var(--over);border-color:var(--over)}
.status-under .badge{color:var(--under);border-color:var(--under)}
.status-exhausted .badge{color:var(--warn);border-color:var(--warn)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:6px 0 10px}
.stats div{display:flex;flex-direction:column}
.stats b{font-size:22px;font-weight:600;line-height:1.1}
.stats span{font-size:11px;color:var(--muted)}
.foot{font-size:11px;color:var(--muted);margin-top:8px}
figure.chart-figure{margin:0}
svg.chart{width:100%;height:auto;display:block;outline:none;touch-action:none}
svg.chart:focus-visible{outline:2px solid var(--chart-series);outline-offset:2px;border-radius:6px}
.grid{stroke:var(--chart-grid);stroke-width:1}
.axis{stroke:var(--chart-axis);stroke-width:1}
.lbl{fill:var(--chart-muted);font-size:12px;font-variant-numeric:tabular-nums}
.future{fill:var(--chart-grid);opacity:.35}
.area{fill:var(--chart-series);opacity:.1}
.used{fill:none;stroke:var(--chart-series);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.pace{stroke:var(--chart-axis);stroke-width:1.5;stroke-dasharray:6 5;stroke-linecap:round}
.proj{stroke:var(--chart-ink);stroke-width:1.5;stroke-dasharray:1.5 4;stroke-linecap:round;opacity:.75}
.now{stroke:var(--chart-axis);stroke-width:1}
.reset-tick{stroke:var(--chart-axis);stroke-width:1.5}
.reset-lbl{fill:var(--chart-muted);font-size:11px}
.end-ring{fill:var(--card)}
.end-dot{fill:var(--chart-series)}
.end-lbl{fill:var(--chart-ink);font-size:13px;font-weight:600}
.crosshair{stroke:var(--chart-ink);stroke-width:1;opacity:0;pointer-events:none}
.hover-dot{fill:var(--chart-series);stroke:var(--card);stroke-width:2;opacity:0;pointer-events:none}
#chart-tooltip{position:absolute;z-index:5;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);pointer-events:none;white-space:nowrap}
#chart-tooltip b{font-size:14px;margin-right:6px}
details.chart-table{margin-top:6px;font-size:12px;color:var(--muted)}
details.chart-table summary{cursor:pointer}
details.chart-table table{margin-top:4px;max-height:220px;display:block;overflow:auto}
.banner{background:var(--card);border:1px solid var(--warn);border-radius:8px;padding:8px 12px;margin:8px 0;font-size:13px}
.legend{display:flex;gap:18px;align-items:center;font-size:12px;color:var(--muted);margin:4px 0 0;flex-wrap:wrap}
.range{display:inline-flex;gap:4px;align-items:center;margin-left:auto}
.range button{font:inherit;font-size:12px;padding:2px 9px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;cursor:pointer}
.range button.on{border-color:var(--chart-series);color:var(--chart-series);font-weight:600}
.range button:focus-visible{outline:2px solid var(--chart-series);outline-offset:1px}
.legend i{display:inline-block;width:22px;vertical-align:middle;margin-right:6px;border-top:2px solid var(--chart-series)}
.legend i.k-pace{border-top:2px dashed var(--chart-axis)}
.legend i.k-proj{border-top:2px dotted var(--chart-ink)}
.legend i.k-reset{width:2px;height:10px;border-top:0;border-left:2px solid var(--chart-axis)}
.legend i.k-now{width:1px;height:10px;border-top:0;border-left:1px solid var(--chart-axis)}
.empty{color:var(--muted);padding:32px 0;text-align:center}
footer{color:var(--muted);font-size:11px;padding:8px 24px 24px;max-width:1900px;margin:0 auto}
section.usage>h2{font-size:15px;margin:22px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
table.usage{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
table.usage th,table.usage td{padding:3px 6px;text-align:right;border-bottom:1px solid var(--grid);white-space:nowrap}
table.usage th{color:var(--muted);font-weight:500}
table.usage th:first-child,table.usage td:first-child{text-align:left}
table.usage tr.inferred td{color:var(--muted);font-style:italic}
.card.usage-card{border-top-color:var(--actual)}
.card.usage-card.muted{border-top-color:var(--ideal);opacity:.8}
.recent{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-top:16px;overflow-x:auto}
.recent h3{margin:0 0 8px;font-size:15px}
.note{font-size:11px;color:var(--muted);margin-top:8px}
"""

HOVER_SCRIPT = """
(function(){
  var RANGE_KEY = 'quota-burndown.range';
  var buttons = Array.prototype.slice.call(document.querySelectorAll('.range button[data-span]'));
  function applyRange(sel){
    Array.prototype.forEach.call(document.querySelectorAll('article'), function(card){
      var figures = Array.prototype.slice.call(card.querySelectorAll('figure.chart-figure[data-span]'));
      if (!figures.length) return;
      var shown = 0;
      figures.forEach(function(f){
        var want = sel === 'auto' ? f.getAttribute('data-auto') : sel;
        f.hidden = f.getAttribute('data-span') !== want;
        if (!f.hidden) shown++;
      });
      if (!shown) figures.forEach(function(f){ f.hidden = f.getAttribute('data-span') !== f.getAttribute('data-auto'); });
    });
    buttons.forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-span') === sel); });
  }
  if (buttons.length){
    var saved = 'auto';
    try { saved = localStorage.getItem(RANGE_KEY) || 'auto'; } catch (e) {}
    if (!buttons.some(function(b){ return b.getAttribute('data-span') === saved; })) saved = 'auto';
    applyRange(saved);
    buttons.forEach(function(b){
      b.addEventListener('click', function(){
        var sel = b.getAttribute('data-span');
        applyRange(sel);
        try { localStorage.setItem(RANGE_KEY, sel); } catch (e) {}
      });
    });
  }
  var tip = document.getElementById('chart-tooltip');
  if (!tip) return;
  Array.prototype.forEach.call(document.querySelectorAll('svg.chart[data-chart]'), function(svg){
    var dataEl = document.querySelector('script.chart-data[data-for="' + svg.getAttribute('data-chart') + '"]');
    if (!dataEl) return;
    var data;
    try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }
    var pts = data.points || [];
    if (!pts.length) return;
    var cross = svg.querySelector('.crosshair'), dot = svg.querySelector('.hover-dot');
    var vw = Number(svg.getAttribute('data-w')) || 800, vh = Number(svg.getAttribute('data-h')) || 320;
    var idx = -1;
    function viewX(evt){
      var r = svg.getBoundingClientRect();
      return (evt.clientX - r.left) / r.width * vw;
    }
    function nearest(x){
      var best = 0, dist = Infinity;
      for (var i = 0; i < pts.length; i++){ var d = Math.abs(pts[i].x - x); if (d < dist){ dist = d; best = i; } }
      return best;
    }
    function show(i){
      idx = i;
      var p = pts[i];
      cross.setAttribute('x1', p.x); cross.setAttribute('x2', p.x); cross.style.opacity = 1;
      dot.setAttribute('cx', p.x); dot.setAttribute('cy', p.y); dot.style.opacity = 1;
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      var v = document.createElement('b'); v.textContent = p.v; tip.appendChild(v);
      tip.appendChild(document.createTextNode('used ' + p.t + (data.reset ? ' \\u00b7 ' + data.reset : '')));
      tip.hidden = false;
      var r = svg.getBoundingClientRect();
      var sx = r.left + window.scrollX + p.x / vw * r.width, sy = r.top + window.scrollY + p.y / vh * r.height;
      var w = tip.offsetWidth || 160;
      tip.style.left = Math.max(4, Math.min(sx + 14, window.scrollX + document.documentElement.clientWidth - w - 8)) + 'px';
      tip.style.top = (sy - 36) + 'px';
    }
    function hide(){ cross.style.opacity = 0; dot.style.opacity = 0; tip.hidden = true; idx = -1; }
    svg.addEventListener('pointermove', function(e){ show(nearest(viewX(e))); });
    svg.addEventListener('pointerleave', hide);
    svg.addEventListener('blur', hide);
    svg.addEventListener('keydown', function(e){
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight'){
        e.preventDefault();
        var n = idx < 0 ? pts.length - 1 : idx + (e.key === 'ArrowRight' ? 1 : -1);
        show(Math.max(0, Math.min(pts.length - 1, n)));
      } else if (e.key === 'Escape'){ hide(); }
    });
  });
})();
"""

RANGE_OPTIONS = (("auto", "Auto"), *((key, key) for key in charts.SPANS))
RANGE_HTML = (
    '<div class="range" role="group" aria-label="Chart time range"><span>Range</span>'
    + "".join(f'<button type="button" data-span="{key}"{" class=\"on\"" if key == "auto" else ""}>{label}</button>' for key, label in RANGE_OPTIONS)
    + "</div>"
)
LEGEND_HTML = (
    '<div class="legend"><span><i class="k-used"></i>% used</span><span><i class="k-pace"></i>linear pace</span>'
    '<span><i class="k-proj"></i>projection at current rate</span><span><i class="k-reset"></i>window reset</span>'
    f'<span><i class="k-now"></i>now</span>{RANGE_HTML}</div>'
)


def render_html(store: Store, now: datetime | None = None, days: int = 7, warnings: list[str] | None = None, refresh_s: int = 120, usage_db: Path | None = None) -> str:
    """The whole page. `days` sizes how much sample history is loaded (at least 31 days, so
    every chart span up to 30 days can be drawn); the range control picks the span shown."""
    now = now or now_utc()
    samples = store.load(since=now - timedelta(days=max(days, 31)))
    latest = store.latest()
    burndowns = current(samples, latest, now)
    by_key: dict[str, list[Sample]] = {}
    for sample in samples:
        by_key.setdefault(sample.key, []).append(sample)

    sections: list[str] = []
    providers: dict[str, list[Burndown]] = {}
    for bd in burndowns:
        providers.setdefault(bd.provider, []).append(bd)
    chart_count = 0
    for provider, items in providers.items():
        cards = []
        for bd in items:
            chart_count += 1
            cards.append(card_html(bd, by_key.get(bd.key, []), now, f"c{chart_count}"))
        sections.append(f'<section class="provider"><h2>{esc(PROVIDER_TITLES.get(provider, provider))}</h2><div class="cards">{"".join(cards)}</div></section>')
    if not sections:
        sections.append('<p class="empty">No samples yet. Run <code>quota-burndown collect</code>.</p>')

    banner = ""
    if warnings:
        banner = '<div class="banner">' + "<br>".join(esc(w) for w in warnings) + "</div>"
    updated = to_local(now).strftime("%a %Y-%m-%d %H:%M")
    hover = f'<div id="chart-tooltip" role="status" aria-live="polite" hidden></div><script>{HOVER_SCRIPT}</script>' if chart_count else ""
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<meta http-equiv=\"refresh\" content=\"{int(refresh_s)}\">"
        f"<title>Quota Burndown</title><style>{CSS}</style></head><body>"
        f'<header class="top"><h1>Quota burndown</h1><div class="meta">updated {esc(updated)} · page reloads every {int(refresh_s) // 60} min · {len(samples)} samples in view</div></header>'
        "<main>"
        f"{banner}"
        f"{LEGEND_HTML}"
        + "".join(sections)
        + (usage_section_html(usage_db, now) if usage_db is not None else "")
        + "</main>"
        f"<footer>Above the dashed pace line = spending faster than a straight line to the reset (over pace); below it = under pace. Hover or focus a chart and use the arrow keys to read exact readings. quota-burndown {esc(__version__)}</footer>"
        f"{hover}"
        "</body></html>\n"
    )


def write_html(store: Store, path: Path, **kwargs) -> Path:
    atomic_write_text(path, render_html(store, **kwargs))
    return path
