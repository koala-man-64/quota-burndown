"""Antigravity (Gemini) synthetic quota burndown provider.

Calculates percentage-of-allowance quota burn and resets from the recorded token activity
in the ledger (usage.sqlite) against a configurable weekly budget.

Each weekly cycle has:
  * a cycle start and end (resets_at) aligned to weekly boundaries (Monday 00:00 UTC);
  * an initial baseline sample (0% at cycle start);
  * an incremental sample per activity timestamp with cumulative percentage of weekly budget used.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import antigravity_weekly_token_budget
from ..store import Sample
from ..util import atomic_write_text, iso, now_utc, parse_iso, read_json

PROVIDER = "antigravity"
WINDOW = "7d:gemini"
WINDOW_MINUTES = 10080
SOURCE = "ledger"


def weekly_cycle_bounds(ts: datetime) -> tuple[datetime, datetime]:
    """Return (cycle_start, resets_at) for the weekly cycle containing ts (Monday 00:00 UTC)."""
    ts_utc = ts.astimezone(timezone.utc)
    day_start = ts_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    cycle_start = day_start - timedelta(days=ts_utc.weekday())
    resets_at = cycle_start + timedelta(days=7)
    return cycle_start, resets_at


def read_events(db_path: Path, since: datetime | None = None) -> list[dict]:
    """Read antigravity request events from usage.sqlite sorted by timestamp."""
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        sql = (
            "SELECT ts, input_tokens_inferred, model, effort "
            "FROM events WHERE provider = 'antigravity' AND kind = 'request'"
        )
        params: list[str] = []
        if since is not None:
            sql += " AND ts >= ?"
            params.append(iso(since))
        sql += " ORDER BY ts ASC"
        rows = conn.execute(sql, params).fetchall()
        events = []
        for r in rows:
            ts = parse_iso(r["ts"])
            if ts is not None:
                events.append({
                    "ts": ts,
                    "tokens": int(r["input_tokens_inferred"] or 0),
                    "model": str(r["model"] or ""),
                    "effort": str(r["effort"] or ""),
                })
        return events
    finally:
        conn.close()


DEFAULT_BUCKET_MINUTES = 30


def to_samples(
    events: list[dict],
    budget: int,
    after_ts: datetime | None = None,
    now: datetime | None = None,
    bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
) -> list[Sample]:
    """Convert event list into quota burndown samples.

    The entire event history is walked per weekly cycle so cumulative tokens
    and resets_at are properly tracked. Periodic downsampling groups dense
    events into time buckets (default 30 min) so charts reflect clean burndown curves.
    Only samples strictly newer than after_ts are returned.
    """
    if not events:
        return []
    now = now or now_utc()
    samples: list[Sample] = []
    bucket_seconds = max(60, bucket_minutes * 60)

    # Group events by weekly cycle_start
    cycles: dict[datetime, list[dict]] = {}
    for event in events:
        cycle_start, _ = weekly_cycle_bounds(event["ts"])
        cycles.setdefault(cycle_start, []).append(event)

    for cycle_start, cycle_events in sorted(cycles.items(), key=lambda x: x[0]):
        resets_at = cycle_start + timedelta(days=7)
        # Baseline sample at start of cycle
        if after_ts is None or cycle_start > after_ts:
            samples.append(Sample(cycle_start, PROVIDER, WINDOW, 0.0, resets_at, WINDOW_MINUTES, SOURCE))

        cumulative_tokens = 0
        cycle_samples: list[Sample] = []
        last_bucket_idx = None
        for ev in sorted(cycle_events, key=lambda x: x["ts"]):
            cumulative_tokens += ev["tokens"]
            used_pct = min(100.0, (cumulative_tokens / budget) * 100.0)
            bucket_idx = int((ev["ts"] - cycle_start).total_seconds() // bucket_seconds)
            sample = Sample(ev["ts"], PROVIDER, WINDOW, round(used_pct, 2), resets_at, WINDOW_MINUTES, SOURCE)
            if bucket_idx != last_bucket_idx:
                cycle_samples.append(sample)
                last_bucket_idx = bucket_idx
            else:
                cycle_samples[-1] = sample

        for s in cycle_samples:
            if after_ts is None or s.ts > after_ts:
                samples.append(s)

    return samples


def collect(
    state_path: Path,
    usage_db: Path,
    since_days: float | None = 14,
    full: bool = False,
    budget: int | None = None,
    now: datetime | None = None,
) -> tuple[list[Sample], list[str], dict]:
    """Scan usage database and produce quota burndown samples.

    Returns (samples, warnings, stats).
    """
    if not usage_db.is_file():
        return [], [], {"present": False, "path": str(usage_db), "events": 0, "new": 0}

    budget = budget or antigravity_weekly_token_budget()
    now = now or now_utc()
    cutoff = now - timedelta(days=since_days) if since_days is not None and not full else None

    state = {} if full else read_json(state_path, {})
    last_ts_str = state.get("last_ts") if isinstance(state, dict) else None
    after_ts = parse_iso(last_ts_str) if last_ts_str and not full else None

    try:
        events = read_events(usage_db, since=cutoff)
    except sqlite3.Error as exc:
        return [], [f"antigravity: cannot read {usage_db.name}: {exc}"], {"present": True}

    if not events:
        return [], [], {"present": True, "events": 0, "new": 0}

    samples = to_samples(events, budget, after_ts=after_ts, now=now)

    newest_ts = events[-1]["ts"]
    if after_ts is None or newest_ts > after_ts:
        atomic_write_text(state_path, json.dumps({"last_ts": iso(newest_ts), "budget": budget}))

    return samples, [], {"present": True, "events": len(events), "new": len(samples), "budget": budget}
