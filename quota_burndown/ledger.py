"""Per-request token usage ledger.

One SQLite table of events, two kinds of row: a `request` (one model call, with whatever
token counts the tool recorded) and a `prompt` (one human turn, no tokens). Rows are keyed
by a natural key from the source data so re-parsing a file is idempotent, and a scan_state
table remembers each source file's size and mtime so unchanged files are skipped.

Antigravity records no labeled token counts; its rows carry `input_tokens_inferred` (an
unlabeled context counter) and leave the exact columns NULL. Rollups keep that bucket
apart from the exact totals.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .util import iso, now_utc, parse_iso, to_local

PROVIDERS = ("claude", "codex", "antigravity")
PROVIDER_TITLES = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity"}
REQUEST = "request"
PROMPT = "prompt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  provider TEXT NOT NULL,
  tool TEXT NOT NULL,
  kind TEXT NOT NULL,
  event_key TEXT NOT NULL,
  ts TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  thread TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  effort TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  output_tokens INTEGER,
  reasoning_tokens INTEGER,
  total_tokens INTEGER,
  input_tokens_inferred INTEGER,
  context_window_inferred INTEGER,
  source_file TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (provider, kind, event_key)
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_provider_kind_ts ON events (provider, kind, ts);
CREATE TABLE IF NOT EXISTS scan_state (
  path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  scanned_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Event:
    provider: str
    tool: str
    kind: str
    event_key: str
    ts: datetime
    session_id: str = ""
    thread: str = ""
    model: str = ""
    effort: str = ""
    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    input_tokens_inferred: int | None = None
    context_window_inferred: int | None = None
    source_file: str = ""

    @property
    def inferred(self) -> bool:
        return self.input_tokens_inferred is not None

    def with_model(self, model: str, effort: str) -> "Event":
        return replace(self, model=model or self.model, effort=effort or self.effort)

    def to_row(self) -> tuple:
        return tuple(iso(self.ts) if name == "ts" else getattr(self, name) for name in COLUMNS)


COLUMNS = [f.name for f in fields(Event)]
_KEY = ("provider", "kind", "event_key")
_UPSERT = (
    f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)}) "
    f"ON CONFLICT({', '.join(_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c not in _KEY)
    # A fuller record (more output) always wins: Claude writes one line per content block with
    # the same message id and output only grows across them. On equal output the row is the
    # same call seen again (a rescan, or Codex history copied into a subagent rollout), so it
    # only changes when the numbers changed, when it fills a missing model or effort, or when a
    # root-thread attribution replaces a subagent copy.
    + " WHERE coalesce(excluded.output_tokens, 0) > coalesce(events.output_tokens, 0)"
    + " OR (coalesce(excluded.output_tokens, 0) = coalesce(events.output_tokens, 0) AND ("
    + "excluded.total_tokens IS NOT events.total_tokens"
    + " OR excluded.input_tokens_inferred IS NOT events.input_tokens_inferred"
    + " OR (events.model = '' AND excluded.model <> '')"
    + " OR (events.effort = '' AND excluded.effort <> '')"
    + " OR (events.thread = 'subagent' AND excluded.thread = 'root')))"
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


# -- write ----------------------------------------------------------------------------

def upsert(conn: sqlite3.Connection, events: Iterable[Event]) -> int:
    rows_ = [e.to_row() for e in events]
    if not rows_:
        return 0
    with conn:
        cur = conn.executemany(_UPSERT, rows_)
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows_)


def file_changed(conn: sqlite3.Connection, path: Path | str, size: int, mtime: float) -> bool:
    row = conn.execute("SELECT size, mtime FROM scan_state WHERE path = ?", (str(path),)).fetchone()
    return row is None or row["size"] != size or row["mtime"] != mtime


def mark_scanned(conn: sqlite3.Connection, path: Path | str, size: int, mtime: float, now: datetime | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO scan_state (path, size, mtime, scanned_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, scanned_at = excluded.scanned_at",
            (str(path), size, mtime, iso(now or now_utc())),
        )


def forget_scans(conn: sqlite3.Connection, contains: str | None = None) -> int:
    """Drop scan_state rows (all, or those whose path contains the text) so files re-parse."""
    with conn:
        if contains:
            cur = conn.execute("DELETE FROM scan_state WHERE instr(path, ?) > 0", (contains,))
        else:
            cur = conn.execute("DELETE FROM scan_state")
    return cur.rowcount


# -- read -----------------------------------------------------------------------------

def rows(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    until: datetime | None = None,
    provider: str | None = None,
    kind: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1 = 1"
    args: list = []
    if since is not None:
        sql += " AND ts >= ?"
        args.append(iso(since))
    if until is not None:
        sql += " AND ts < ?"
        args.append(iso(until))
    if provider:
        sql += " AND provider = ?"
        args.append(provider)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY ts, provider, event_key"
    return conn.execute(sql, args).fetchall()


def recent_requests(conn: sqlite3.Connection, limit: int = 25) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM events WHERE kind = ? ORDER BY ts DESC LIMIT ?", (REQUEST, limit)).fetchall()


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM events").fetchone()[0])


def row_ts(row: sqlite3.Row) -> datetime:
    return parse_iso(row["ts"]) or now_utc()


def day_utc(row: sqlite3.Row) -> str:
    return str(row["ts"])[:10]


def day_local(row: sqlite3.Row) -> str:
    return to_local(row_ts(row)).strftime("%Y-%m-%d")


# -- rollups --------------------------------------------------------------------------

@dataclass
class Totals:
    prompts: int = 0
    requests: int = 0
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0
    total: int = 0
    inferred_requests: int = 0
    inferred_input: int = 0

    def add(self, row: sqlite3.Row) -> None:
        if row["kind"] == PROMPT:
            self.prompts += 1
            return
        self.requests += 1
        if row["input_tokens_inferred"] is not None:
            self.inferred_requests += 1
            self.inferred_input += int(row["input_tokens_inferred"])
            return
        self.input += int(row["input_tokens"] or 0)
        self.cache_read += int(row["cache_read_tokens"] or 0)
        self.cache_write += int(row["cache_write_tokens"] or 0)
        self.output += int(row["output_tokens"] or 0)
        self.reasoning += int(row["reasoning_tokens"] or 0)
        self.total += int(row["total_tokens"] or 0)

    @property
    def has_exact(self) -> bool:
        return self.requests > self.inferred_requests

    def to_dict(self) -> dict:
        return {
            "prompts": self.prompts, "requests": self.requests, "input": self.input, "cache_read": self.cache_read,
            "cache_write": self.cache_write, "output": self.output, "reasoning": self.reasoning, "total": self.total,
            "inferred_requests": self.inferred_requests, "inferred_input": self.inferred_input,
        }


def rollup(items: Iterable[sqlite3.Row], key: Callable[[sqlite3.Row], str]) -> dict[str, Totals]:
    out: dict[str, Totals] = {}
    for row in items:
        out.setdefault(key(row), Totals()).add(row)
    return out
