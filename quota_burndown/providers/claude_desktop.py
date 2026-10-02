"""Claude quota from the desktop app's own usage history.

The Claude desktop app polls the plan usage for its display and appends each reading to
plan-usage-history.json: `{"t": epoch ms, "org": id, "u": {"fh": five-hour %, "sd": seven-day %}}`,
one every 5 to 15 minutes while the app runs. That gives the 5h and 7d windows with no
credential and no network call, and it is the only Claude limit source. Two things the
file lacks are inferred:

  * the 5-hour window's reset: `fh` drops to 0 when a window ends, so a window starts at the
    first non-zero reading after a zero and resets five hours later;
  * the 7-day window's reset: the weekly reset runs on a fixed schedule, so the newest reset
    the Claude Code status line reported is kept as an anchor and stepped forward in whole
    weeks. Without an anchor, or once an observed drop to 0 contradicts its schedule, the
    reset is the last drop plus seven days. That fallback runs late by however long the app
    was not polling when the window reset (typically overnight). Until a drop or an anchor
    exists the window has no reset and shows as idle.

The file has no per-model figure, so per-model windows are not tracked.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from ..config import WINDOW_MINUTES, claude_desktop_history
from ..store import Sample
from ..util import atomic_write_text, from_epoch, parse_iso, read_json

PROVIDER = "claude"
SOURCE = "desktop"
FIVE_HOURS = timedelta(minutes=WINDOW_MINUTES["5h"])
SEVEN_DAYS = timedelta(minutes=WINDOW_MINUTES["7d"])
ANCHOR_NAME = "claude-weekly-reset-anchor.json"


def _reported_weekly_reset(home: Path) -> tuple[datetime, datetime] | None:
    """(resets_at, observed_at) of the newest status-line handoff that reported a 7-day reset."""
    from ..handoff import read
    best: tuple[datetime, datetime] | None = None
    for record in read(home):
        observed = parse_iso(record.get("observed_at"))
        week = record["quota"].get("seven_day")
        if observed is None or not isinstance(week, dict):
            continue
        raw = week.get("resets_at")
        reset = parse_iso(raw) if isinstance(raw, str) else from_epoch(raw)
        if reset is not None and (best is None or observed > best[1]):
            best = (reset, observed)
    return best


def weekly_anchor(home: Path) -> datetime | None:
    """The newest provider-reported 7-day reset. It is persisted because handoff files are
    pruned a day after the status line last ran, while the schedule outlives them."""
    path = home / ANCHOR_NAME
    stored = read_json(path, {})
    stored = stored if isinstance(stored, dict) else {}
    reset = parse_iso(stored.get("resets_at"))
    observed = parse_iso(stored.get("observed_at"))
    reported = _reported_weekly_reset(home)
    if reported and (reset is None or observed is None or reported[1] > observed):
        reset, observed = reported
        atomic_write_text(path, json.dumps({"resets_at": reset.isoformat(), "observed_at": observed.isoformat()}))
    return reset


def project_reset(anchor: datetime, ts: datetime) -> datetime:
    """The first scheduled weekly reset strictly after ts."""
    return anchor + (math.floor((ts - anchor) / SEVEN_DAYS) + 1) * SEVEN_DAYS


def read_history(path: Path) -> list[dict]:
    """Readings sorted by time; malformed entries dropped."""
    data = read_json(path, None)
    entries = data.get("samples") if isinstance(data, dict) else None
    out: list[dict] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("u"), dict):
            continue
        ts = from_epoch(entry.get("t"))
        if ts is None:
            continue
        usage = entry["u"]
        values = {}
        for key, window in (("fh", "5h"), ("sd", "7d")):
            raw = usage.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            values[window] = float(raw)
        if values:
            out.append({"t": int(entry.get("t")), "ts": ts, "org": str(entry.get("org") or ""), "values": values})
    out.sort(key=lambda item: item["t"])
    return out


def to_samples(history: list[dict], after_t: int = 0, anchor: datetime | None = None) -> list[Sample]:
    """Samples for readings newer than after_t. The whole history is walked so window starts
    are known even for the first new reading."""
    samples: list[Sample] = []
    session_start: datetime | None = None
    previous_5h: float | None = None
    last_7d_drop: datetime | None = None
    previous_7d: float | None = None
    previous_7d_ts: datetime | None = None
    # A drop with no scheduled reset between it and the reading before it means the schedule
    # moved or the window was reset off-schedule: trust the drop until one agrees again.
    on_schedule = True
    for item in history:
        ts = item["ts"]
        values = item["values"]
        if "5h" in values:
            used = values["5h"]
            if used <= 0:
                session_start = None
            elif previous_5h is None or previous_5h <= 0 or session_start is None:
                session_start = ts
            previous_5h = used
            resets = session_start + FIVE_HOURS if session_start is not None and ts < session_start + FIVE_HOURS else None
            if item["t"] > after_t:
                samples.append(Sample(ts, PROVIDER, "5h", used, resets, WINDOW_MINUTES["5h"], SOURCE))
        if "7d" in values:
            used = values["7d"]
            if previous_7d is not None and used < previous_7d and used <= 1.0:
                last_7d_drop = ts
                if anchor is not None and previous_7d_ts is not None:
                    on_schedule = project_reset(anchor, previous_7d_ts) <= ts
            previous_7d = used
            previous_7d_ts = ts
            if anchor is not None and on_schedule:
                resets = project_reset(anchor, ts)
            else:
                resets = last_7d_drop + SEVEN_DAYS if last_7d_drop is not None and ts < last_7d_drop + SEVEN_DAYS else None
            if item["t"] > after_t:
                samples.append(Sample(ts, PROVIDER, "7d", used, resets, WINDOW_MINUTES["7d"], SOURCE))
    return samples


def collect(state_path: Path, path: Path | None = None, home: Path | None = None) -> tuple[list[Sample], list[str], dict]:
    """New readings since the last run. Returns (samples, warnings, stats).

    When the weekly anchor changes, the newest reading is emitted again so its corrected reset
    replaces the one already published instead of waiting for the app's next reading."""
    path = path or claude_desktop_history()
    if not path.is_file():
        # Say where we looked: a scheduled task can run with a different environment (or a
        # virtualized AppData) than the shell the tool was set up from.
        return [], [], {"present": False, "path": str(path), "appdata_env": os.environ.get("APPDATA")}
    state = read_json(state_path, {})
    state = state if isinstance(state, dict) else {}
    after_t = int(state.get("last_t") or 0)
    anchor = weekly_anchor(home or state_path.parent)
    anchor_text = anchor.isoformat() if anchor else None
    try:
        history = read_history(path)
    except OSError as exc:
        return [], [f"claude desktop: cannot read {path.name}: {exc.__class__.__name__}"], {"present": True}
    if not history:
        return [], [f"claude desktop: no readings in {path.name}"], {"present": True, "readings": 0}
    newest = history[-1]["t"]
    anchor_changed = anchor_text != state.get("anchor")
    if anchor_changed:
        after_t = min(after_t, newest - 1)
    samples = to_samples(history, after_t, anchor)
    if newest != state.get("last_t") or anchor_changed:
        atomic_write_text(state_path, json.dumps({"last_t": newest, "anchor": anchor_text}))
    return samples, [], {"present": True, "readings": len(history), "new": len(samples)}
