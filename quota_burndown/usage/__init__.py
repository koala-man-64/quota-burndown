"""Per-request usage providers and the collector that feeds them into the ledger.

Each provider module exposes `discover(home, since_days)` -> [(path, size, mtime)] and
`parse_file(path, warnings)` -> [Event]. The collector re-parses a file only when its size
or mtime changed since the last scan, newest files first, and stops early when a time
budget runs out; files it did not reach stay unscanned and are picked up next run.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, Sequence

from .. import ledger

PROVIDERS = ("claude", "codex", "antigravity")


def provider_module(name: str):
    from . import antigravity, claude, codex  # local import keeps package import light and acyclic

    return {"claude": claude, "codex": codex, "antigravity": antigravity}[name]


def collect(
    conn: sqlite3.Connection,
    providers: Sequence[str] = PROVIDERS,
    since_days: float | None = 30,
    budget_s: float | None = None,
    homes: dict[str, Path] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict, list[str]]:
    """Parse changed source files into the ledger. Returns (stats per provider, warnings)."""
    deadline = time.monotonic() + budget_s if budget_s else None
    stats: dict[str, dict] = {}
    warnings: list[str] = []
    for name in providers:
        module = provider_module(name)
        files = module.discover((homes or {}).get(name), since_days)
        files.sort(key=lambda item: -item[2])
        parsed = written = deferred = 0
        for position, (path, size, mtime) in enumerate(files):
            if deadline is not None and time.monotonic() >= deadline:
                deferred = len(files) - position
                break
            if not ledger.file_changed(conn, path, size, mtime):
                continue
            try:
                written += ledger.upsert(conn, module.parse_file(path, warnings))
                ledger.mark_scanned(conn, path, size, mtime)
                parsed += 1
            except (OSError, ValueError, sqlite3.Error) as exc:
                warnings.append(f"{name}: {path.name}: {exc.__class__.__name__}: {exc}")
            if progress and parsed and parsed % 100 == 0:
                progress(f"{name}: parsed {parsed} files, {written} events so far")
        stats[name] = {"files_seen": len(files), "files_parsed": parsed, "events_written": written, "files_deferred": deferred}
        if deferred:
            warnings.append(f"{name}: time budget reached; {deferred} file(s) deferred to the next run")
    return stats, warnings
