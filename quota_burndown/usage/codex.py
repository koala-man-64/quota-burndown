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
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from ..config import codex_home
from ..ledger import PROMPT, REQUEST, Event
from ..util import parse_iso

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
    previous: Usage | None = None
    requests: list[Event] = []
    prompt_events: list[Event] = []  # event_msg user_message (codex exec)
    prompt_items: list[Event] = []   # response_item role=user (Codex Desktop)
    pending: list[tuple[list[Event], int]] = []

    def fill_pending() -> None:
        if not model and not effort:
            return
        for bucket, index in pending:
            bucket[index] = bucket[index].with_model(model, effort)
        pending.clear()

    def prompt(bucket: list[Event], key: str, ts) -> None:
        bucket.append(Event(PROVIDER, tool, PROMPT, key, ts, session_id=thread_id, thread=thread, model=model, effort=effort, source_file=source))
        if not model and not effort:
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
            key = f"{thread_id}:{ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else 'L%d' % lineno}"

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
                fill_pending()
                continue
            if entry_type == "response_item":
                if payload.get("type") == "message" and payload.get("role") == "user" and ts is not None:
                    text = input_text(payload.get("content"))
                    if text is not None and text.strip() and not is_wrapper(text):
                        prompt(prompt_items, key, ts)
                continue
            if entry_type != "event_msg":
                continue
            event_type = payload.get("type")
            if event_type == "user_message":
                if ts is not None:
                    prompt(prompt_events, key, ts)
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
            requests.append(Event(
                PROVIDER, tool, REQUEST, key, ts,
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
    return requests + prompts
