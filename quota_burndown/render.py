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
from .capacity import FABLE_WEEKLY_UNAVAILABLE
from .charts import ChartData
from .ledger import Totals
from .model import Burndown, canonical_samples, current
from .store import Sample, Store
from .util import atomic_write_text, fmt_local, fmt_minutes, now_utc, read_json, to_local

STALE_MIN = 20
PROVIDER_TITLES = {"claude": "Claude", "codex": "Codex", "codex-spark": "Codex Spark", "antigravity": "Antigravity"}
WINDOW_TITLES = {"5h": "5-hour session", "7d": "7-day (all models)"}
FAMILY_TITLES = {"gpt": "GPT", "spark": "Spark", "fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "gemini": "Gemini"}
FABLE_WINDOW = "7d:fable"  # fixed chart slot beside the Claude all-models weekly chart
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
    if t1 < t0:
        return None
    if t1 == t0:
        if not (data.span_start <= t0 <= data.span_end):
            return None
        return _x_of(data, t0), _y_of(v0), _x_of(data, t0), _y_of(v1)
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
    for line in data.reset_pace:
        clipped = _clip(data, line)
        if clipped:
            parts.append('<line class="pace-reset" x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}"/>'.format(*clipped))
    if data.projection is not None:
        clipped = _clip(data, data.projection)
        if clipped:
            parts.append('<line class="proj" x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}"/>'.format(*clipped))
    for line in data.reset_projections:
        clipped = _clip(data, line)
        if clipped:
            parts.append('<line class="proj-reset" x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}"/>'.format(*clipped))
    for ts, used, label in data.reset_markers:
        if data.span_start <= ts <= data.span_end:
            cx, cy = _x_of(data, ts), _y_of(used)
            parts.append(f'<circle class="reset-marker" cx="{cx:.1f}" cy="{cy:.1f}" r="4.5"><title>{esc(label)}</title></circle>')
            anchor = "end" if cx > CHART_W * 0.75 else "start"
            dx = -8 if anchor == "end" else 8
            parts.append(f'<text class="reset-marker-lbl" x="{cx + dx:.1f}" y="{cy - 6:.1f}" text-anchor="{anchor}">{esc(label)}</text>')
    for mark in data.credit_expiries:
        if data.span_start <= mark.ts <= data.span_end:
            x = _x_of(data, mark.ts)
            parts.append(f'<line class="credit-expiry-tick" x1="{x:.1f}" y1="{plot_bottom}" x2="{x:.1f}" y2="{plot_bottom + 8:.1f}"/>')
            parts.append(f'<text class="credit-expiry-lbl" x="{x:.1f}" y="{plot_bottom + 18:.1f}" text-anchor="middle">credit expires</text>')
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


def chart_figure_html(data: ChartData, chart_id: str, title: str) -> str:
    """The one exact two-cycle chart rendered for a capacity card."""
    points = chart_points(data)
    reset = next((f"resets {fmt_local(m.ts)}" for m in data.resets if m.current), None)
    payload = json.dumps({"points": points, "reset": reset}, separators=(",", ":")).replace("</", "<\\/")
    return (
        f'<figure class="chart-figure" data-span="{esc(data.span_key)}" '
        f'data-domain-start="{esc(data.span_start.isoformat())}" data-domain-end="{esc(data.span_end.isoformat())}">'
        f'<h4>% used, {esc(data.span_label)}</h4>{chart_svg(data, chart_id, title)}'
        f'<script type="application/json" class="chart-data" data-for="{esc(chart_id)}">{payload}</script>'
        f"{chart_table_html(points)}</figure>"
    )


def card_figures_html(bd: Burndown, history: list[Sample], now: datetime, chart_id: str, title: str) -> str:
    data = charts.build(bd, history, now)
    if data is None:
        return '<div class="note">No readings in the two-cycle domain.</div>'
    return chart_figure_html(data, chart_id, title)


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
    data = charts.build(bd, history, now)
    if data is None:
        figure = '<div class="note">No readings in the two-cycle domain.</div>'
    else:
        figure = chart_figure_html(data, chart_id, title)
    reset_strip = ""
    reset_badge = ""
    if data is not None and data.reset_credits_count > 0:
        reset_badge = f'<span class="badge reset-badge">{data.reset_credits_count} reset credit{"s" if data.reset_credits_count != 1 else ""}</span>'
        strip_parts = [f"<b>{data.reset_credits_count} reset credit{'s' if data.reset_credits_count != 1 else ''} available</b>"]
        if data.credit_expiries:
            strip_parts.append(f"earliest expires {esc(fmt_local(data.credit_expiries[0].ts))}")
        if data.next_reset_time is not None:
            strip_parts.append(f"ideal next reset: <b>{esc(fmt_local(data.next_reset_time))}</b>")
        if data.runway_with_resets_min is not None:
            strip_parts.append(f"runway with resets: <b>{esc(fmt_minutes(data.runway_with_resets_min))}</b>")
        reset_strip = f'<div class="reset-strip">{" · ".join(strip_parts)}</div>'
    header_badge = f'<div class="badges"><span class="badge">{esc(badge_text(bd))}</span>{reset_badge}</div>' if reset_badge else f'<span class="badge">{esc(badge_text(bd))}</span>'
    age = f"{bd.age_min:.0f} min ago" if bd.age_min is not None else "never"
    foot = f"last sample {esc(age)} via {esc(bd.source or '?')} · {len(bd.samples)} samples this window"
    if stale:
        foot += " · <b>stale</b>: collector has not run recently"
    return (
        f'<article class="{classes}">'
        f'<header><h3>{esc(title)}</h3>{header_badge}</header>'
        f'<div class="stats">{stats}</div>'
        f"{reset_strip}"
        f"{figure}"
        f'<div class="foot">{foot}</div>'
        "</article>"
    )


def history_group(bd: Burndown) -> str:
    """Heading for a historic chart cell; Codex Spark is presented as its own provider."""
    if bd.provider == "codex" and bd.window.partition(":")[2].lower() == "spark":
        return "codex-spark"
    return bd.provider


def unavailable_card_html(window: str, reason: str) -> str:
    """A fixed chart slot with no readings keeps the grid's shape and says why it is empty."""
    stats = "".join(f'<div><b>—</b><span>{label}</span></div>' for label in ("used", "pace", "time left", "projection"))
    return (
        '<article class="card unavailable">'
        f'<header><h3>{esc(window_title(window))}</h3><span class="badge">no readings</span></header>'
        f'<div class="stats">{stats}</div>'
        f'<p class="empty">{esc(reason)}</p>'
        '<div class="foot">last sample never · 0 samples</div>'
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


def daily_models_html(data: dict) -> str:
    """One shared SVG/table renderer for the static page and live usage response."""
    dates = data["dates"]
    cards = []
    for group in data["providers"]:
        title = esc(group["label"])
        models = group["models"]
        recorded, excluded = group["recorded_requests"], group["excluded_requests"]
        coverage = f'{recorded:,} requests with recorded totals.'
        if excluded:
            coverage += f' Incomplete data: {excluded:,} requests excluded because token totals are unknown.'
        parts = []
        if recorded:
            peak = max(group["daily_tokens"]) or 1
            for tick in range(5):
                y = 250 - tick * 50
                parts.append(f'<line class="grid" x1="65" y1="{y}" x2="785" y2="{y}"/>')
                parts.append(f'<text class="lbl" x="57" y="{y + 4}" text-anchor="end">{esc(_n(round(peak * tick / 4)))}</text>')
            for i, day in enumerate(dates):
                x, stacked = 78 + i * 101, 0
                for model in models:
                    tokens = model["tokens"][i]
                    height = tokens / peak * 200
                    if tokens:
                        label = f'{day} · {model["label"]}: {tokens:,} tokens'
                        parts.append(f'<rect x="{x}" y="{250 - stacked - height:.2f}" width="70" height="{height:.2f}" fill="{esc(model["color"])}"><title>{esc(label)}</title></rect>')
                    stacked += height
                parts.append(f'<text class="lbl" x="{x + 35}" y="{240 - stacked:.2f}" text-anchor="middle">{esc(_n(group["daily_tokens"][i]))}</text>')
                parts.append(f'<text class="lbl" x="{x + 35}" y="275" text-anchor="middle">{day[5:]}</text>')
            chart = f'<svg class="model-token-chart" viewBox="0 0 800 290" role="img" aria-label="{title} daily recorded tokens by model"><title>{title} daily recorded tokens by model; exact values in the data table</title>{"".join(parts)}</svg>'
        else:
            chart = '<p class="empty">Recorded token totals unavailable.</p>'
        legend = ''.join(
            f'<li><i style="background:{esc(model["color"])}"></i>{esc(model["label"])} · {model["total_tokens"]:,} recorded tokens</li>'
            for model in models
        )
        body = ''.join(
            f'<tr><th scope="row">{esc(model["label"])}</th>'
            + ''.join(f'<td>{tokens:,}</td>' for tokens in model["tokens"])
            + f'<td>{model["total_tokens"]:,}</td><td>{model["excluded_requests"]:,}</td></tr>'
            for model in models
        )
        totals = ''.join(f'<td>{tokens:,}</td>' for tokens in group["daily_tokens"])
        # Provider values originate in ledger records; encode rather than interpolate into IDs.
        detail_id = 'model-token-table-' + group['provider'].encode().hex()
        table = (
            f'<details id="{detail_id}" class="model-token-table"><summary>Daily/model totals · {title}</summary>'
            f'<table class="usage"><caption>{title} recorded tokens · local dates; unknown totals excluded</caption>'
            '<thead><tr><th scope="col">Model</th>'
            + ''.join(f'<th scope="col">{day}</th>' for day in dates)
            + '<th scope="col">Total</th><th scope="col">Excluded requests</th></tr></thead>'
            + f'<tbody>{body}</tbody><tfoot><tr><th scope="row">Total</th>{totals}<td>{group["total_tokens"]:,}</td><td>{excluded:,}</td></tr></tfoot></table></details>'
        )
        cards.append(f'<article class="model-token-card"><h3>{title}</h3><p>{group["total_tokens"]:,} recorded tokens</p><p class="note">{coverage}</p>{chart}<ul class="model-token-legend">{legend}</ul>{table}</article>')
    return '<div class="model-token-grid">' + ''.join(cards) + '</div>'


def daily_models_section_html(usage_db: Path, now: datetime) -> str:
    try:
        conn = ledger.connect(usage_db)
        try:
            content = daily_models_html(usage_report.daily_model_payload(conn, now))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        content = f'<p class="banner">Usage ledger unavailable: {esc(type(exc).__name__)}</p>'
    return (
        '<section class="model-tokens"><h2>Recorded tokens by model · last 7 days</h2>'
        '<p class="note">Today and the preceding six local calendar days. Locally recorded tokens, not subscription quota share. Effort levels are combined; today is partial.</p>'
        f'<div id="daily-models-panel">{content}</div></section>'
    )


def efficiency_table_html(rows: list[dict], title: str) -> str:
    """A compact, escaped ledger drilldown.  This is presentation-only accounting."""
    detail_id = "efficiency-session" if title == "By session" else "efficiency-model"
    if not rows:
        return f'<details id="{detail_id}" class="efficiency"><summary>{esc(title)}: no requests in range</summary></details>'
    body = []
    for row in rows[:20]:
        if row.get("exact"):
            reasoning = "unknown" if row.get("reasoning_share_of_output_pct") is None else f"{row['reasoning_share_of_output_pct']:.1f}%"
            median = "—" if row.get("median_tokens_per_request") is None else _n(int(row["median_tokens_per_request"]))
            values = tuple(_n(row[field]) if row.get(field) is not None else "unknown" for field in ("uncached_input", "cached_input", "output")) + (reasoning, str(row["requests"]), median)
        else:
            values = ("unknown", "unknown", "unknown", "unknown", str(row["requests"]), "unknown")
        anchor = f' id="{esc(row["anchor"])}"' if row.get("anchor") else ""
        body.append("<tr" + anchor + ">" + f"<td>{esc(row['key'])}</td>" + "".join(f"<td>{esc(value)}</td>" for value in values) + "</tr>")
    return (
        f'<details id="{detail_id}" class="efficiency"><summary>{esc(title)} (last 7 days)</summary>'
        '<table class="usage"><thead><tr><th>group</th><th>uncached in</th><th>cached in</th><th>output</th><th>reasoning / output</th><th>requests</th><th>median tokens/request</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table><div class="note">Uncached input normalizes Codex inclusive input by subtracting cache tokens once; ledger totals are unchanged.</div></details>'
    )


def efficiency_section_html(usage_db: Path, now: datetime) -> str:
    try:
        conn = ledger.connect(usage_db)
        try:
            data = usage_report.efficiency_payload(conn, now)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f'<section class="efficiency-section"><h2>Efficiency drilldowns</h2><div class="banner">usage ledger unavailable: {esc(exc.__class__.__name__)}</div></section>'
    insight_html = "".join(
        f'<li>{esc(item["label"])}: {_n(int(item["value"]))} <a href="#{esc(item["session_anchor"])}">view session evidence</a></li>'
        for item in data.get("insights", [])
    )
    return (
        '<section class="efficiency-section"><h2>Efficiency drilldowns</h2>'
        '<div class="note">Evidence-linked local ledger aggregates. Antigravity activity has unknown exact token capacity and counts.</div>'
        f'<ul id="efficiency-insights" class="insights">{insight_html}</ul>'
        f'<div id="efficiency-session-panel">{efficiency_table_html(data["by_session"], "By session")}</div>'
        f'<div id="efficiency-model-panel">{efficiency_table_html(data["by_model_effort"], "By model × effort")}</div>'
        '</section>'
    )


# -- live capacity --------------------------------------------------------------------

def _capacity_window_html(window: dict) -> str:
    if not isinstance(window, dict):
        window = {}
    remaining = window.get("remaining_pct")
    usable = window.get("usable_pct")
    state = window.get("allowance_state") or "unknown"
    freshness = window.get("freshness") or "unknown"
    remaining_text = "unknown" if remaining is None else f"{float(remaining):.0f}%"
    budget = "unknown" if usable is None else f"{float(usable):.0f}%"
    reset = window.get("resets_at") or "unknown"
    runway = window.get("runway_minutes")
    runway_text = "unknown" if runway is None else fmt_minutes(float(runway))
    age = window.get("source_age_s")
    age_text = "unknown" if age is None else f'<span class="source-age" data-source-age="{float(age):.3f}">{float(age):.0f}s ago</span>'
    values = (window.get("window") or "window", remaining_text, budget, reset, runway_text)
    cells = "".join(f"<td>{esc(value)}</td>" for value in values)
    cells += f"<td>{esc(freshness)} · {age_text}</td><td>{esc(window.get('source') or 'unknown')}</td>"
    return "<tr>" + cells + "</tr>"


def _capacity_value(value, suffix: str = "%") -> str:
    """Format reported capacity values without turning missing values into zero."""
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.0f}{suffix}"
    except (TypeError, ValueError):
        return "unknown"


def _capacity_rate(value) -> str:
    try:
        return f"{float(value):.2f} pp/hour"
    except (TypeError, ValueError):
        return "unknown"


def _group_limit_row(limit: dict) -> str:
    window = limit.get("window") if isinstance(limit.get("window"), dict) else {}
    unavailable = not limit.get("pool_id")
    reason = limit.get("availability_reason")
    models = ", ".join(str(model) for model in (limit.get("models") or [])) or "unknown"
    scope = limit.get("account_scope") or "unknown"
    mapping = limit.get("mapping_confidence") or "unknown"
    remaining = _capacity_value(window.get("remaining_pct"))
    used = _capacity_value(window.get("used_pct"))
    reserve = _capacity_value(window.get("usable_pct"))
    runway = window.get("runway_minutes")
    runway_text = "unknown" if runway is None else fmt_minutes(float(runway))
    whole_rate = _capacity_rate(window.get("whole_window_rate_pph"))
    recent_rate = _capacity_rate(window.get("recent_rate_pph"))
    age = window.get("source_age_s")
    age_html = "unknown" if age is None else f'<span class="source-age" data-source-age="{float(age):.3f}">{float(age):.0f}s ago</span>'
    deadline = window.get("valid_until")
    freshness_html = f'<span data-freshness-deadline="{esc(deadline)}">{esc(window.get("freshness") or "unknown")}</span>' if deadline else esc(window.get("freshness") or "unknown")
    provenance = f'<br><small>reset: {esc(window.get("reset_provenance") or "unknown")}; observation: {esc(window.get("observation_time_provenance") or "unknown")}</small>'
    availability = f"<br><small>{esc(reason)}</small>" if reason else ""
    pool = "unreported" if unavailable else esc(limit.get("pool_id"))
    credits = limit.get("reset_credits") or []
    credits_html = ""
    if credits:
        credits_count = len(credits)
        first_exp = credits[0].get("expires_at")
        exp_txt = ""
        if first_exp:
            try:
                dt = parse_iso(first_exp) if isinstance(first_exp, str) else first_exp
                exp_txt = f" (expires {fmt_local(dt)})"
            except Exception:
                pass
        credits_html = f'<br><span class="badge reset-badge">{credits_count} reset credit{"s" if credits_count != 1 else ""}{exp_txt}</span>'
    return (
        f'<tr data-reset="{esc(window.get("resets_at") or "")}">'
        f'<th scope="row">{esc(limit.get("label") or limit.get("id") or "limit")}{availability}</th>'
        f'<td>{pool}<br><small>{esc(scope)} · {esc(models)} · mapping {esc(mapping)}<br>'
        f'constraining: {esc(limit.get("constraining_window") or "unknown")}</small>{credits_html}</td>'
        f'<td>{used}</td><td class="window-budget">{remaining}</td>'
        f'<td class="window-budget">{reserve}</td><td>{esc(window.get("resets_at") or "unknown")}</td>'
        f'<td class="window-forecast">{esc(runway_text)}<br><small>whole: {whole_rate}; last hour: {recent_rate}</small></td>'
        f'<td>{freshness_html} · {age_html}</td>'
        f'<td>{esc(window.get("source") or "unavailable")}{provenance}</td></tr>'
    )


def _group_extras_html(capacity: dict, group: dict) -> str:
    """Render observations that did not match one of the fixed consumer slots."""
    provider = group.get("provider")
    seen = {
        (limit.get("pool_id"), (limit.get("window") or {}).get("window"))
        for limit in (group.get("limits") or []) if isinstance(limit, dict) and limit.get("pool_id") and ":reported-" not in str(limit.get("id"))
    }
    rows = []
    for pool in capacity.get("pools") or []:
        if not isinstance(pool, dict) or pool.get("provider") != provider:
            continue
        for window in pool.get("windows") or []:
            if not isinstance(window, dict) or (pool.get("id"), window.get("window")) in seen:
                continue
            rows.append(_group_limit_row({
                "id": f"reported-{pool.get('id')}-{window.get('window')}",
                "label": f"{pool.get('label') or pool.get('limit_id') or 'reported pool'} · {window.get('window') or 'window'}",
                "pool_id": pool.get("id"), "account_scope": pool.get("account_scope"),
                "models": pool.get("models"), "mapping_confidence": pool.get("mapping_confidence"),
                "constraining_window": pool.get("constraining_window"), "window": window,
            }))
    if not rows:
        return ""
    return (
        f'<details class="capacity-extras" data-extra-provider="{esc(provider)}"><summary>Other observed windows</summary>'
        '<div class="capacity-scroll"><table class="usage capacity-table"><thead><tr><th>allowance</th><th>pool / membership</th><th>used</th><th>remaining</th><th>reserve-adjusted budget</th><th>reset</th><th>runway</th><th>freshness</th><th>source</th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table></div></details>"
    )


def _provider_groups_html(capacity: dict) -> str:
    groups = capacity.get("provider_groups")
    if not isinstance(groups, list):
        return ""
    rendered = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        rows = "".join(_group_limit_row(limit) for limit in (group.get("limits") or []) if isinstance(limit, dict) and ":reported-" not in str(limit.get("id")))
        rendered.append(
            f'<section class="capacity-provider" data-provider="{esc(group.get("provider") or "unknown")}">'
            f'<h3>{esc(group.get("label") or group.get("provider") or "Provider")}</h3>'
            '<div class="capacity-scroll"><table class="usage capacity-table"><thead><tr><th>allowance</th><th>pool / membership</th><th>used</th><th>remaining</th><th>reserve-adjusted budget</th><th>reset</th><th>runway</th><th>freshness</th><th>source</th></tr></thead><tbody>'
            + (rows or '<tr><td colspan="9">no reported allowances</td></tr>') + "</tbody></table></div>"
            + _group_extras_html(capacity, group) + "</section>"
        )
    return "".join(rendered)


def capacity_matrix_html(capacity: dict | None, live: bool = False) -> str:
    """The capacity contract is observational: unknown values stay visibly unknown."""
    if not isinstance(capacity, dict) or not capacity:
        return '<section id="capacity" class="capacity"><h2>Capacity</h2><div class="banner">Capacity is unknown: snapshot unavailable; quota cards below are historic readings.</div></section>'
    policy = capacity.get("policy") if isinstance(capacity.get("policy"), dict) else {}
    reserve = policy.get("reserve_pct", 10)
    presets = policy.get("presets", (5, 10, 20))
    grouped = _provider_groups_html(capacity)
    rows = []
    for pool in capacity.get("pools") or []:
        if not isinstance(pool, dict):
            continue
        windows = [window for window in (pool.get("windows") or [{}]) if isinstance(window, dict)] or [{}]
        pool_label = pool.get("label") or f"{pool.get('provider', '?')} / {pool.get('limit_id', '?')}"
        models = ", ".join(pool.get("models") or []) or "unknown"
        first = True
        for window in windows:
            pool_cells = f'<td rowspan="{len(windows)}">{esc(pool_label)}<br><small>{esc(pool.get("account_scope", "unknown"))}<br>{esc(models)} · mapping {esc(pool.get("mapping_confidence") or "unknown")}<br>constraining: {esc(pool.get("constraining_window", "unknown"))}</small></td>' if first else ""
            first = False
            rows.append("<tr>" + pool_cells + _capacity_window_html(window)[4:])
    controls = "".join(
        f'<button type="button" data-reserve="{int(value)}" class="{"on" if value == reserve else ""}">{int(value)}%</button>'
        for value in presets
    )
    fallback_table = (
        '<div class="capacity-scroll"><table class="usage capacity-table"><thead><tr><th>pool / associated models</th><th>window</th><th>remaining</th><th>reserve-adjusted budget</th><th>reset</th><th>runway</th><th>freshness</th><th>source</th></tr></thead>'
        + '<tbody>' + ("".join(rows) or '<tr><td colspan="8">no reported pools</td></tr>') + "</tbody></table></div>"
    )
    matrix = grouped or fallback_table
    mode = "live" if live else "static fallback"
    health = capacity.get("collector_health") if isinstance(capacity.get("collector_health"), dict) else {}
    health_text = ", ".join(f"{name}: {(info if isinstance(info, dict) else {}).get('state', 'unknown')}" for name, info in health.items()) or "unknown"
    provider_states = capacity.get("provider_states") if isinstance(capacity.get("provider_states"), dict) else {}
    def provider_state(name):
        value = provider_states.get(name, "unknown")
        return value.get("state", "unknown") if isinstance(value, dict) else value
    provider_text = ", ".join(f"{name}: {provider_state(name)}" for name in ("codex", "claude", "antigravity"))
    return (
        '<section id="capacity" class="capacity" aria-live="polite"><header><h2>Capacity</h2>'
        f'<span id="capacity-state" class="badge">{esc(mode)} · rev {esc(capacity.get("revision", "?"))}</span></header>'
        f'<div id="capacity-observation-state" class="note">Pools are shared allowances. Models show membership only. Unreported in-flight usage: {esc(capacity.get("unreported_in_flight_usage", "unknown"))}. Provider observations: {esc(provider_text)}. Collector health: {esc(health_text)}.</div>'
        '<div class="reserve" role="group" aria-label="Reserve policy"><span>Reserve</span>' + controls + '</div>'
        f'<div id="capacity-groups">{matrix}</div></section>'
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
.history-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;align-items:start}
.history-grid section.provider{min-width:0}
.history-grid .cards{grid-template-columns:minmax(0,1fr)}
.model-token-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:20px}
.model-token-card{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.model-token-card h3{margin:0}.model-token-chart{display:block;width:100%;height:auto}
.model-token-chart .lbl{fill:var(--muted);font-size:12px}.model-token-chart .grid{stroke:var(--line)}
.model-token-legend{list-style:none;padding:0;display:flex;gap:8px 16px;flex-wrap:wrap;font-size:12px;overflow-wrap:anywhere}
.model-token-legend i{display:inline-block;width:10px;height:10px;margin-right:6px;border-radius:2px}
.model-token-table{overflow:auto}.model-token-table summary{cursor:pointer}.model-token-table table{white-space:nowrap}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(620px,1fr));gap:20px}
@media (max-width:1200px){.history-grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;border-top:4px solid var(--ideal)}
.card.status-over{border-top-color:var(--over)}
.card.status-under{border-top-color:var(--under)}
.card.status-exhausted{border-top-color:var(--warn)}
.card.status-on-pace{border-top-color:var(--actual)}
.card.stale{opacity:.7}
.card.unavailable{opacity:.8}
.card header{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
.card h3{margin:0;font-size:15px}
.card h4{margin:14px 0 4px;font-size:12px;color:var(--muted);font-weight:500}
.badge{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.badges{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.reset-badge{color:#10b981;border-color:#10b981;font-weight:500}
.reset-strip{font-size:12px;color:var(--fg);background:var(--grid);padding:5px 10px;border-radius:6px;margin:4px 0 8px}
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
.pace-reset{stroke:var(--chart-series);stroke-width:1.5;stroke-dasharray:4 4;stroke-linecap:round;opacity:.7}
.proj{stroke:var(--chart-ink);stroke-width:1.5;stroke-dasharray:1.5 4;stroke-linecap:round;opacity:.75}
.proj-reset{stroke:#10b981;stroke-width:2;stroke-dasharray:3 3;stroke-linecap:round}
.reset-marker{fill:#10b981;stroke:var(--card);stroke-width:2}
.reset-marker-lbl{fill:#10b981;font-size:11px;font-weight:600}
.credit-expiry-tick{stroke:var(--warn);stroke-width:1.5}
.credit-expiry-lbl{fill:var(--warn);font-size:10px}
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
.legend i{display:inline-block;width:22px;vertical-align:middle;margin-right:6px;border-top:2px solid var(--chart-series)}
.legend i.k-pace{border-top:2px dashed var(--chart-axis)}
.legend i.k-pace-reset{border-top:2px dashed var(--chart-series)}
.legend i.k-proj{border-top:2px dotted var(--chart-ink)}
.legend i.k-proj-reset{border-top:2px dashed #10b981}
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
section.capacity{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:8px 0 18px;border-top:4px solid var(--actual)}
section.capacity header{display:flex;justify-content:space-between;gap:8px;align-items:center}section.capacity h2,.efficiency-section h2{font-size:15px;margin:0;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.reserve{display:flex;gap:5px;align-items:center;margin:10px 0;font-size:12px}.reserve button{font:inherit;font-size:12px;padding:2px 9px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;cursor:pointer}.reserve button.on{border-color:var(--chart-series);color:var(--chart-series);font-weight:600}.reserve button:focus-visible{outline:2px solid var(--chart-series);outline-offset:1px}
.capacity-scroll{overflow-x:auto}.capacity-table small{color:var(--muted);font-weight:400}.efficiency-section{margin-top:18px}.efficiency{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-top:8px;overflow-x:auto}.efficiency summary{cursor:pointer;font-weight:600}.efficiency table{margin-top:8px}
.capacity-provider{margin:16px 0}.capacity-provider h3{font-size:14px;margin:0 0 6px}.capacity-extras{margin-top:8px;font-size:12px}.capacity-extras summary{cursor:pointer;color:var(--muted)}
"""

HOVER_SCRIPT = """
(function(){
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

CAPACITY_SCRIPT = """
(function(){
  var section = document.getElementById('capacity');
  if (!section || !window.EventSource) return;
  var groupsHost = document.getElementById('capacity-groups'), state = document.getElementById('capacity-state'), ageStarted = Date.now();
  function text(v){ return v === undefined || v === null || v === '' ? 'unknown' : String(v); }
  function pct(v){ return v === undefined || v === null ? 'unknown' : Math.round(Number(v)) + '%'; }
  function age(v){ return v === undefined || v === null ? 'unknown' : '<span class="source-age" data-source-age="' + Number(v) + '">' + Math.round(Number(v)) + 's ago</span>'; }
  function sourceAge(w){ var observed=w && w.observed_at ? Date.parse(w.observed_at) : NaN; return Number.isFinite(observed) ? Math.max(0, (Date.now()-observed)/1000) : w && w.source_age_s; }
  function freshness(w){ var deadline=w && w.valid_until ? ' data-freshness-deadline="' + esc(w.valid_until) + '"' : ''; return '<span' + deadline + '>' + esc(w && w.freshness) + '</span>'; }
  function provenance(w){ return '<br><small>reset: ' + esc(w && w.reset_provenance) + '; observation: ' + esc(w && w.observation_time_provenance) + '; TTL ' + esc(w && w.freshness_ttl_s) + 's</small>'; }
  function runway(v){ return v === undefined || v === null ? 'unknown' : Math.round(Number(v)) + ' min'; }
  function rate(v){ return v === undefined || v === null ? 'unknown' : Number(v).toFixed(2) + ' pp/hour'; }
  function esc(v){ var d=document.createElement('div'); d.textContent=text(v); return d.innerHTML; }
  function tickAges(){
    var elapsed=(Date.now()-ageStarted)/1000;
    Array.prototype.forEach.call(section.querySelectorAll('[data-source-age]'), function(el){
      el.textContent=Math.floor(Number(el.getAttribute('data-source-age')) + elapsed) + 's ago';
    });
    Array.prototype.forEach.call(section.querySelectorAll('[data-freshness-deadline]'), function(el){
      if(Date.now() >= Date.parse(el.getAttribute('data-freshness-deadline'))){
        el.textContent='stale';
        var row=el.closest('tr'); if(row) Array.prototype.forEach.call(row.querySelectorAll('.window-forecast'), function(cell){cell.textContent='unknown';});
      }
    });
    Array.prototype.forEach.call(section.querySelectorAll('[data-reset]'), function(row){
      if(Date.now() >= Date.parse(row.getAttribute('data-reset'))){
        Array.prototype.forEach.call(row.querySelectorAll('.window-budget,.window-forecast'), function(el){el.textContent='unknown';});
      }
    });
  }
  setInterval(tickAges, 1000);
  var lastInstance=null, lastRevision=-1;
  function groupRow(l){
    var w=l.window || {}, reason=l.availability_reason ? '<br><small>' + esc(l.availability_reason) + '</small>' : '';
    return '<tr data-reset="' + esc(w.resets_at) + '"><th scope="row">' + esc(l.label || l.id || 'limit') + reason + '</th><td>' + (l.pool_id ? esc(l.pool_id) : 'unreported') + '<br><small>' + esc(l.account_scope) + ' · ' + esc((l.models || []).join(', ') || 'unknown') + ' · mapping ' + esc(l.mapping_confidence) + '<br>constraining: ' + esc(l.constraining_window) + '</small></td><td>' + pct(w.used_pct) + '</td><td class="window-budget">' + pct(w.remaining_pct) + '</td><td class="window-budget">' + pct(w.usable_pct) + '</td><td>' + esc(w.resets_at) + '</td><td class="window-forecast">' + runway(w.runway_minutes) + '<br><small>whole: ' + rate(w.whole_window_rate_pph) + '; last hour: ' + rate(w.recent_rate_pph) + '</small></td><td>' + freshness(w) + ' · ' + age(sourceAge(w)) + '</td><td>' + esc(w.source || 'unavailable') + provenance(w) + '</td></tr>';
  }
  function grouped(snapshot){
    if (!Array.isArray(snapshot.provider_groups)) return '';
    var used = {};
    snapshot.provider_groups.forEach(function(g){ (g.limits || []).forEach(function(l){ if(l && l.pool_id && String(l.id).indexOf(':reported-') < 0) used[l.pool_id + '\\u0000' + text((l.window || {}).window)] = true; }); });
    return snapshot.provider_groups.map(function(g){
      if (!g || typeof g !== 'object') return '';
      var rows=(g.limits || []).filter(function(l){return l && typeof l === 'object' && String(l.id).indexOf(':reported-') < 0;}).map(groupRow).join('') || '<tr><td colspan="9">no reported allowances</td></tr>';
      var extras=[];
      (snapshot.pools || []).forEach(function(p){ if(!p || p.provider !== g.provider) return; (p.windows || []).forEach(function(w){ if(!w || used[p.id + '\\u0000' + text(w.window)]) return; extras.push(groupRow({id:'reported-' + p.id + '-' + w.window,label:(p.label || p.limit_id || 'reported pool') + ' · ' + (w.window || 'window'),pool_id:p.id,account_scope:p.account_scope,models:p.models,mapping_confidence:p.mapping_confidence,constraining_window:p.constraining_window,window:w})); }); });
      var extraHtml=extras.length ? '<details class="capacity-extras" data-extra-provider="' + esc(g.provider) + '"><summary>Other observed windows</summary><div class="capacity-scroll"><table class="usage capacity-table"><thead><tr><th>allowance</th><th>pool / membership</th><th>used</th><th>remaining</th><th>reserve-adjusted budget</th><th>reset</th><th>runway</th><th>freshness</th><th>source</th></tr></thead><tbody>' + extras.join('') + '</tbody></table></div></details>' : '';
      return '<section class="capacity-provider" data-provider="' + esc(g.provider) + '"><h3>' + esc(g.label || g.provider || 'Provider') + '</h3><div class="capacity-scroll"><table class="usage capacity-table"><thead><tr><th>allowance</th><th>pool / membership</th><th>used</th><th>remaining</th><th>reserve-adjusted budget</th><th>reset</th><th>runway</th><th>freshness</th><th>source</th></tr></thead><tbody>' + rows + '</tbody></table></div>' + extraHtml + '</section>';
    }).join('');
  }
  function render(snapshot){
    if (!snapshot || !groupsHost) return;
    if (snapshot.instance_id === lastInstance && snapshot.revision < lastRevision) return;
    lastInstance=snapshot.instance_id; lastRevision=snapshot.revision;
    var active = document.activeElement, reserveFocus = active && active.getAttribute && active.getAttribute('data-reserve');
    var scroller = section.querySelector('.capacity-scroll'), left = scroller ? scroller.scrollLeft : 0, providerScroll = {};
    Array.prototype.forEach.call(section.querySelectorAll('.capacity-provider'), function(provider){ var scroll=provider.querySelector(':scope > .capacity-scroll'); if(scroll) providerScroll[provider.getAttribute('data-provider')]=scroll.scrollLeft; });
    var extraState={};
    Array.prototype.forEach.call(section.querySelectorAll('details.capacity-extras'), function(detail){ var scroll=detail.querySelector('.capacity-scroll'); extraState[detail.getAttribute('data-extra-provider')]={open:detail.open,left:scroll ? scroll.scrollLeft : 0,focused:document.activeElement === detail.querySelector('summary')}; });
    var rows = [];
    (snapshot.pools || []).forEach(function(pool){
      var wins = pool.windows && pool.windows.length ? pool.windows : [{}], label = pool.label || (text(pool.provider) + ' / ' + text(pool.limit_id));
      var models = (pool.models || []).join(', ') || 'unknown';
      wins.forEach(function(w, i){
        var poolCell = i ? '' : '<td rowspan="' + wins.length + '">' + esc(label) + '<br><small>' + esc(pool.account_scope) + '<br>' + esc(models) + ' · mapping ' + esc(pool.mapping_confidence) + '<br>constraining: ' + esc(pool.constraining_window) + '</small></td>';
        rows.push('<tr data-reset="' + esc(w.resets_at) + '">' + poolCell + '<td>' + esc(w.window || 'window') + '</td><td class="window-budget">' + pct(w.remaining_pct) + '</td><td class="window-budget">' + pct(w.usable_pct) + '</td><td>' + esc(w.resets_at) + '</td><td class="window-forecast">' + runway(w.runway_minutes) + '</td><td>' + freshness(w) + ' · ' + age(sourceAge(w)) + '</td><td>' + esc(w.source) + provenance(w) + '</td></tr>');
      });
    });
    var groupedHtml=grouped(snapshot);
    groupsHost.innerHTML = groupedHtml || '<div class="capacity-scroll"><table class="usage capacity-table"><thead><tr><th>pool / associated models</th><th>window</th><th>remaining</th><th>reserve-adjusted budget</th><th>reset</th><th>runway</th><th>freshness</th><th>source</th></tr></thead><tbody>' + (rows.join('') || '<tr><td colspan="8">no reported pools</td></tr>') + '</tbody></table></div>';
    Object.keys(extraState).forEach(function(provider){ var extra=section.querySelector('[data-extra-provider="' + provider + '"]'), saved=extraState[provider]; if(!extra) return; extra.open=saved.open; var scroll=extra.querySelector('.capacity-scroll'); if(scroll) scroll.scrollLeft=saved.left; if(saved.focused) extra.querySelector('summary').focus(); });
    if (state) state.textContent = 'live · rev ' + text(snapshot.revision);
    var observation=document.getElementById('capacity-observation-state');
    if(observation){
      var states=snapshot.provider_states || {}, health=snapshot.collector_health || {};
      observation.textContent='Pools are shared allowances. Models show membership only. Unreported in-flight usage: unknown. Provider observations: ' + ['codex','claude','antigravity'].map(function(p){return p + ': ' + text(typeof states[p] === 'object' ? states[p].state : states[p]);}).join(', ') + '. Collector health: ' + Object.keys(health).map(function(p){return p + ': ' + text(health[p].state);}).join(', ');
    }
    Array.prototype.forEach.call(section.querySelectorAll('[data-reserve]'),function(b){b.classList.toggle('on', Number(b.getAttribute('data-reserve')) === Number((snapshot.policy || {}).reserve_pct));});
    ageStarted = Date.now(); tickAges();
    Array.prototype.forEach.call(section.querySelectorAll('.capacity-provider'), function(provider){ var scroll=provider.querySelector(':scope > .capacity-scroll'), saved=providerScroll[provider.getAttribute('data-provider')]; if(scroll && saved !== undefined) scroll.scrollLeft=saved; });
    if (!groupedHtml && scroller) section.querySelector('.capacity-scroll').scrollLeft = left;
    if (reserveFocus) { var button = section.querySelector('[data-reserve="' + reserveFocus + '"]'); if (button) button.focus(); }
  }
  function reserve(button){
    fetch('/v1/policy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({reserve_pct:Number(button.getAttribute('data-reserve'))})})
      .catch(function(){ /* keep the server policy authoritative */ });
  }
  section.addEventListener('click', function(event){ var b=event.target.closest('[data-reserve]'); if (b) reserve(b); });
  var stream = new EventSource('/v1/capacity/events');
  stream.onopen = function(){ if (state) state.textContent = 'live · stream connected'; };
  stream.onmessage = function(event){ try { render(JSON.parse(event.data)); } catch (ignore) {} };
  stream.onerror = function(){ if (state) state.textContent = 'stream disconnected · reconnecting; last snapshot retained'; };

  function value(v){ return v === undefined || v === null ? 'unknown' : String(v); }
  function count(v){ return v === undefined || v === null ? 'unknown' : Math.round(Number(v)).toLocaleString(); }
  function panel(name, title, rows){
    var host=document.getElementById('efficiency-' + name + '-panel'); if (!host) return;
    var old=document.getElementById('efficiency-' + name), open=old && old.open;
    var focused=document.activeElement === (old && old.querySelector('summary'));
    var left=old ? old.scrollLeft : 0, top=old ? old.scrollTop : 0;
    var body=(rows || []).slice(0,20).map(function(r){
      var reasoning=r.reasoning_share_of_output_pct === undefined || r.reasoning_share_of_output_pct === null ? 'unknown' : r.reasoning_share_of_output_pct + '%';
      var id=r.anchor ? ' id="' + esc(r.anchor) + '"' : '';
      return '<tr' + id + '><td>' + esc(r.key) + '</td><td>' + count(r.uncached_input) + '</td><td>' + count(r.cached_input) + '</td><td>' + count(r.output) + '</td><td>' + reasoning + '</td><td>' + count(r.requests) + '</td><td>' + count(r.median_tokens_per_request) + '</td></tr>';
    }).join('') || '<tr><td colspan="7">no requests in range</td></tr>';
    host.innerHTML='<details id="efficiency-' + name + '" class="efficiency"' + (open ? ' open' : '') + '><summary>' + esc(title) + ' (last 7 days)</summary><table class="usage"><thead><tr><th>group</th><th>uncached in</th><th>cached in</th><th>output</th><th>reasoning / output</th><th>requests</th><th>median tokens/request</th></tr></thead><tbody>' + body + '</tbody></table></details>';
    var replacement=document.getElementById('efficiency-' + name);
    if (replacement){ replacement.scrollLeft=left; replacement.scrollTop=top; if (focused) replacement.querySelector('summary').focus(); }
  }
  function dailyModels(markup){
    var host=document.getElementById('daily-models-panel');
    if (!host || typeof markup !== 'string' || host.dataset.markup === markup) return;
    var focused=document.activeElement, focusId=null, states={};
    var x=window.scrollX, y=window.scrollY;
    host.querySelectorAll('details').forEach(function(el){
      states[el.id]={open:el.open,left:el.scrollLeft,top:el.scrollTop};
      if (focused === el.querySelector('summary')) focusId=el.id;
    });
    host.innerHTML=markup; host.dataset.markup=markup;
    host.querySelectorAll('details').forEach(function(el){
      var old=states[el.id]; if (!old) return;
      el.open=old.open; el.scrollLeft=old.left; el.scrollTop=old.top;
      if (el.id === focusId) el.querySelector('summary').focus({preventScroll:true});
    });
    window.scrollTo(x,y);
  }
  function efficiency(){
    fetch('/v1/usage', {cache:'no-store'}).then(function(r){ return r.ok ? r.json() : null; }).then(function(data){
      if (!data) return; dailyModels(data.daily_models_html); panel('session','By session',data.by_session); panel('model','By model × effort',data.by_model_effort);
    }).catch(function(){});
  }
  efficiency(); setInterval(efficiency, 30000);
})();
"""

LIVE_SCRIPT = """
(function(){
  function esc(v){ var d=document.createElement('div'); d.textContent=v===undefined||v===null?'unknown':String(v); return d.innerHTML; }
  function value(v){ return v === undefined || v === null ? 'unknown' : String(v); }
  function count(v){ return v === undefined || v === null ? 'unknown' : Math.round(Number(v)).toLocaleString(); }
  function panel(name, title, rows){
    var host=document.getElementById('efficiency-' + name + '-panel'); if (!host) return;
    var old=document.getElementById('efficiency-' + name), open=old && old.open;
    var focused=document.activeElement === (old && old.querySelector('summary'));
    var left=old ? old.scrollLeft : 0, top=old ? old.scrollTop : 0;
    var body=(rows || []).slice(0,20).map(function(r){
      var reasoning=r.reasoning_share_of_output_pct === undefined || r.reasoning_share_of_output_pct === null ? 'unknown' : r.reasoning_share_of_output_pct + '%';
      var id=r.anchor ? ' id="' + esc(r.anchor) + '"' : '';
      return '<tr' + id + '><td>' + esc(r.key) + '</td><td>' + count(r.uncached_input) + '</td><td>' + count(r.cached_input) + '</td><td>' + count(r.output) + '</td><td>' + reasoning + '</td><td>' + count(r.requests) + '</td><td>' + count(r.median_tokens_per_request) + '</td></tr>';
    }).join('') || '<tr><td colspan="7">no requests in range</td></tr>';
    host.innerHTML='<details id="efficiency-' + name + '" class="efficiency"' + (open ? ' open' : '') + '><summary>' + esc(title) + ' (last 7 days)</summary><table class="usage"><thead><tr><th>group</th><th>uncached in</th><th>cached in</th><th>output</th><th>reasoning / output</th><th>requests</th><th>median tokens/request</th></tr></thead><tbody>' + body + '</tbody></table></details>';
    var replacement=document.getElementById('efficiency-' + name);
    if (replacement){ replacement.scrollLeft=left; replacement.scrollTop=top; if (focused) replacement.querySelector('summary').focus(); }
  }
  function dailyModels(markup){
    var host=document.getElementById('daily-models-panel');
    if (!host || typeof markup !== 'string' || host.dataset.markup === markup) return;
    var focused=document.activeElement, focusId=null, states={};
    var x=window.scrollX, y=window.scrollY;
    host.querySelectorAll('details').forEach(function(el){
      states[el.id]={open:el.open,left:el.scrollLeft,top:el.scrollTop};
      if (focused === el.querySelector('summary')) focusId=el.id;
    });
    host.innerHTML=markup; host.dataset.markup=markup;
    host.querySelectorAll('details').forEach(function(el){
      var old=states[el.id]; if (!old) return;
      el.open=old.open; el.scrollLeft=old.left; el.scrollTop=old.top;
      if (el.id === focusId) el.querySelector('summary').focus({preventScroll:true});
    });
    window.scrollTo(x,y);
  }
  function efficiency(){
    fetch('/v1/usage', {cache:'no-store'}).then(function(r){ return r.ok ? r.json() : null; }).then(function(data){
      if (!data) return; dailyModels(data.daily_models_html); panel('session','By session',data.by_session); panel('model','By model × effort',data.by_model_effort);
    }).catch(function(){});
  }
  efficiency(); setInterval(efficiency, 30000);
})();
"""

LEGEND_HTML = (
    '<div class="legend"><span><i class="k-used"></i>% used</span><span><i class="k-pace"></i>linear pace</span>'
    '<span><i class="k-pace-reset"></i>even pace (with resets)</span>'
    '<span><i class="k-proj"></i>projection at current rate</span>'
    '<span><i class="k-proj-reset"></i>projection (with resets)</span>'
    '<span><i class="k-reset"></i>window reset</span>'
    '<span><i class="k-now"></i>now</span></div>'
)


def render_html(
    store: Store, now: datetime | None = None, days: int = 7,
    warnings: list[str] | None = None, refresh_s: int = 120,
    usage_db: Path | None = None, capacity: dict | None = None, live: bool = False,
) -> str:
    """The whole page. Weekly history renders as exact two-window-cycle charts."""
    now = now or now_utc()
    samples = canonical_samples(store.load(since=now - timedelta(days=max(days, 31))))
    latest = store.latest()
    burndowns = current(samples, latest, now)
    by_key: dict[str, list[Sample]] = {}
    for sample in samples:
        by_key.setdefault(sample.key, []).append(sample)

    # One grid cell per weekly chart, so five charts fill two rows of three.
    weekly = [bd for bd in burndowns if bd.window_min != 300]
    cells = [(history_group(bd), card_html(bd, by_key.get(bd.key, []), now, f"c{index}")) for index, bd in enumerate(weekly, 1)]
    chart_count = len(weekly)
    claude_cells = [index for index, (group, _) in enumerate(cells) if group == "claude"]
    if claude_cells and not any(bd.provider == "claude" and bd.window == FABLE_WINDOW for bd in weekly):
        # Fable weekly is a fixed slot beside the all-models chart: the grid keeps its shape
        # whether or not a Claude source has ever reported a Fable reading.
        cells.insert(claude_cells[-1] + 1, ("claude", unavailable_card_html(FABLE_WINDOW, FABLE_WEEKLY_UNAVAILABLE)))
    stamp = esc(to_local(now).strftime("%Y-%m-%d %H:%M"))
    sections = [
        f'<section class="provider"><h2>Historic readings as of {stamp} · {esc(PROVIDER_TITLES.get(group, group))}</h2>'
        '<div class="note">Model-grouped recorded usage only; these cards do not represent independent live allowances.</div>'
        f'<div class="cards">{card}</div></section>'
        for group, card in cells
    ]
    history = (
        f'<div class="history-grid">{"".join(sections)}</div>'
        if sections else '<p class="empty">No historical charts to show.</p>'
    )

    banner = ""
    if warnings:
        banner = '<div class="banner">' + "<br>".join(esc(w) for w in warnings) + "</div>"
    updated = to_local(now).strftime("%a %Y-%m-%d %H:%M")
    legend = LEGEND_HTML if chart_count else ""
    hover = f'<div id="chart-tooltip" role="status" aria-live="polite" hidden></div><script>{HOVER_SCRIPT}</script>' if chart_count else ""
    live_script = f'<script id="live-script">{LIVE_SCRIPT}</script>' if live else ""
    mode = "live dashboard" if live else f"static fallback generated {updated}"
    return "".join((
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "" if live else f"<meta http-equiv=\"refresh\" content=\"{int(refresh_s)}\">",
        f"<title>Quota Burndown</title><style>{CSS}</style></head><body>",
        f'<header class="top"><h1>Quota burndown</h1><div class="meta">{esc(mode)} · {len(samples)} historic samples in view</div></header>',
        "<main>", banner, legend, history,
        daily_models_section_html(usage_db, now) if usage_db is not None else "",
        usage_section_html(usage_db, now) if usage_db is not None else "",
        efficiency_section_html(usage_db, now) if usage_db is not None else "", "</main>",
        f"<footer>Above the dashed pace line = spending faster than a straight line to the reset (over pace); below it = under pace. Hover or focus a chart and use the arrow keys to read exact readings. quota-burndown {esc(__version__)}</footer>",
        hover, live_script, "</body></html>\n",
    ))


def write_html(store: Store, path: Path, **kwargs) -> Path:
    atomic_write_text(path, render_html(store, **kwargs))
    return path
