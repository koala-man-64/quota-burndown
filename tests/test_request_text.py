import http.client
import json
import threading
from dataclasses import asdict

import pytest

from quota_burndown import ledger, render, request_text
from quota_burndown.service import CapacityService, make_server
from quota_burndown.usage import antigravity, claude, codex
from quota_burndown.util import iso, now_utc
from test_usage_antigravity import make_home
from test_usage_codex import session_meta, token_count, turn_context, user_item, vec, write


def row(event):
    result = asdict(event)
    result['ts'] = iso(event.ts)
    return result


def response(text):
    return json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                      'channel': 'final', 'content': [{'type': 'output_text', 'text': text}]}})


def test_codex_selects_one_call_and_deduplicates_usage(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    first = token_count('2026-09-02T01:00:02Z', 4, vec(10, 0, 5), vec(10, 0, 5))
    write(path, [session_meta('2026-09-02T01:00:00Z', 'session'),
                 turn_context('2026-09-02T01:00:00Z', 1, 'test', 'low', 'turn'),
                 user_item('2026-09-02T01:00:01Z', 2, 'raw <script>prompt</script>'),
                 response('first answer'), first, first,
                 response('second answer'),
                 token_count('2026-09-02T01:00:03Z', 7, vec(20, 0, 10), vec(10, 0, 5)),
                 turn_context('2026-09-02T01:00:04Z', 8, 'test', 'low', 'next'),
                 user_item('2026-09-02T01:00:04Z', 9, 'new prompt'), response('third answer'),
                 token_count('2026-09-02T01:00:05Z', 11, vec(30, 0, 15), vec(10, 0, 5))])
    rows = [row(e) for e in codex.parse_file(path) if e.kind == ledger.REQUEST]
    results = [request_text.read_request(r) for r in rows]
    assert [r['response'] for r in results] == ['first answer', 'second answer', 'third answer']
    assert [r['request'] for r in results] == ['raw <script>prompt</script>'] * 2 + ['new prompt']
    rows[0]['session_id'] = 'wrong'
    assert request_text.read_request(rows[0])['status'] == 'unavailable'


def test_codex_no_recorded_response_does_not_borrow_next_call(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write(path, [session_meta('2026-09-02T01:00:00Z', 'session'),
                 user_item('2026-09-02T01:00:01Z', 1, 'prompt'),
                 token_count('2026-09-02T01:00:02Z', 2, vec(10, 0, 5), vec(10, 0, 5)),
                 response('later answer')])
    event = next(e for e in codex.parse_file(path) if e.kind == ledger.REQUEST)
    result = request_text.read_request(row(event))
    assert result['response'] == ''
    assert result['status'] == 'partial' and 'neighboring calls' in result['note']


def test_codex_prompt_before_task_start_and_tool_payloads_are_excluded(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write(path, [session_meta('2026-09-02T01:00:00Z', 'session'),
                 user_item('2026-09-02T01:00:01Z', 1, 'original prompt'),
                 json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}}),
                 turn_context('2026-09-02T01:00:01Z', 3, 'test', 'low', 'turn'),
                 json.dumps({'type': 'response_item', 'payload': {'type': 'function_call', 'arguments': 'sensitive tool argument'}}),
                 response('visible answer'),
                 token_count('2026-09-02T01:00:02Z', 6, vec(10, 0, 5), vec(10, 0, 5))])
    event = next(e for e in codex.parse_file(path) if e.kind == ledger.REQUEST)
    result = request_text.read_request(row(event))
    assert result['request'] == 'original prompt'
    assert result['response'] == 'visible answer'
    assert 'sensitive tool argument' not in json.dumps(result)


def claude_entry(kind, content, key='', **extra):
    return {'type': kind, 'sessionId': 's', 'timestamp': '2026-09-02T01:00:00Z',
            'message': {'id': key, 'content': content, 'model': 'test', 'usage': {'input_tokens': 2, 'output_tokens': 3}}, **extra}


def test_claude_streams_and_tool_results(tmp_path):
    path = tmp_path / 'session.jsonl'
    entries = [claude_entry('user', 'first prompt'),
               claude_entry('assistant', [{'type': 'text', 'text': 'hello'}], 'a'),
               claude_entry('assistant', [{'type': 'text', 'text': 'hello world'}], 'a'),
               claude_entry('user', [{'type': 'tool_result', 'content': 'tool output'}], toolUseResult={}),
               claude_entry('user', [{'type': 'tool_result', 'content': 'unflagged tool output'}]),
               claude_entry('assistant', [{'type': 'tool_use', 'input': {'secret': 'tool input'}}], 'b'),
               claude_entry('assistant', [{'type': 'text', 'text': 'next call'}], 'b'),
               claude_entry('user', 'later prompt'),
               claude_entry('assistant', [{'type': 'text', 'text': 'later answer'}], 'c')]
    write(path, [json.dumps(e) for e in entries])
    rows = [row(e) for e in claude.parse_file(path) if e.kind == ledger.REQUEST]
    assert request_text.read_request(rows[0])['response'] == 'hello world'
    assert request_text.read_request(rows[1])['request'] == 'first prompt'
    assert request_text.read_request(rows[1])['response'] == 'next call'
    assert request_text.read_request(rows[2])['request'] == 'later prompt'


@pytest.mark.parametrize('gens,expected', [((1, 2), 'available'), ((1, 2, 3), 'unavailable')])
def test_antigravity_requires_matching_generations(tmp_path, gens, expected):
    _, path = make_home(tmp_path, gens=gens)
    events = [e for e in antigravity.parse_file(path) if e.kind == ledger.REQUEST]
    result = request_text.read_request(row(events[1]))
    assert result['status'] == expected
    if expected == 'available':
        assert result['request'] == '<USER_REQUEST>more</USER_REQUEST>'
        assert result['response'] == 'done'


def test_missing_source_and_limits(tmp_path, monkeypatch):
    event = ledger.Event('claude', 'claude-code', ledger.REQUEST, 'a', now_utc(), source_file=str(tmp_path / 'missing'))
    assert request_text.read_request(row(event))['status'] == 'unavailable'
    path = tmp_path / 'huge.jsonl'
    path.write_text('x' * 100)
    monkeypatch.setattr(request_text, 'MAX_LINE_BYTES', 50)
    result = request_text.read_request({**row(event), 'source_file': str(path)})
    assert result['status'] == 'unavailable' and 'limit' in result['note']


def publish(service, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(service.stop_event, 'wait', lambda _: service.stop_event.set())
        service._ledger()
    service.stop_event.clear()


def test_endpoint_only_accepts_displayed_rows_and_keeps_text_out_of_page(paths, monkeypatch):
    conn = ledger.connect(paths.usage_db)
    events = [ledger.Event('claude', 'claude-code', ledger.REQUEST, str(i), now_utc()) for i in range(26)]
    ledger.upsert(conn, events)
    conn.close()
    service = CapacityService(paths, collectors=False)
    publish(service, monkeypatch)
    assert len(service._recent_page[1]) == 26
    assert service._page.count(b'<summary>View raw text</summary>') == 26
    secret = '</pre><script>window.injected=true</script>'
    reads = []
    monkeypatch.setattr(request_text, 'read_request', lambda r: reads.append(r) or request_text._result(secret, 'answer'))
    server = make_server(service, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def get(path, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        client.request('GET', path, headers=headers or {})
        result = client.getresponse()
        status, body, cache = result.status, result.read(), result.getheader('Cache-Control')
        client.close()
        return status, body, cache
    try:
        for event in events[:5]:
            rid = request_text.row_id(row(event))
            assert get('/v1/recent-text/' + rid)[0] == 200
        selected = next(iter(service._recent_page[1]))
        for invalid in ('../../secret', 'unknown'):
            assert get('/v1/recent-text/' + invalid)[0] == 404
        assert reads == []
        assert get('/v1/recent-text/' + selected, {'Origin': 'https://attacker.example'})[0] == 403
        assert reads == []
        status, body, cache = get('/v1/recent-text/' + selected)
        assert status == 200 and cache == 'no-store'
        assert json.loads(body)['request'] == secret
        assert len(reads) == 1
        for path in ('/', '/usage.json', '/v1/usage'):
            assert secret.encode() not in get(path)[1]
        service._recent_page = (service._page, {})
        assert get('/v1/recent-text/' + selected)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_static_render_does_not_offer_or_embed_text(paths, store):
    conn = ledger.connect(paths.usage_db)
    ledger.upsert(conn, [ledger.Event('claude', 'claude-code', ledger.REQUEST, 'test', now_utc())])
    conn.close()
    assert 'View raw text' not in render.render_html(store, usage_db=paths.usage_db)
