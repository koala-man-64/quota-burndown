"""Text tables, status lines, CSV and JSON views over the usage ledger."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from . import ledger
from .ledger import PROVIDER_TITLES, Totals
from .util import iso, now_utc, to_local

DIMENSIONS = ("provider", "tool", "model", "effort", "model_effort", "day", "session", "thread")
DEFAULT_DIMENSIONS = ("provider", "model_effort", "day")
KEYS: dict[str, Callable[[sqlite3.Row], str]] = {
    "provider": lambda r: r["provider"],
    "tool": lambda r: r["tool"],
    "model": lambda r: f"{r['provider']} {r['model'] or '?'}",
    "effort": lambda r: f"{r['provider']} {r['effort'] or '?'}",
    "model_effort": lambda r: f"{r['provider']} {r['model'] or '?'} @ {r['effort'] or '?'}",
    "day": ledger.day_utc,
    "session": lambda r: f"{r['provider']} {r['session_id'][:8] or '?'}",
    "thread": lambda r: f"{r['provider']} {r['thread'] or '?'}",
}
TITLES = {
    "provider": "by provider", "tool": "by tool", "model": "by model", "effort": "by reasoning effort",
    "model_effort": "by model x effort", "day": "by day (UTC, most recent first)", "session": "by session", "thread": "by thread type",
}
PERIODS = ("today", "7d", "30d")


def fmt_n(value: int, raw: bool = False) -> str:
    if raw:
        return f"{value:,}"
    if value >= 1_000_000_000:
        return f"{value / 1e9:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1e6:.1f}M"
    if value >= 1_000:
        return f"{value / 1e3:.0f}K"
    return str(value)


def sorted_totals(totals: dict[str, Totals], dimension: str) -> list[tuple[str, Totals]]:
    if dimension == "day":
        return sorted(totals.items(), key=lambda kv: kv[0], reverse=True)
    return sorted(totals.items(), key=lambda kv: (-kv[1].total, -kv[1].inferred_input, -kv[1].prompts, kv[0]))


def table(title: str, totals: dict[str, Totals], dimension: str = "", raw: bool = False, top: int | None = 20) -> str:
    items = sorted_totals(totals, dimension)
    shown = items[:top] if top else items
    grand = sum(t.total for _, t in items) or 1
    width = min(max([len(k) for k, _ in shown] + [8]), 60)
    header = f"{'':<{width}}  {'prompts':>7} {'reqs':>6} {'input':>8} {'cache_r':>8} {'cache_w':>8} {'output':>8} {'total':>8} {'%tok':>5} {'~input':>8}"
    lines = [f"== {title} ==", header, "-" * len(header)]
    for key, t in shown:
        label = key if len(key) <= 60 else key[:57] + "..."
        inferred = f"~{fmt_n(t.inferred_input, raw)}" if t.inferred_requests else ""
        lines.append(
            f"{label:<{width}}  {t.prompts:>7} {t.requests:>6} {fmt_n(t.input, raw):>8} {fmt_n(t.cache_read, raw):>8} "
            f"{fmt_n(t.cache_write, raw):>8} {fmt_n(t.output, raw):>8} {fmt_n(t.total, raw):>8} {100 * t.total / grand:>4.0f}% {inferred:>8}"
        )
    if top and len(items) > top:
        rest = items[top:]
        lines.append(f"{'(+%d more)' % len(rest):<{width}}  {sum(t.prompts for _, t in rest):>7} {sum(t.requests for _, t in rest):>6} {'':>8} {'':>8} {'':>8} {'':>8} {fmt_n(sum(t.total for _, t in rest), raw):>8}")
    return "\n".join(lines)


def summary(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, dict[str, Totals]]:
    """Per provider: totals for the local calendar day, the rolling 7 days and the rolling 30 days."""
    now = now or now_utc()
    today = to_local(now).strftime("%Y-%m-%d")
    week = now - timedelta(days=7)
    out: dict[str, dict[str, Totals]] = {p: {period: Totals() for period in PERIODS} for p in ledger.PROVIDERS}
    for row in ledger.rows(conn, since=now - timedelta(days=30)):
        buckets = out.setdefault(row["provider"], {period: Totals() for period in PERIODS})
        buckets["30d"].add(row)
        if ledger.row_ts(row) >= week:
            buckets["7d"].add(row)
        if ledger.day_local(row) == today:
            buckets["today"].add(row)
    return out


def summary_dict(summary_: dict[str, dict[str, Totals]]) -> dict:
    return {provider: {period: totals.to_dict() for period, totals in periods.items()} for provider, periods in summary_.items()}


def _period_text(t: Totals, raw: bool = False) -> str:
    text = f"{t.prompts} prompts, {t.requests} requests, {fmt_n(t.total, raw)} tokens"
    if t.inferred_requests:
        text += f" (~{fmt_n(t.inferred_input, raw)} input, inferred)"
    return text


def status_lines(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    lines = []
    for provider, periods in summary(conn, now).items():
        if not any(t.prompts or t.requests for t in periods.values()):
            continue
        lines.append(f"{PROVIDER_TITLES.get(provider, provider)} usage today: {_period_text(periods['today'])}; 7d: {_period_text(periods['7d'])}")
    return lines


def _range(now: datetime, days: int | None, since: str | None, until: str | None) -> tuple[datetime | None, datetime | None]:
    """UTC bounds. `since`/`until` are UTC calendar dates (inclusive); `days` is a rolling window."""
    start = end = None
    if since:
        start = datetime.fromisoformat(since).replace(tzinfo=now.tzinfo)
    if until:
        end = datetime.fromisoformat(until).replace(tzinfo=now.tzinfo) + timedelta(days=1)
    if start is None and end is None and days:
        start = now - timedelta(days=days)
    return start, end


def render_text(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    days: int | None = 7,
    since: str | None = None,
    until: str | None = None,
    dimensions: Sequence[str] = DEFAULT_DIMENSIONS,
    raw: bool = False,
    top: int = 20,
) -> str:
    now = now or now_utc()
    start, end = _range(now, days, since, until)
    rows = ledger.rows(conn, since=start, until=end)
    if not rows:
        return "no usage recorded for that range; run: quota-burndown collect"
    grand = Totals()
    for row in rows:
        grand.add(row)
    span = f"{iso(start)[:10]} -> {iso(end - timedelta(seconds=1))[:10]}" if start and end else f"since {iso(start)[:10]}" if start else f"until {iso(end)[:10]}" if end else "all time"
    head = [
        f"Usage ledger - {span} (UTC): {grand.prompts:,} prompts, {grand.requests:,} requests, {len(rows):,} rows",
        f"  exact tokens {grand.total:,}: input {grand.input:,} | cache read {grand.cache_read:,} | cache write {grand.cache_write:,} | output {grand.output:,} (reasoning {grand.reasoning:,})",
    ]
    if grand.inferred_requests:
        head.append(f"  inferred (Antigravity, not in the totals above): {grand.inferred_requests:,} requests, ~{grand.inferred_input:,} input tokens")
    blocks = ["\n".join(head)]
    for dimension in dimensions:
        blocks.append(table(TITLES[dimension], ledger.rollup(rows, KEYS[dimension]), dimension, raw, top))
    return "\n\n".join(blocks)


def row_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["inferred"] = row["input_tokens_inferred"] is not None
    return data


def export_rows(conn: sqlite3.Connection, now: datetime | None = None, days: int | None = 7, since: str | None = None, until: str | None = None) -> list[dict]:
    now = now or now_utc()
    start, end = _range(now, days, since, until)
    return [row_dict(r) for r in ledger.rows(conn, since=start, until=end)]


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = ledger.COLUMNS + ["inferred"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def payload(conn: sqlite3.Connection, now: datetime | None = None, days: int = 7, recent: int = 50) -> dict:
    now = now or now_utc()
    rows = ledger.rows(conn, since=now - timedelta(days=days))
    return {
        "generated_at": iso(now),
        "days": days,
        "summary": summary_dict(summary(conn, now)),
        "by_model_effort": [
            {"key": key, **t.to_dict()} for key, t in sorted_totals(ledger.rollup(rows, KEYS["model_effort"]), "model_effort")
        ],
        "recent": [row_dict(r) for r in ledger.recent_requests(conn, recent)],
        "note": "Antigravity token counts are inferred from an unlabeled context counter and are kept out of exact totals; Google exposes no local limit signal.",
    }
