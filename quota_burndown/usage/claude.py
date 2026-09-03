"""Claude Code per-request usage from the transcripts under ~/.claude/projects.

Facts the parser relies on (verified against Claude Code 2.1.2xx transcripts):
  * one API response is written as several `assistant` lines, one per content block, all
    carrying the same message.id and the same usage object, so requests are keyed by
    message.id and the line with the largest output_tokens wins;
  * a human prompt is a `user` line whose message.content is a plain string and which has
    no top-level toolUseResult (tool results are also `user` lines, with list content);
  * the reasoning effort sits at the top level of each assistant line as `effort`;
  * subagent transcripts live under <project>/<session>/subagents/, workflow agents under
    <project>/<session>/subagents/workflows/.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import claude_home
from ..ledger import PROMPT, REQUEST, Event
from ..util import parse_iso

PROVIDER = "claude"
TOOL = "claude-code"
_MARKERS = ('"type":"assistant"', '"type": "assistant"', '"type":"user"', '"type": "user"')


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def projects_root(home: Path | None = None) -> Path:
    return (home or claude_home()) / "projects"


def discover(home: Path | None = None, since_days: float | None = 30) -> list[tuple[Path, int, float]]:
    """(path, size, mtime) for transcripts modified within since_days (None = all)."""
    root = projects_root(home)
    if not root.is_dir():
        return []
    cutoff = time.time() - since_days * 86400 if since_days is not None else None
    found: list[tuple[Path, int, float]] = []
    for path in root.rglob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        if cutoff is not None and st.st_mtime < cutoff:
            continue
        found.append((path, st.st_size, st.st_mtime))
    return found


def thread_kind(path: Path) -> str:
    text = path.as_posix()
    if "/subagents/workflows/" in text:
        return "workflow"
    if "/subagents/" in text:
        return "subagent"
    return "main"


def _request(entry: dict, ts, thread: str, source: str) -> Event | None:
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    key = str(message.get("id") or entry.get("requestId") or entry.get("uuid") or "")
    if not key:
        return None
    details = usage.get("output_tokens_details")
    thinking = _int(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    input_tokens = _int(usage.get("input_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    output = _int(usage.get("output_tokens"))
    return Event(
        PROVIDER, TOOL, REQUEST, key, ts,
        session_id=str(entry.get("sessionId") or ""),
        thread=thread,
        model=str(message.get("model") or ""),
        effort=str(entry.get("effort") or ""),
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output,
        reasoning_tokens=thinking,
        total_tokens=input_tokens + cache_read + cache_write + output,
        source_file=source,
    )


def _is_prompt(entry: dict) -> bool:
    message = entry.get("message")
    return isinstance(message, dict) and isinstance(message.get("content"), str) and "toolUseResult" not in entry


def parse_file(path: Path) -> list[Event]:
    """Every request (deduplicated by message id) and every human prompt in one transcript.
    Prompts take the model and effort of the first request that follows them."""
    thread = thread_kind(path)
    source = str(path)
    requests: dict[str, Event] = {}
    prompts: list[Event] = []
    pending: list[int] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not any(marker in line for marker in _MARKERS):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            ts = parse_iso(entry.get("timestamp"))
            if ts is None:
                continue
            if kind == "user":
                if _is_prompt(entry):
                    key = str(entry.get("uuid") or f"{path.name}:{lineno}")
                    prompts.append(Event(PROVIDER, TOOL, PROMPT, key, ts, session_id=str(entry.get("sessionId") or ""), thread=thread, source_file=source))
                    pending.append(len(prompts) - 1)
                continue
            if kind != "assistant":
                continue
            event = _request(entry, ts, thread, source)
            if event is None:
                continue
            previous = requests.get(event.event_key)
            if previous is None or (event.output_tokens or 0) >= (previous.output_tokens or 0):
                requests[event.event_key] = event
            for index in pending:
                prompts[index] = prompts[index].with_model(event.model, event.effort)
            pending.clear()
    return list(requests.values()) + prompts
