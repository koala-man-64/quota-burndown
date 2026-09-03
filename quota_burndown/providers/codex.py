"""Codex CLI quota.

Every `token_count` event Codex writes to a rollout file carries a `rate_limits` block with
used_percent, window_minutes and resets_at for each active window. Scanning rollouts gives a
full history for free, with no network call and no credentials. Files are scanned
incrementally: a state file remembers the byte offset reached in each rollout.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ..config import codex_home, label_for_minutes
from ..store import Sample
from ..util import atomic_write_text, from_epoch, parse_iso, read_json

PROVIDER = "codex"
_MARKER = b'"rate_limits"'


def rollout_files(home: Path | None = None, since_days: float | None = 14) -> list[tuple[Path, int, float]]:
    """(path, size, mtime) for rollouts modified within since_days (None = all), oldest first."""
    home = home or codex_home()
    cutoff = time.time() - since_days * 86400 if since_days is not None else None
    found: list[tuple[Path, int, float]] = []
    for folder, pattern in ((home / "archived_sessions", "*.jsonl"), (home / "sessions", "**/*.jsonl")):
        if not folder.is_dir():
            continue
        for path in folder.glob(pattern):
            try:
                st = path.stat()
            except OSError:
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                continue
            found.append((path, st.st_size, st.st_mtime))
    found.sort(key=lambda item: item[2])
    return found


def samples_from_line(line: str, fallback_ts: datetime | None = None) -> list[Sample]:
    try:
        event = json.loads(line)
    except ValueError:
        return []
    payload = event.get("payload") if isinstance(event, dict) else None
    if not isinstance(payload, dict):
        return []
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return []
    ts = parse_iso(event.get("timestamp")) or fallback_ts
    if ts is None:
        return []
    out: list[Sample] = []
    for part in ("primary", "secondary"):
        block = limits.get(part)
        if not isinstance(block, dict):
            continue
        percent = block.get("used_percent")
        minutes = block.get("window_minutes")
        if percent is None or not minutes:
            continue
        try:
            minutes = int(minutes)
            percent = float(percent)
        except (TypeError, ValueError):
            continue
        resets_at = from_epoch(block.get("resets_at"))
        if resets_at is not None and resets_at < ts:
            continue  # a reading for a window that had already ended when it was written
        out.append(Sample(ts, PROVIDER, label_for_minutes(minutes), percent, resets_at, minutes, "rollout"))
    return out


def scan_file(path: Path, offset: int = 0) -> tuple[list[Sample], int]:
    """Read complete lines from offset; returns (samples, offset of the next unread byte)."""
    samples: list[Sample] = []
    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            start = fh.tell()
            raw = fh.readline()
            if not raw:
                return samples, start
            if not raw.endswith(b"\n"):  # partial line still being written
                return samples, start
            if _MARKER not in raw:
                continue
            samples.extend(samples_from_line(raw.decode("utf-8", "replace")))


def collect(state_path: Path, home: Path | None = None, since_days: float | None = 14, full: bool = False) -> tuple[list[Sample], list[str], dict]:
    """Scan new rollout bytes. Returns (samples, warnings, stats)."""
    state = {} if full else read_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    files = rollout_files(home, None if full else since_days)
    samples: list[Sample] = []
    warnings: list[str] = []
    scanned = 0
    for path, size, mtime in files:
        key = str(path)
        prev = state.get(key) if isinstance(state.get(key), dict) else {}
        if prev.get("size") == size and prev.get("mtime") == mtime:
            continue
        offset = int(prev.get("offset") or 0)
        if size < offset:
            offset = 0
        try:
            new, end = scan_file(path, offset)
        except OSError as exc:
            warnings.append(f"codex: cannot read {path.name}: {exc.__class__.__name__}")
            continue
        samples.extend(new)
        scanned += 1
        state[key] = {"offset": end, "size": size, "mtime": mtime}
    atomic_write_text(state_path, json.dumps(state))
    return samples, warnings, {"files_seen": len(files), "files_scanned": scanned}
