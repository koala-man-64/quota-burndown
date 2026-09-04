"""Claude quota from the desktop app's own usage history.

The Claude desktop app polls the plan usage for its display and appends each reading to
plan-usage-history.json: `{"t": epoch ms, "org": id, "u": {"fh": five-hour %, "sd": seven-day %}}`,
one every 5 to 15 minutes while the app runs. That gives the 5h and 7d windows with no
credential and no network call. Two things the file lacks are inferred:

  * the 5-hour window's reset: `fh` drops to 0 when a window ends, so a window starts at the
    first non-zero reading after a zero and resets five hours later;
  * the 7-day window's reset: the last reset the usage endpoint reported is used while it is
    still ahead; otherwise the last time `sd` dropped to 0 plus seven days.

The per-model 7-day window is not in the file and stays with the endpoint sampler.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ..config import WINDOW_MINUTES, claude_desktop_history
from ..store import Sample
from ..util import atomic_write_text, from_epoch, read_json

PROVIDER = "claude"
SOURCE = "desktop"
FIVE_HOURS = timedelta(minutes=WINDOW_MINUTES["5h"])
SEVEN_DAYS = timedelta(minutes=WINDOW_MINUTES["7d"])


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


def to_samples(history: list[dict], known_7d_reset: datetime | None = None, after_t: int = 0) -> list[Sample]:
    """Samples for readings newer than after_t. The whole history is walked so window starts
    are known even for the first new reading."""
    samples: list[Sample] = []
    session_start: datetime | None = None
    previous_5h: float | None = None
    last_7d_drop: datetime | None = None
    previous_7d: float | None = None
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
            previous_7d = used
            if known_7d_reset is not None and known_7d_reset > ts:
                resets = known_7d_reset
            elif last_7d_drop is not None and ts < last_7d_drop + SEVEN_DAYS:
                resets = last_7d_drop + SEVEN_DAYS
            else:
                resets = None
            if item["t"] > after_t:
                samples.append(Sample(ts, PROVIDER, "7d", used, resets, WINDOW_MINUTES["7d"], SOURCE))
    return samples


def collect(state_path: Path, known_7d_reset: datetime | None = None, path: Path | None = None) -> tuple[list[Sample], list[str], dict]:
    """New readings since the last run. Returns (samples, warnings, stats)."""
    path = path or claude_desktop_history()
    if not path.is_file():
        # Say where we looked: a scheduled task can run with a different environment (or a
        # virtualized AppData) than the shell the tool was set up from.
        return [], [], {"present": False, "path": str(path), "appdata_env": os.environ.get("APPDATA")}
    state = read_json(state_path, {})
    after_t = int(state.get("last_t") or 0) if isinstance(state, dict) else 0
    try:
        history = read_history(path)
    except OSError as exc:
        return [], [f"claude desktop: cannot read {path.name}: {exc.__class__.__name__}"], {"present": True}
    if not history:
        return [], [f"claude desktop: no readings in {path.name}"], {"present": True, "readings": 0}
    samples = to_samples(history, known_7d_reset, after_t)
    newest = history[-1]["t"]
    if newest != after_t:
        atomic_write_text(state_path, json.dumps({"last_t": newest}))
    return samples, [], {"present": True, "readings": len(history), "new": len(samples)}
