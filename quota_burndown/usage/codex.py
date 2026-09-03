"""Codex per-call usage from the rollout files under ~/.codex.

Every `token_count` event carries `total_token_usage` (cumulative for the thread) and
`last_token_usage` (the call that just finished). Consecutive cumulative snapshots differ by
exactly the last-call vector, so one ledger request is written per snapshot using the
cumulative delta, with the last-call vector as the fallback when a counter resets or a
subagent rollout starts from an inherited parent baseline. The accounting mirrors the
standalone codex_token_usage_audit.py script and its tests.

Model and effort come from the `turn_context` record that precedes each turn. Human prompts
are `event_msg` user_message records when the client writes them (codex exec), otherwise
`response_item` user messages minus the wrappers Codex Desktop injects as role=user text.

A subagent rollout opens with a copy of its parent's history: the parent's prompts,
`task_started` markers and `token_count` snapshots, verbatim. Requests are therefore keyed
by turn id (from `task_started` / `turn_context`) plus the cumulative counter, and prompts
by timestamp plus a hash of their text, so a copy lands on the same ledger row as the
original instead of counting twice. Rollouts without turn ids fall back to (thread, ordinal).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from ..config import codex_home
from ..ledger import PROMPT, REQUEST, Event
from ..util import iso, parse_iso

PROVIDER = "codex"
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
# Text Codex Desktop injects into the transcript as role=user without a human typing it.
WRAPPER_PREFIXES = (
    "<hook_prompt",
    "<recommended_plugins",
    "<environment_context",
    "<user_instructions",
    "<permissions",
    "# Files mentioned by the user:",
)
_TAG_START = re.compile(r"<[A-Za-z_][\w.-]*(?:[\s>/]|$)")


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    def delta_from(self, previous: "Usage") -> tuple["Usage", bool]:
        current = asdict(self)
        prior = asdict(previous)
        if any(current[name] < prior[name] for name in TOKEN_FIELDS):
            return self, True
        delta = {name: current[name] - prior[name] for name in TOKEN_FIELDS}
        delta["total_tokens"] = delta["input_tokens"] + delta["output_tokens"]
        return Usage(**delta), False


def usage_vector(value: Any) -> Usage | None:
    """A validated token vector, or None when the block is missing or inconsistent."""
    if not isinstance(value, Mapping):
        return None
    parsed: dict[str, int] = {}
    for name in TOKEN_FIELDS:
        raw = value.get(name, 0)
        if isinstance(raw, bool):
            return None
        try:
            parsed[name] = int(raw or 0)
        except (TypeError, ValueError):
            return None
        if parsed[name] < 0:
            return None
    if not parsed["total_tokens"]:
        parsed["total_tokens"] = parsed["input_tokens"] + parsed["output_tokens"]
    usage = Usage(**parsed)
    if usage.total_tokens != usage.input_tokens + usage.output_tokens:
        return None
    if usage.cached_input_tokens > usage.input_tokens or usage.cache_write_input_tokens > usage.input_tokens:
        return None
    if usage.reasoning_output_tokens > usage.output_tokens:
        return None
    return usage


def _at_most(left: Usage, right: Usage) -> bool:
    return all(getattr(left, name) <= getattr(right, name) for name in TOKEN_FIELDS)


def usage_increment(current: Usage, last: Usage | None, previous: Usage | None) -> Usage:
    """The new usage one cumulative snapshot represents.

    Codex can repeat a snapshot, reset a counter, or seed a subagent rollout with the parent's
    baseline. The first and any rebased snapshot use the validated last-call vector; monotone
    snapshots use the exact cumulative difference."""
    valid_last = last is not None and _at_most(last, current)
    if previous is None:
        return last if valid_last else Usage()
    if current == previous:
        return Usage()
    if _at_most(previous, current):
        delta, reset = current.delta_from(previous)
        if not reset and usage_vector(asdict(delta)) is not None:
            return delta
        return last if valid_last else Usage()
    return last if valid_last else Usage()


def _subagent_spawn(source: Any) -> Mapping[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    subagent = source.get("subagent")
    if not isinstance(subagent, Mapping):
        return {}
    spawn = subagent.get("thread_spawn")
    return spawn if isinstance(spawn, Mapping) else {}


def session_id_from_filename(path: Path) -> str:
    parts = path.stem.rsplit("-", 5)
    return "-".join(parts[-5:]) if len(parts) == 6 else path.stem


def input_text(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    texts = [str(block.get("text") or "") for block in content if isinstance(block, Mapping) and block.get("type") == "input_text"]
    return "\n".join(texts) if texts else None


def is_wrapper(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(WRAPPER_PREFIXES) or bool(_TAG_START.match(stripped))


def prompt_key(ts, text: str) -> str:
    digest = hashlib.sha1(text.strip().encode("utf-8", "replace")).hexdigest()[:12]
    return f"{iso(ts)}:{digest}"


def request_key(thread_id: str, turn_id: str, ordinal_key: str, cumulative: Usage) -> str:
    if turn_id:
        return f"{turn_id}:{cumulative.total_tokens}:{cumulative.output_tokens}"
    return f"{thread_id}:{ordinal_key}"


def home_of(path: Path) -> Path:
    """The Codex data root a rollout belongs to (its sessions/ or archived_sessions/ parent)."""
    for parent in path.parents:
        if parent.name in ("sessions", "archived_sessions"):
            return parent.parent
    return codex_home()


_state_cache: dict[tuple[str, float], dict[str, tuple[str, str]]] = {}


def state_models(home: Path) -> dict[str, tuple[str, str]]:
    """thread id -> (model, reasoning_effort) from Codex's own state database, read-only.
    Used only for rollouts that never record a turn_context. Empty when unavailable."""
    db = home / "state_5.sqlite"
    try:
        stamp = db.stat().st_mtime
    except OSError:
        return {}
    key = (str(db), stamp)
    if key in _state_cache:
        return _state_cache[key]
    out: dict[str, tuple[str, str]] = {}
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        try:
            conn.execute("PRAGMA query_only = ON")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "threads" in tables:
                for thread_id, model, effort in conn.execute("SELECT id, model, reasoning_effort FROM threads"):
                    if thread_id:
                        out[str(thread_id)] = (str(model or ""), str(effort or ""))
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    _state_cache.clear()
    _state_cache[key] = out
    return out


def discover(home: Path | None = None, since_days: float | None = 30) -> list[tuple[Path, int, float]]:
    """(path, size, mtime) for rollouts modified within since_days (None = all)."""
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
    return found


def parse_file(path: Path, warnings: list[str] | None = None) -> list[Event]:
    source = str(path)
    thread_id = session_id_from_filename(path)
    tool = "codex-cli"
    thread = "root"
    model = effort = ""
    first_model = first_effort = ""
    turn_id = ""
    own_turns: set[str] = set()  # turns this rollout has a turn_context for, i.e. its own work
    previous: Usage | None = None
    requests: list[Event] = []
    request_turns: list[str] = []
    prompt_events: list[Event] = []  # event_msg user_message (codex exec)
    prompt_items: list[Event] = []   # response_item role=user (Codex Desktop)
    pending: list[tuple[list[Event], int]] = []

    def fill_pending() -> None:
        if not model and not effort:
            return
        for bucket, index in pending:
            bucket[index] = bucket[index].with_model(model, effort)
        pending.clear()

    def prompt(bucket: list[Event], text: str, ts) -> None:
        bucket.append(Event(PROVIDER, tool, PROMPT, prompt_key(ts, text), ts, session_id=thread_id, thread=thread, model=model, effort=effort, source_file=source))
        # In a subagent rollout a prompt seen before any turn_context is usually the parent's,
        # copied in; leave it blank so the parent's own rollout fills it.
        if not model and not effort and thread != "subagent":
            pending.append((bucket, len(bucket) - 1))

    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, start=1):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            entry_type = entry.get("type")
            ts = parse_iso(entry.get("timestamp"))
            ordinal = entry.get("ordinal")
            ordinal_key = str(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool) else f"L{lineno}"

            if entry_type == "session_meta":
                thread_id = str(payload.get("id") or payload.get("session_id") or thread_id)
                if str(payload.get("originator") or "") == "Codex Desktop":
                    tool = "codex-desktop"
                if payload.get("thread_source") == "subagent" or payload.get("parent_thread_id") or _subagent_spawn(payload.get("source")):
                    thread = "subagent"
                continue
            if entry_type == "turn_context":
                model = str(payload.get("model") or model)
                effort = str(payload.get("effort") or effort)
                first_model, first_effort = first_model or model, first_effort or effort
                turn_id = str(payload.get("turn_id") or turn_id)
                if turn_id:
                    own_turns.add(turn_id)
                fill_pending()
                continue
            if entry_type == "response_item":
                if payload.get("type") == "message" and payload.get("role") == "user" and ts is not None:
                    text = input_text(payload.get("content"))
                    if text is not None and text.strip() and not is_wrapper(text):
                        prompt(prompt_items, text, ts)
                continue
            if entry_type != "event_msg":
                continue
            event_type = payload.get("type")
            if event_type == "task_started":
                turn_id = str(payload.get("turn_id") or turn_id)
                continue
            if event_type == "user_message":
                text = str(payload.get("message") or "")
                if ts is not None and text.strip():
                    prompt(prompt_events, text, ts)
                continue
            if event_type != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            current = usage_vector(info.get("total_token_usage"))
            if current is None:
                continue
            increment = usage_increment(current, usage_vector(info.get("last_token_usage")), previous)
            previous = current
            if not increment.total_tokens or ts is None:
                continue
            request_turns.append(turn_id)
            requests.append(Event(
                PROVIDER, tool, REQUEST, request_key(thread_id, turn_id, ordinal_key, current), ts,
                session_id=thread_id, thread=thread, model=model, effort=effort,
                input_tokens=increment.input_tokens,
                cache_read_tokens=increment.cached_input_tokens,
                cache_write_tokens=increment.cache_write_input_tokens,
                output_tokens=increment.output_tokens,
                reasoning_tokens=increment.reasoning_output_tokens,
                total_tokens=increment.total_tokens,
                source_file=source,
            ))
            fill_pending()
    prompts = prompt_events if prompt_events else prompt_items
    if any(not e.model or not e.effort for e in requests + prompts):
        # A thread runs on one model, so its first turn_context (or Codex's own thread record)
        # fills calls logged before it. In a subagent rollout only the child's own turns are
        # filled; the copied parent history stays blank for the parent's rollout to attribute.
        fill_model, fill_effort = first_model, first_effort
        if not fill_model or not fill_effort:
            state_model, state_effort = state_models(home_of(path)).get(thread_id, ("", ""))
            fill_model, fill_effort = fill_model or state_model, fill_effort or state_effort

        def fill(event: Event) -> Event:
            return replace(event, model=event.model or fill_model, effort=event.effort or fill_effort)

        if thread == "subagent":
            requests = [fill(e) if (not tid or tid in own_turns) else e for e, tid in zip(requests, request_turns)]
        else:
            requests = [fill(e) for e in requests]
            prompts = [fill(p) for p in prompts]
    return requests + prompts
