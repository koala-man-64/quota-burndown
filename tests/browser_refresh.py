"""Dependency-free browser regression: py -B tests/browser_refresh.py.

Open the printed /tests URL. Uses disposable synthetic data, the real renderer,
and accelerated browser timers. Results remain visible for inspection.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_burndown import ledger, render
from quota_burndown.config import Paths
from quota_burndown.store import Sample, Store
from quota_burndown.util import now_utc


HARNESS = """<!doctype html><title>Dashboard refresh regression</title>
<h1>Dashboard refresh regression</h1><pre id="result">Running…</pre>
<iframe title="Dashboard under test" src="/" style="width:100%;height:650px"></iframe>
<script>
const frame=document.querySelector('iframe'), result=document.querySelector('#result');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const assert=(ok,why)=>{if(!ok) throw new Error(why);};
async function until(fn,why){for(let i=0;i<100;i++){if(fn())return;await sleep(50);}throw new Error(why);}
async function mode(value){await fetch('/fixture?'+new URLSearchParams(value));}
frame.onload=async()=>{
 try {
  const win=frame.contentWindow, doc=win.document, errors=[];
  win.addEventListener('error',e=>errors.push(e.message));
  const snapshot=()=>doc.querySelector('#dashboard-data').dataset.generatedAt;
  const status=()=>doc.querySelector('#refresh-status').textContent;
  const card=()=>doc.querySelector('article[data-provider="codex"]');
  const text=doc.querySelector('.request-text');
  text.querySelector('summary').click();
  await until(()=>text.dataset.loaded,'raw text loads');
  const raw=text.querySelector('.request-input').textContent;
  assert(raw.includes('<script>'),'raw text is displayed literally');
  assert(!win.injected,'raw text must not execute');
  doc.querySelector('.target-preset').click();
  const target=doc.querySelector('#target-date-input').value;
  doc.querySelector('.chart-table summary').click();
  const chart=doc.querySelector('svg.chart'); chart.focus();
  chart.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
  assert(!doc.querySelector('#chart-tooltip').hidden,'initial chart keyboard interaction');
  win.scrollTo(0,150); const scroll=win.scrollY;
  const original=snapshot(), oldUsage=doc.querySelector('section.usage').textContent;
  const oldDaily=doc.querySelector('#daily-models-panel').textContent;
  const oldEfficiency=doc.querySelector('#efficiency-session-panel').textContent;
  await mode({revision:2});
  await until(()=>snapshot()!==original,'first automatic refresh');
  assert(card().dataset.used==='22.00','headline quota updates');
  assert(doc.querySelector('section.usage').textContent!==oldUsage,'usage totals update');
  assert(doc.querySelector('#daily-models-panel').textContent!==oldDaily,'daily model totals update');
  assert(doc.querySelector('#efficiency-session-panel').textContent!==oldEfficiency,'efficiency totals update');
  assert(JSON.parse(doc.querySelector('script.chart-data').textContent).points.at(-1).v==='22%','chart observations update');
  assert(doc.querySelector('#dashboard-meta').textContent.includes('2 historic samples'),'header count updates');
  assert(doc.querySelector('#target-date-input').value===target,'target date preserved');
  assert(doc.querySelector('#target-date-toggle').checked,'target mode preserved');
  assert(doc.querySelector('.chart-table').open,'expanded chart table preserved');
  assert(doc.activeElement.matches('svg.chart'),'keyboard focus restored');
  assert(Math.abs(win.scrollY-scroll)<2,'scroll position preserved');
  assert(text.isConnected&&text.open&&text.querySelector('.request-input').textContent===raw,'open raw text retained');
  doc.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
  assert(!doc.querySelector('#chart-tooltip').hidden,'new chart keyboard handler works');
  assert(doc.querySelector('.pace-target'),'target overlays reapplied');
  text.querySelector('summary').focus();
  const second=snapshot(); await mode({revision:3});
  await until(()=>snapshot()!==second,'second automatic refresh');
  assert(text.isConnected&&text.open,'expanded request remains after falling out of recent list');
  assert(doc.activeElement===text.querySelector('summary'),'retained request focus preserved');
  assert(doc.querySelectorAll('.request-text').length===2,'new requests appear alongside retained request');
  text.querySelector('summary').click();
  await mode({revision:4}); await until(()=>card().dataset.used==='24.00','closed retained row removed');
  assert(!text.isConnected,'retained row retires after closing');
  const good=snapshot(); await mode({mode:'fail'});
  await until(()=>status().includes('refresh failed'),'failure is visible');
  assert(snapshot()===good,'failed fetch keeps last good data');
  await mode({mode:'ok'});
  await until(()=>!status().includes('refresh failed'),'recover before malformed-response test');
  await mode({mode:'invalid'});
  await until(()=>status().includes('refresh failed'),'malformed response produces a new failure');
  const invalidStats=await (await fetch('/fixture')).json();
  assert(invalidStats.invalid_calls>0,'malformed-response request actually ran');
  assert(snapshot()===good&&status().includes('refresh failed'),'invalid snapshot keeps last good data');
  await mode({mode:'slow'}); await sleep(1100);
  assert(snapshot()===good&&status().includes('refresh failed'),'timeout keeps last good data');
  await mode({mode:'ok',revision:5});
  await until(()=>card().dataset.used==='25.00'&&!status().includes('failed'),'refresh recovers');
  const stats=await (await fetch('/fixture')).json();
  assert(stats.slow_calls>=2,'multiple timeout retries exercised');
  assert(stats.min_slow_gap>=300,'requests do not overlap before timeout');
  assert(errors.length===0,'no browser errors: '+errors.join(','));
  result.textContent='PASS: two automatic refresh cycles; all data regions; target date, expanded details, focus and scroll; loaded and retired request text; chart keyboard handlers; failed/malformed/timed-out fetch; retry recovery; bounded in-flight refresh.';
 } catch(error) {result.textContent='FAIL: '+error.stack;}
};
</script>"""


def main():
    with tempfile.TemporaryDirectory(prefix='quota-browser-test-') as folder:
        paths = Paths(Path(folder))
        store = Store(paths)
        now = now_utc()
        pages = {}
        conn = ledger.connect(paths.usage_db)
        for revision in range(1, 6):
            ts = now + timedelta(seconds=revision)
            store.append([Sample(ts, 'codex', '7d', 20 + revision,
                                 now + timedelta(days=3), 10080, 'app-server')])
            ledger.upsert(conn, [ledger.Event('codex', 'codex-desktop', ledger.REQUEST,
                                             str(revision), ts, model='test-model',
                                             session_id='browser-test', total_tokens=revision * 1000)])
            rows = ledger.recent_requests(conn)
            recent = rows if revision <= 2 else rows[:1]
            page = render.render_html(store, now=ts, usage_db=paths.usage_db,
                                      live=True, recent_rows=recent)
            # Speed only the real refresh and timeout timers, leaving logic intact.
            page = page.replace('</head>', '''<script>
const interval=window.setInterval, timeout=window.setTimeout;
window.setInterval=(fn,ms)=>interval(fn,ms===30000?150:ms);
window.setTimeout=(fn,ms)=>timeout(fn,ms===15000?350:ms);
</script></head>''')
            pages[revision] = page.encode()
        conn.close()
        state = {'revision': 1, 'mode': 'ok', 'last_slow': None, 'min_slow_gap': 99999,
                 'slow_calls': 0, 'invalid_calls': 0}
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                code, ctype = 200, 'text/html; charset=utf-8'
                if url.path == '/tests':
                    body = HARNESS.encode()
                elif url.path == '/fixture':
                    with lock:
                        args = parse_qs(url.query)
                        if 'revision' in args:
                            state['revision'] = int(args['revision'][0])
                        if 'mode' in args:
                            state['mode'] = args['mode'][0]
                        body = json.dumps(state).encode()
                    ctype = 'application/json'
                elif url.path.startswith('/v1/recent-text/'):
                    body = json.dumps({'status': 'available', 'request': '<script>window.injected=true</script>',
                                       'response': 'Synthetic recorded response'}).encode()
                    ctype = 'application/json'
                elif url.path == '/':
                    with lock:
                        mode, revision = state['mode'], state['revision']
                        if mode == 'invalid':
                            state['invalid_calls'] += 1
                        if mode == 'slow':
                            state['slow_calls'] += 1
                            clock = time.monotonic()
                            if state['last_slow'] is not None:
                                state['min_slow_gap'] = min(state['min_slow_gap'], (clock-state['last_slow'])*1000)
                            state['last_slow'] = clock
                    body = pages[revision]
                    if mode == 'fail':
                        code, body = 503, b'Unavailable'
                    elif mode == 'invalid':
                        body = b'<html>Invalid snapshot</html>'
                    elif mode == 'slow':
                        time.sleep(0.8)
                else:
                    code, body = 404, b'Not found'
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        print(f'http://127.0.0.1:{server.server_port}/tests', flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
