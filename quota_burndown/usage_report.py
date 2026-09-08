"""Text tables, status lines, CSV and JSON views over the usage ledger."""
from __future__ import annotations

import csv
import hashlib
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


def _efficiency_key(row: sqlite3.Row, dimension: str) -> str:
    """A display-only drilldown key.  Keep the original identifiers in the ledger.

    Session IDs are intentionally not shortened here: callers decide how much of an
    already-local identifier to present, and HTML rendering escapes it.
    """
    if dimension == "session":
        return f"{row['provider']} {row['session_id'] or '?'}"
    return f"{row['provider']} {row['model'] or '?'} @ {row['effort'] or '?'}"


def _anchor(key: str) -> str:
    return "efficiency-session-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def efficiency_rows(rows: Sequence[sqlite3.Row], dimension: str) -> list[dict]:
    """Normalize request accounting for dashboard drilldowns without changing ledger totals.

    Codex's recorded input includes cache tokens.  The presentation therefore subtracts
    cache read/write once for its uncached-input column.  Providers with inferred input
    retain unknown exact token fields instead of turning an unknown into zero.
    """
    groups: dict[str, dict] = {}
    for row in rows:
        if row["kind"] != ledger.REQUEST:
            continue
        key = _efficiency_key(row, dimension)
        item = groups.setdefault(key, {
            "key": key, "anchor": _anchor(key) if dimension == "session" else None,
            "provider": row["provider"], "requests": 0, "uncached_input": 0,
            "cached_input": 0, "output": 0, "reasoning": 0,
            "uncached_input_known": True, "cached_input_known": True,
            "output_known": True, "reasoning_known": True, "total_known": True,
            "exact_requests": 0, "inferred_requests": 0,
            "tokens_per_request": [],
        })
        item["requests"] += 1
        if row["input_tokens_inferred"] is not None:
            item["inferred_requests"] += 1
            item["uncached_input_known"] = item["cached_input_known"] = False
            item["output_known"] = item["reasoning_known"] = item["total_known"] = False
            continue
        input_value, read_value, write_value = row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]
        output_value, reasoning_value, total_value = row["output_tokens"], row["reasoning_tokens"], row["total_tokens"]
        if input_value is None:
            item["uncached_input_known"] = False
        # Some adapters omit an absent cache-write field; a reported cache-read is
        # still enough to present cached input.  When neither exists, it is unknown.
        if read_value is None and write_value is None:
            item["cached_input_known"] = False
        if output_value is None:
            item["output_known"] = False
        if reasoning_value is None:
            item["reasoning_known"] = False
        if total_value is None:
            item["total_known"] = False
        input_tokens = int(input_value or 0)
        cached = int(read_value or 0) + int(write_value or 0)
        # Codex's input field is inclusive.  Other providers' fields are kept as
        # reported because their cache values are separately accounted by the ledger.
        uncached = max(0, input_tokens - cached) if row["provider"] == "codex" else input_tokens
        output = int(output_value or 0)
        reasoning = int(reasoning_value or 0)
        item["uncached_input"] += uncached
        item["cached_input"] += cached
        item["output"] += output
        item["reasoning"] += reasoning
        item["exact_requests"] += 1
        if total_value is not None:
            item["tokens_per_request"].append(int(total_value))

    out = []
    for item in groups.values():
        values = sorted(item.pop("tokens_per_request"))
        median = None
        if values:
            mid = len(values) // 2
            median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        for field in ("uncached_input", "cached_input", "output", "reasoning"):
            if not item.pop(field + "_known"):
                item[field] = None
        total_known = item.pop("total_known")
        item["median_tokens_per_request"] = median
        # Reasoning is a subset of output, so use output as the unambiguous denominator.
        item["reasoning_share_of_output_pct"] = (
            round(100 * item["reasoning"] / item["output"], 1)
            if item["reasoning"] is not None and item["output"] not in (None, 0) else None
        )
        if not total_known:
            item["median_tokens_per_request"] = None
        item["exact"] = item["inferred_requests"] == 0
        out.append(item)
    return sorted(out, key=lambda item: (-item["requests"], item["key"]))


def daily_model_payload(conn: sqlite3.Connection, now: datetime) -> dict:
    """Recorded totals for seven local calendar dates, without estimating missing tokens."""
    today = to_local(now).date()
    dates = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    providers = {provider: {} for provider in ledger.PROVIDERS}
    # Read a bounded superset, then bucket each timestamp in its own local offset.
    # Subtracting days from today's fixed UTC offset would misclassify DST boundaries.
    for row in ledger.rows(conn, since=now - timedelta(days=8), kind=ledger.REQUEST):
        ts = ledger.row_ts(row)
        day = to_local(ts).date().isoformat()
        if ts > now or day not in dates:
            continue
        models = providers.setdefault(row["provider"], {})
        model = (row["model"] or "").strip()
        item = models.setdefault(model, {
            "model": model, "label": model or "Unknown model",
            "color": f'hsl({int(hashlib.sha256(model.encode()).hexdigest()[:8], 16) % 36000 / 100:.2f}, 65%, 62%)',
            "tokens": [0] * 7, "recorded_requests": 0, "excluded_requests": 0,
        })
        if row["total_tokens"] is None or row["input_tokens_inferred"] is not None:
            item["excluded_requests"] += 1
        else:
            item["tokens"][dates.index(day)] += int(row["total_tokens"])
            item["recorded_requests"] += 1
    groups = []
    for provider, models in sorted(providers.items()):
        series = sorted(models.values(), key=lambda item: item["model"])
        for item in series:
            item["total_tokens"] = sum(item["tokens"])
        groups.append({
            "provider": provider, "label": PROVIDER_TITLES.get(provider, provider),
            "models": series,
            "daily_tokens": [sum(item["tokens"][i] for item in series) for i in range(7)],
            "total_tokens": sum(item["total_tokens"] for item in series),
            "recorded_requests": sum(item["recorded_requests"] for item in series),
            "excluded_requests": sum(item["excluded_requests"] for item in series),
        })
    return {"dates": dates, "calendar": "local", "through": iso(now), "providers": groups}


def efficiency_payload(conn: sqlite3.Connection, now: datetime | None = None, days: int = 7) -> dict:
    """Dashboard-ready, local-ledger efficiency drilldowns for the rolling range."""
    now = now or now_utc()
    rows = ledger.rows(conn, since=now - timedelta(days=days), kind=ledger.REQUEST)
    sessions = efficiency_rows(rows, "session")
    insights = []
    for field, label in (("uncached_input", "highest uncached input"), ("median_tokens_per_request", "largest median tokens per request"), ("reasoning", "largest reasoning-token contribution")):
        candidates = [row for row in sessions if isinstance(row.get(field), (int, float))]
        if candidates:
            chosen = max(candidates, key=lambda row: row[field])
            insights.append({"label": label, "value": chosen[field], "session_anchor": chosen["anchor"]})
    return {
        "generated_at": iso(now), "days": days,
        "by_session": sessions,
        "by_model_effort": efficiency_rows(rows, "model_effort"),
        "daily_models": daily_model_payload(conn, now),
        "insights": insights,
        "note": "Uncached input is normalized for presentation only. Antigravity exact token fields remain unknown.",
    }
