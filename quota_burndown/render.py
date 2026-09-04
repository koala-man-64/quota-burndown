"""Self-contained HTML page with inline SVG burndown charts. No JavaScript, no CDN."""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, ledger, usage_report
from .ledger import Totals
from .model import Burndown, current, group_instances
from .store import Sample, Store
from .util import atomic_write_text, fmt_local, fmt_minutes, now_utc, to_local

STALE_MIN = 20
PROVIDER_TITLES = {"claude": "Claude", "codex": "Codex"}
WINDOW_TITLES = {"5h": "5-hour session", "7d": "7-day (all models)"}
STATUS_TEXT = {
    "over": "over pace",
    "under": "under pace",
    "on-pace": "on pace",
    "early": "too early to call",
    "exhausted": "budget exhausted",
    "expired": "window ended",
    "idle": "no active window",
}


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def window_title(window: str) -> str:
    if window in WINDOW_TITLES:
        return WINDOW_TITLES[window]
    if window.startswith("7d:"):
        return f"7-day ({window[3:].replace('-', ' ').title()})"
    return window


def _tick_plan(total_min: int) -> tuple[int, str]:
    if total_min <= 360:
        return 60, "%H:%M"
    if total_min <= 1440:
        return 240, "%H:%M"
    return 1440, "%a"


def burndown_svg(bd: Burndown, width: int = 640, height: int = 320) -> str:
    if bd.start is None or bd.resets_at is None:
        return ""
    pl, pr, pt, pb = 44, 14, 12, 26
    iw, ih = width - pl - pr, height - pt - pb
    total = float(bd.window_min)

    def x_of(minutes: float) -> float:
        return pl + iw * min(max(minutes, 0.0), total) / total

    def y_of(remaining: float) -> float:
        return pt + ih * (1 - min(max(remaining, 0.0), 100.0) / 100)

    parts: list[str] = []
    for value in (0, 25, 50, 75, 100):
        y = y_of(value)
        parts.append(f'<line class="grid" x1="{pl}" y1="{y:.1f}" x2="{pl + iw}" y2="{y:.1f}"/>')
        parts.append(f'<text class="lbl" x="{pl - 6}" y="{y + 4:.1f}" text-anchor="end">{value}%</text>')
    step, fmt = _tick_plan(bd.window_min)
    minute = 0.0
    while minute <= total + 1e-9:
        x = x_of(minute)
        stamp = bd.start + timedelta(minutes=minute)
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{pt}" x2="{x:.1f}" y2="{pt + ih}"/>')
        parts.append(f'<text class="lbl" x="{x:.1f}" y="{height - 8}" text-anchor="middle">{esc(fmt_local(stamp, fmt))}</text>')
        minute += step

    parts.append(f'<line class="ideal" x1="{x_of(0):.1f}" y1="{y_of(100):.1f}" x2="{x_of(total):.1f}" y2="{y_of(0):.1f}"/>')

    points = [(x_of(0), y_of(100))]
    for sample in bd.samples:
        elapsed = (sample.ts - bd.start).total_seconds() / 60
        points.append((x_of(elapsed), y_of(100 - sample.used)))
    if bd.status != "expired":
        points.append((x_of(bd.elapsed_min), y_of(bd.remaining_pct)))
    parts.append('<polyline class="actual" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in points) + '"/>')

    if bd.status in ("over", "under", "on-pace") and bd.rate_per_hour > 0:
        x0, y0 = x_of(bd.elapsed_min), y_of(bd.remaining_pct)
        if bd.exhausts_before_reset and bd.exhaust_at is not None:
            x1, y1 = x_of((bd.exhaust_at - bd.start).total_seconds() / 60), y_of(0)
        else:
            x1, y1 = x_of(total), y_of(100 - bd.projected_end)
        parts.append(f'<line class="proj" x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}"/>')

    xn = x_of(bd.elapsed_min)
    parts.append(f'<line class="now" x1="{xn:.1f}" y1="{pt}" x2="{xn:.1f}" y2="{pt + ih}"/>')
    parts.append(f'<circle class="dot" cx="{xn:.1f}" cy="{y_of(bd.remaining_pct):.1f}" r="3.5"/>')
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Remaining budget versus a linear burn">'
        + "".join(parts)
        + "</svg>"
    )


def history_svg(samples: list[Sample], now: datetime, days: int = 7, width: int = 640, height: int = 220) -> str:
    if not samples:
        return ""
    pl, pr, pt, pb = 44, 14, 10, 24
    iw, ih = width - pl - pr, height - pt - pb
    t0 = now - timedelta(days=days)
    span = (now - t0).total_seconds()

    def x_of(stamp: datetime) -> float:
        return pl + iw * min(max((stamp - t0).total_seconds(), 0.0), span) / span

    def y_of(remaining: float) -> float:
        return pt + ih * (1 - min(max(remaining, 0.0), 100.0) / 100)

    parts: list[str] = []
    for value in (0, 50, 100):
        y = y_of(value)
        parts.append(f'<line class="grid" x1="{pl}" y1="{y:.1f}" x2="{pl + iw}" y2="{y:.1f}"/>')
        parts.append(f'<text class="lbl" x="{pl - 6}" y="{y + 4:.1f}" text-anchor="end">{value}%</text>')
    local_now = to_local(now)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    while midnight > to_local(t0):
        x = x_of(midnight)
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{pt}" x2="{x:.1f}" y2="{pt + ih}"/>')
        parts.append(f'<text class="lbl" x="{x:.1f}" y="{height - 8}" text-anchor="middle">{esc(midnight.strftime("%a %d"))}</text>')
        midnight -= timedelta(days=1)

    instances = []
    for group in group_instances(samples).values():
        instances.extend(group)
    instances.sort(key=lambda inst: inst.resets_at)
    for inst in instances:
        items = [s for s in inst.samples if s.ts >= t0]
        earlier = [s for s in inst.samples if s.ts < t0]
        if earlier:
            items.insert(0, earlier[-1])
        if not items:
            continue
        window_end = min(inst.resets_at, now)
        if inst.start >= t0 and inst.resets_at <= now:
            parts.append(f'<line class="ideal faint" x1="{x_of(inst.start):.1f}" y1="{y_of(100):.1f}" x2="{x_of(inst.resets_at):.1f}" y2="{y_of(0):.1f}"/>')
        if t0 <= inst.resets_at <= now:
            parts.append(f'<line class="reset" x1="{x_of(inst.resets_at):.1f}" y1="{pt}" x2="{x_of(inst.resets_at):.1f}" y2="{pt + ih}"/>')
        pts = [f"{x_of(s.ts):.1f},{y_of(100 - s.used):.1f}" for s in items]
        if items[-1].ts < window_end:  # hold the last reading flat until the window ends or now
            pts.append(f"{x_of(window_end):.1f},{y_of(100 - items[-1].used):.1f}")
        parts.append(f'<polyline class="actual" points="{" ".join(pts)}"/>')
    return (
        f'<svg class="chart small" viewBox="0 0 {width} {height}" role="img" aria-label="Remaining budget over the last {days} days">'
        + "".join(parts)
        + "</svg>"
    )


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


def card_html(bd: Burndown, history: list[Sample], now: datetime, days: int) -> str:
    stale = bd.age_min is not None and bd.age_min > STALE_MIN
    classes = f"card status-{bd.status}" + (" stale" if stale else "")
    proj_value, proj_label = projection_text(bd)
    if bd.status == "idle":
        stats = (
            f'<div><b>{bd.used:.0f}%</b><span>used</span></div>'
            f'<div><b>—</b><span>pace</span></div>'
            f'<div><b>—</b><span>time left</span></div>'
            f'<div><b>{esc(proj_value)}</b><span>{esc(proj_label)}</span></div>'
        )
    else:
        resets = fmt_local(bd.resets_at) if bd.resets_at else "?"
        stats = (
            f'<div><b>{bd.used:.0f}%</b><span>used · {bd.remaining_pct:.0f}% left</span></div>'
            f'<div><b>{bd.pace:.0f}%</b><span>linear pace</span></div>'
            f'<div><b>{esc(fmt_minutes(bd.remaining_min))}</b><span>left · resets {esc(resets)}</span></div>'
            f'<div><b>{esc(proj_value)}</b><span>{esc(proj_label)}</span></div>'
        )
    age = f"{bd.age_min:.0f} min ago" if bd.age_min is not None else "never"
    foot = f"last sample {esc(age)} via {esc(bd.source or '?')} · {len(bd.samples)} samples this window"
    if stale:
        foot += " · <b>stale</b>: collector has not run recently"
    return (
        f'<article class="{classes}">'
        f'<header><h3>{esc(window_title(bd.window))}</h3><span class="badge">{esc(badge_text(bd))}</span></header>'
        f'<div class="stats">{stats}</div>'
        f"{burndown_svg(bd)}"
        f'<h4>Last {days} days</h4>{history_svg(history, now, days)}'
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


CSS = """
:root{--bg:#f6f7f9;--card:#ffffff;--fg:#1c1e21;--muted:#6b7280;--line:#e5e7eb;--grid:#eef0f3;--ideal:#9ca3af;--actual:#2563eb;--over:#dc2626;--under:#16a34a;--warn:#d97706;--now:#111827}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8eb;--muted:#9aa3ad;--line:#2a2f3a;--grid:#232834;--ideal:#6b7280;--actual:#60a5fa;--over:#f87171;--under:#4ade80;--warn:#fbbf24;--now:#e6e8eb}}
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
svg.chart{width:100%;height:auto;display:block}
.grid{stroke:var(--grid);stroke-width:1}
.lbl{fill:var(--muted);font-size:10px}
.ideal{stroke:var(--ideal);stroke-width:1.5;stroke-dasharray:5 4}
.ideal.faint{opacity:.5;stroke-width:1}
.actual{fill:none;stroke:var(--actual);stroke-width:2.2;stroke-linejoin:round}
.status-over .actual{stroke:var(--over)}
.status-under .actual{stroke:var(--under)}
.proj{stroke:var(--now);stroke-width:1.5;stroke-dasharray:2 4;opacity:.8}
.now{stroke:var(--now);stroke-width:1;opacity:.5}
.reset{stroke:var(--warn);stroke-width:1;stroke-dasharray:2 3}
.dot{fill:var(--now)}
.banner{background:var(--card);border:1px solid var(--warn);border-radius:8px;padding:8px 12px;margin:8px 0;font-size:13px}
.legend{display:flex;gap:18px;font-size:12px;color:var(--muted);margin:4px 0 0;flex-wrap:wrap}
.legend i{display:inline-block;width:22px;border-top:2px dashed var(--ideal);vertical-align:middle;margin-right:6px}
.legend i.a{border-top:2px solid var(--actual)}
.legend i.p{border-top:2px dotted var(--now)}
.legend i.r{border-top:2px dashed var(--warn)}
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


def render_html(store: Store, now: datetime | None = None, days: int = 7, warnings: list[str] | None = None, refresh_s: int = 120, usage_db: Path | None = None) -> str:
    now = now or now_utc()
    samples = store.load(since=now - timedelta(days=max(days, 8)))
    latest = store.latest()
    burndowns = current(samples, latest, now)
    by_key: dict[str, list[Sample]] = {}
    for sample in samples:
        by_key.setdefault(sample.key, []).append(sample)

    sections: list[str] = []
    providers: dict[str, list[Burndown]] = {}
    for bd in burndowns:
        providers.setdefault(bd.provider, []).append(bd)
    for provider, items in providers.items():
        cards = "".join(card_html(bd, by_key.get(bd.key, []), now, days) for bd in items)
        sections.append(f'<section class="provider"><h2>{esc(PROVIDER_TITLES.get(provider, provider))}</h2><div class="cards">{cards}</div></section>')
    if not sections:
        sections.append('<p class="empty">No samples yet. Run <code>quota-burndown collect</code>.</p>')

    banner = ""
    if warnings:
        banner = '<div class="banner">' + "<br>".join(esc(w) for w in warnings) + "</div>"
    updated = to_local(now).strftime("%a %Y-%m-%d %H:%M")
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<meta http-equiv=\"refresh\" content=\"{int(refresh_s)}\">"
        f"<title>Quota Burndown</title><style>{CSS}</style></head><body>"
        f'<header class="top"><h1>Quota burndown</h1><div class="meta">updated {esc(updated)} · page reloads every {int(refresh_s) // 60} min · {len(samples)} samples in view</div></header>'
        "<main>"
        f"{banner}"
        '<div class="legend"><span><i></i>linear pace (ideal burn)</span><span><i class="a"></i>remaining budget</span><span><i class="p"></i>projection at current rate</span><span><i class="r"></i>window reset</span></div>'
        + "".join(sections)
        + (usage_section_html(usage_db, now) if usage_db is not None else "")
        + "</main>"
        f"<footer>Above the dashed line = under pace (budget to spare). Below it = over pace. quota-burndown {esc(__version__)}</footer>"
        "</body></html>\n"
    )


def write_html(store: Store, path: Path, **kwargs) -> Path:
    atomic_write_text(path, render_html(store, **kwargs))
    return path
