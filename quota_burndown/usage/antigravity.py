"""Antigravity per-generation usage from ~/.gemini/antigravity.

Antigravity writes no labeled token counts anywhere on disk. What it does write:
  * brain/<conversation>/.system_generated/logs/transcript.jsonl: one JSON line per step
    with `type` (USER_INPUT, PLANNER_RESPONSE, GENERIC), `created_at`, and `content`; step 0
    records the model picker choice as text ("... Model Selection from None to Gemini 3.8
    Flash (High)");
  * conversations/<conversation>.db: SQLite; `gen_metadata` holds one protobuf blob per
    model call and `executor_metadata` one blob with the effort-qualified model string
    (e.g. gemini-3.8-flash-high).

Inside each gen_metadata blob, field path 1.9.10.1 grows with every call and path 1.9.10.4
is constant at the model's context window (256000 observed), which is how a prompt token
count and its ceiling would behave. No schema confirms that, so the values land in the
ledger's *inferred* columns, never in the exact totals. Output tokens are unknown.

Generations are matched to transcript PLANNER_RESPONSE steps by order (verified 1:1 on
real data) to get a timestamp; when the counts differ the database mtime is used instead.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import proto
from ..config import antigravity_home
from ..ledger import PROMPT, REQUEST, Event
from ..util import parse_iso

PROVIDER = "antigravity"
TOOL = "antigravity"
PATH_CONTEXT_TOKENS = (1, 9, 10, 1)
PATH_CONTEXT_WINDOW = (1, 9, 10, 4)
_EFFORTS = "low|medium|high|xhigh|max"
_VARIANT = re.compile(rf"^(?P<model>(?:gemini|claude|gpt)-[\w.-]+?)-(?P<effort>{_EFFORTS})$")
_BARE_MODEL = re.compile(r"^(?:gemini|claude|gpt)-[\w.-]+$")
_SETTING = re.compile(r"Model Selection[`'\"]*\s+from\s+.*?\s+to\s+(?P<model>[^()\n]+?)\s*\((?P<effort>\w+)\)")


def transcript_path(home: Path, conversation_id: str) -> Path:
    return home / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"


def discover(home: Path | None = None, since_days: float | None = 30) -> list[tuple[Path, int, float]]:
    """One entry per conversation database. Size and mtime fold in the transcript so a change
    to either file triggers a re-parse."""
    home = home or antigravity_home()
    folder = home / "conversations"
    if not folder.is_dir():
        return []
    cutoff = time.time() - since_days * 86400 if since_days is not None else None
    found: list[tuple[Path, int, float]] = []
    for path in folder.glob("*.db"):
        try:
            st = path.stat()
        except OSError:
            continue
        size, mtime = st.st_size, st.st_mtime
        try:
            ts = transcript_path(home, path.stem).stat()
            size += ts.st_size
            mtime = max(mtime, ts.st_mtime)
        except OSError:
            pass
        if cutoff is not None and mtime < cutoff:
            continue
        found.append((path, size, mtime))
    return found


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")


def model_from_setting(content: str) -> tuple[str, str]:
    match = _SETTING.search(content or "")
    if not match:
        return "", ""
    return slug(match.group("model")), match.group("effort").lower()


def split_variant(strings: list[str]) -> tuple[str, str]:
    for text in strings:
        match = _VARIANT.match(text.strip())
        if match:
            return match.group("model"), match.group("effort")
    return "", ""


def bare_model(strings: list[str]) -> str:
    for text in strings:
        candidate = text.strip()
        if _BARE_MODEL.match(candidate) and not _VARIANT.match(candidate):
            return candidate
    return ""


def read_transcript(path: Path) -> list[dict]:
    steps: list[dict] = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return steps
    with fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            ts = parse_iso(entry.get("created_at"))
            index = entry.get("step_index")
            if ts is None or not isinstance(index, int):
                continue
            steps.append({"step_index": index, "type": str(entry.get("type") or ""), "ts": ts, "content": str(entry.get("content") or "")})
    steps.sort(key=lambda s: s["step_index"])
    return steps


def read_db(path: Path) -> tuple[list[tuple[int, bytes]], list[bytes]]:
    """(gen_metadata rows ordered by idx, executor_metadata blobs). Read-only; raises
    sqlite3.OperationalError when Antigravity holds the database locked."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        gens: list[tuple[int, bytes]] = []
        if "gen_metadata" in tables:
            for idx, data in conn.execute("SELECT idx, data FROM gen_metadata ORDER BY idx"):
                if isinstance(data, (bytes, memoryview)):
                    gens.append((int(idx), bytes(data)))
        executors: list[bytes] = []
        if "executor_metadata" in tables:
            executors = [bytes(row[0]) for row in conn.execute("SELECT data FROM executor_metadata") if isinstance(row[0], (bytes, memoryview))]
        return gens, executors
    finally:
        conn.close()


def _walk_safely(blob: bytes) -> proto.Walked:
    try:
        return proto.walk(blob)
    except proto.WireError:
        walked = proto.Walked()
        text = proto.printable_text(blob)
        if text:
            walked.strings.append(text)
        return walked


def parse_file(path: Path, warnings: list[str] | None = None, home: Path | None = None) -> list[Event]:
    """Requests and prompts for one conversation. `path` is the conversation database."""
    warnings = warnings if warnings is not None else []
    home = home or path.parent.parent
    conversation = path.stem
    source = str(path)
    steps = read_transcript(transcript_path(home, conversation))
    setting_model, setting_effort = "", ""
    for step in steps:
        setting_model, setting_effort = model_from_setting(step["content"])
        if setting_model:
            break

    gens, executors = read_db(path)
    executor_strings: list[str] = []
    for blob in executors:
        executor_strings.extend(_walk_safely(blob).strings)
    executor_model, executor_effort = split_variant(executor_strings)
    effort = executor_effort or setting_effort
    fallback_model = executor_model or setting_model

    planner = [s for s in steps if s["type"] == "PLANNER_RESPONSE"]
    if gens and len(planner) != len(gens):
        try:
            db_ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            db_ts = datetime.now(timezone.utc)
        warnings.append(f"antigravity: {conversation[:8]} has {len(gens)} generations but {len(planner)} planner steps; using database mtime for timestamps")
        timestamps = [db_ts] * len(gens)
    else:
        timestamps = [s["ts"] for s in planner]

    events: list[Event] = []
    for (idx, blob), ts in zip(gens, timestamps):
        walked = _walk_safely(blob)
        events.append(Event(
            PROVIDER, TOOL, REQUEST, f"{conversation}:gen:{idx}", ts,
            session_id=conversation, thread="main",
            model=bare_model(walked.strings) or fallback_model, effort=effort,
            input_tokens_inferred=walked.first(PATH_CONTEXT_TOKENS),
            context_window_inferred=walked.first(PATH_CONTEXT_WINDOW),
            source_file=source,
        ))
    for step in steps:
        if step["type"] == "USER_INPUT":
            events.append(Event(
                PROVIDER, TOOL, PROMPT, f"{conversation}:step:{step['step_index']}", step["ts"],
                session_id=conversation, thread="main", model=fallback_model, effort=effort, source_file=source,
            ))
    return events
