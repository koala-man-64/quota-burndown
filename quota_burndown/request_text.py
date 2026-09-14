"""Read text for a selected ledger request without retaining transcripts in the ledger.

These are recorded user prompts and per-call responses, not reconstructed API bodies.
All source paths come from server-owned ledger rows, never from HTTP parameters.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .usage import antigravity, claude, codex
from .util import iso, parse_iso

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 256 * 1024


class TextUnavailable(ValueError):
    pass


def row_id(row) -> str:
    return hashlib.sha256(json.dumps([row['provider'], row['event_key'], row['source_file']]).encode()).hexdigest()


def _entries(path: Path):
    with path.open('rb') as source:
        consumed = 0
        for index in range(1, 1_000_001):
            line = source.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            consumed += len(line)
            if len(line) > MAX_LINE_BYTES or consumed > MAX_SOURCE_BYTES:
                raise TextUnavailable('Transcript exceeds the safe viewer read limit.')
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(entry, dict):
                yield index, entry
        raise TextUnavailable('Transcript exceeds the safe viewer record limit.')


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get('type') in ('text', 'input_text', 'output_text') and isinstance(block.get('text'), str):
            parts.append(block['text'])
    return '\n'.join(parts)


def _result(prompt: str, response: str, note: str = '') -> dict:
    if len(prompt) + len(response) > MAX_TEXT_CHARS:
        raise TextUnavailable('Recorded text exceeds the safe viewer display limit.')
    return {'status': 'available' if prompt and response else 'partial', 'request': prompt, 'response': response,
            'note': note or ('Recorded user prompt and response only; full API input, hidden reasoning, tool payloads, and prior conversation context are not reconstructed.'
                            if prompt and response else 'Partial text: a prompt or response could not be matched to this call. Missing text is not reconstructed from neighboring calls.')}


def _codex(row, path: Path) -> dict:
    thread_id = codex.session_id_from_filename(path)
    turn_id = ''
    previous = None
    prompt = ''
    pending_prompt = False
    response = []
    fallback = []
    for lineno, entry in _entries(path):
        payload = entry.get('payload')
        if not isinstance(payload, dict):
            continue
        kind = entry.get('type')
        event = payload.get('type')
        if kind == 'session_meta':
            thread_id = str(payload.get('id') or payload.get('session_id') or thread_id)
        elif kind == 'turn_context' or (kind == 'event_msg' and event == 'task_started'):
            next_turn = str(payload.get('turn_id') or turn_id)
            if next_turn != turn_id:
                if not pending_prompt:
                    prompt = ''
                response, fallback = [], []
            turn_id = next_turn
        elif kind == 'response_item':
            if event == 'message':
                text = _text(payload.get('content'))
                if payload.get('role') == 'user' and text and not codex.is_wrapper(text):
                    prompt = text
                    pending_prompt = True
                elif payload.get('role') == 'assistant' and payload.get('channel') != 'analysis' and text:
                    response.append(text)
        elif kind == 'event_msg':
            if event == 'user_message':
                prompt = str(payload.get('message') or '')
                pending_prompt = True
            elif event == 'agent_message':
                fallback.append(str(payload.get('message') or ''))
            elif event == 'token_count':
                info = payload.get('info')
                if not isinstance(info, dict):
                    continue
                current = codex.usage_vector(info.get('total_token_usage'))
                if current is None:
                    continue
                increment = codex.usage_increment(current, codex.usage_vector(info.get('last_token_usage')), previous)
                previous = current
                ts = parse_iso(entry.get('timestamp'))
                if not increment.total_tokens or ts is None:
                    continue
                ordinal = entry.get('ordinal')
                ordinal_key = str(ordinal) if type(ordinal) is int else f'L{lineno}'
                key = codex.request_key(thread_id, turn_id, ordinal_key, current)
                if key == row['event_key'] and thread_id == row['session_id'] and iso(ts) == row['ts']:
                    return _result(prompt, '\n\n'.join(response or fallback))
                response, fallback = [], []
                pending_prompt = False
        if len(prompt) + sum(map(len, response)) + sum(map(len, fallback)) > MAX_TEXT_CHARS:
            raise TextUnavailable('Recorded text exceeds the safe viewer display limit.')
    raise TextUnavailable('The recorded request could not be matched in its source transcript.')


def _claude(row, path: Path) -> dict:
    prompt = ''
    selected_prompt = ''
    found = False
    parts = []
    for _, entry in _entries(path):
        message = entry.get('message')
        if not isinstance(message, dict):
            continue
        if entry.get('type') == 'user' and claude._is_prompt(entry) and str(entry.get('sessionId') or '') == row['session_id']:
            text = _text(message.get('content'))
            if text:
                prompt = text
        elif entry.get('type') == 'assistant':
            key = str(message.get('id') or entry.get('requestId') or entry.get('uuid') or '')
            if key == row['event_key'] and str(entry.get('sessionId') or '') == row['session_id']:
                if not found:
                    selected_prompt = prompt
                found = True
                text = _text(message.get('content'))
                # Streams may repeat or extend the same text block.
                if text and text not in parts:
                    if parts and text.startswith(parts[-1]):
                        parts[-1] = text
                    else:
                        parts.append(text)
                if len(selected_prompt) + sum(map(len, parts)) > MAX_TEXT_CHARS:
                    raise TextUnavailable('Recorded text exceeds the safe viewer display limit.')
    if not found:
        raise TextUnavailable('The recorded request could not be matched in its source transcript.')
    return _result(selected_prompt, '\n\n'.join(parts))


def _antigravity(row, path: Path) -> dict:
    transcript = antigravity.transcript_path(path.parent.parent, path.stem)
    steps = []
    for _, entry in _entries(transcript):
        if type(entry.get('step_index')) is int and parse_iso(entry.get('created_at')):
            steps.append(entry)
    steps.sort(key=lambda step: step['step_index'])
    planners = [s for s in steps if s.get('type') == 'PLANNER_RESPONSE']
    # The existing provider associates generations by order, only when counts agree.
    import sqlite3
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        indexes = [r[0] for r in conn.execute('SELECT idx FROM gen_metadata ORDER BY idx LIMIT 100001')]
    finally:
        conn.close()
    if len(indexes) != len(planners) or len(indexes) > 100000:
        raise TextUnavailable('Generation and transcript counts differ; text cannot be matched safely.')
    matches = [i for i, idx in enumerate(indexes) if f'{path.stem}:gen:{idx}' == row['event_key']]
    if len(matches) != 1 or path.stem != row['session_id']:
        raise TextUnavailable('The recorded request could not be matched in its source transcript.')
    selected = planners[matches[0]]
    if iso(parse_iso(selected['created_at'])) != row['ts']:
        raise TextUnavailable('Transcript timestamp does not match the recorded request.')
    prompts = [s for s in steps if s.get('type') == 'USER_INPUT' and s['step_index'] < selected['step_index']]
    return _result(str(prompts[-1].get('content') or '') if prompts else '', str(selected.get('content') or ''))


def read_request(row) -> dict:
    import sqlite3
    try:
        source = row['source_file']
        if not source:
            raise TextUnavailable('No source transcript was recorded for this request.')
        parser = {'codex': _codex, 'claude': _claude, 'antigravity': _antigravity}.get(row['provider'])
        if parser is None:
            raise TextUnavailable('Text viewing is unavailable for this provider.')
        return parser(row, Path(source))
    except TextUnavailable as exc:
        return {'status': 'unavailable', 'note': str(exc)}
    except (OSError, sqlite3.Error):
        return {'status': 'unavailable', 'note': 'Source transcript is missing, unreadable, or currently unavailable.'}
