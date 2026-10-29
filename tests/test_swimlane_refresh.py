import json
import re
import shutil
import subprocess
import threading
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

from quota_burndown import swimlane
from quota_burndown.service import CapacityService, WriterLease
from quota_burndown.util import now_utc


def launcher(tmp_path, body):
    path = tmp_path / "collector.py"
    path.write_text(body, encoding="utf-8")
    return path


def successful_launcher(tmp_path):
    return launcher(tmp_path, """
import json, sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
assert sys.argv[1] == '--home' and sys.argv[3:] == ['refresh']
home = Path(sys.argv[2])
now = datetime.now(timezone.utc).replace(microsecond=0)
(home/'snapshot.json').write_text(json.dumps({'serverDate': format_datetime(now)}))
revision = now.isoformat().replace('+00:00', 'Z')
(home/'swimlane.html').write_text('<html><head><meta name="ado-snapshot-at" content="'+revision+'"></head><body>current board</body></html>')
""")


def test_real_child_refresh_and_failure_keep_last_good(tmp_path, monkeypatch):
    worker = swimlane.SwimlaneRefresh(home=tmp_path / "data", launcher=successful_launcher(tmp_path))
    assert worker.refresh(threading.Event()) == 300, worker.status()
    assert worker.status()["state"] == "ok"
    assert worker.status()["stale"] is False
    before = (worker.home / "swimlane.html").read_bytes()
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "test-secret")
    worker.launcher = launcher(tmp_path, "import os,sys; print(os.environ['AZURE_DEVOPS_EXT_PAT']); sys.exit(1)")
    assert worker.refresh(threading.Event()) == 60
    status = worker.status()
    assert status["state"] == "error"
    assert "test-secret" not in json.dumps(status)
    assert "[redacted]" in status["detail"]
    assert "retaining last good" in worker.label(status)
    assert (worker.home / "swimlane.html").read_bytes() == before
    assert "test-secret" not in (worker.home / "refresh-status.json").read_text()


@pytest.mark.parametrize("failure, expected_detail", [
    ("auth", "AZURE_DEVOPS_EXT_PAT is missing"),
    ("network", "ADO request failed"),
])
def test_host_failure_banner_keeps_recent_last_good_board(paths, tmp_path, monkeypatch,
                                                        failure, expected_detail):
    home = tmp_path / "backlog"
    monkeypatch.setenv("ADO_SWIMLANE_HOME", str(home))
    service = CapacityService(paths)
    worker = service.swimlane
    worker.launcher = successful_launcher(tmp_path)
    try:
        assert worker.refresh(threading.Event()) == 300, worker.status()
        before = {name: (home / name).read_bytes() for name in ("snapshot.json", "swimlane.html")}
        revision = worker.status()["rendered_at"]
        assert worker.status()["stale"] is False
        assert "Refresh ok." in service.swimlane_html().decode()

        # Exercise the bundled CLI's auth/retry/error path in the real refresh
        # child. Every provider request is intercepted before any network I/O.
        bundled_launcher = Path(__file__).resolve().parents[1] / "ado-swimlane" / "ado-swimlane.py"
        monkeypatch.setenv("ADO_SWIMLANE_TEMPLATE", str(bundled_launcher.parent / "board" / "board.html"))
        worker.launcher = launcher(tmp_path, f"""
import runpy, sys, urllib.error
sys.path.insert(0, {str(bundled_launcher.parent)!r})
from ado_swimlane import ado
def fail_network(self, request):
    raise urllib.error.URLError('simulated offline provider')
ado.Client._open = fail_network
ado.time.sleep = lambda seconds: None
runpy.run_path({str(bundled_launcher)!r}, run_name='__main__')
""")
        if failure == "network":
            monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "test-only-pat")
        else:
            monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)

        assert worker.refresh(threading.Event()) == 60
        status = worker.status()
        assert status["state"] == "error"
        assert expected_detail in status["detail"]
        assert status["stale"] is False
        assert status["snapshot_at"] == status["rendered_at"] == revision
        page = service.swimlane_html().decode()
        assert "Refresh failed; retaining last good board." in page
        assert f'ado-snapshot-at" content="{revision}"' in page
        assert "current board" in page
        assert "Refresh ok." not in page
        assert {name: (home / name).read_bytes() for name in before} == before
        assert service.threads == []
    finally:
        service.close()


def test_writer_contention_does_not_launch_child(tmp_path):
    worker = swimlane.SwimlaneRefresh(home=tmp_path, launcher=tmp_path / "missing.py")
    with WriterLease(tmp_path, ".dashboard-refresh.lock"):
        assert worker.refresh(threading.Event()) == 60
    assert worker.status()["state"] == "busy"
    assert worker.status()["attempted_at"] is None


def test_snapshot_clock_not_attempt_clock_controls_freshness(tmp_path):
    stamp = now_utc() - timedelta(days=4)
    (tmp_path / "snapshot.json").write_text(json.dumps({"serverDate": format_datetime(stamp)}))
    (tmp_path / "swimlane.html").write_text('<meta name="ado-snapshot-at" content="2026-10-25T13:52:00Z">')
    worker = swimlane.SwimlaneRefresh(home=tmp_path)
    worker.attempt = now_utc().isoformat()
    worker._set("ok")
    assert worker.status()["stale"] is True
    assert "Backlog stale" in worker.label(worker.status())
    assert "no verified provider snapshot" in worker.label(worker.status(), rendered=False)


def test_worker_retries_and_keeps_running_after_filesystem_error(tmp_path, monkeypatch):
    worker = swimlane.SwimlaneRefresh(home=tmp_path)
    waits = []
    event = threading.Event()
    def refresh(stop):
        if not waits:
            raise OSError("disk unavailable")
        worker._set("ok")
        return 300
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            event.set()
    monkeypatch.setattr(worker, "refresh", refresh)
    monkeypatch.setattr(event, "wait", wait)
    worker.run(event)
    assert waits == [60, 300]
    assert worker.status()["state"] == "ok"


def test_timeout_terminates_child(tmp_path, monkeypatch):
    worker = swimlane.SwimlaneRefresh(home=tmp_path / "data", launcher=launcher(tmp_path, "import time; time.sleep(30)"))
    monkeypatch.setattr(swimlane, "TIMEOUT_S", 0)
    assert worker.refresh(threading.Event()) == 60
    assert worker.status()["state"] == "error"
    assert "deadline" in worker.status()["detail"]


def test_host_decorates_cached_board_and_starts_worker(paths, tmp_path, monkeypatch):
    monkeypatch.setenv("ADO_SWIMLANE_HOME", str(tmp_path))
    (tmp_path / "swimlane.html").write_text('<html><body>saved board</body></html>')
    service = CapacityService(paths)
    page = service.swimlane_html().decode()
    assert 'id="ado-refresh-status"' in page
    assert "no verified provider snapshot" in page
    assert "/v1/swimlane/status" in page
    names = []
    monkeypatch.setattr(service, "_spawn", lambda name, target: names.append(name))
    try:
        service.start()
        assert "backlog-refresh" in names
    finally:
        service.close()


def test_old_render_never_inherits_new_snapshot_freshness(tmp_path):
    (tmp_path / "snapshot.json").write_text(json.dumps({"serverDate": format_datetime(now_utc())}))
    (tmp_path / "swimlane.html").write_text('<meta name="ado-snapshot-at" content="2026-10-25T13:52:00Z"><body>old</body>')
    worker = swimlane.SwimlaneRefresh(home=tmp_path)
    assert worker.status()["stale"] is True
    page = worker.decorate((tmp_path / "swimlane.html").read_bytes(), rendered=True).decode()
    assert "Backlog stale · 2026-10-25T13:52:00Z" in page
    assert "s.rendered_at!==snapshot" in page


def test_curated_template_override_is_passed_without_publication(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    source = tmp_path / "curated.html"
    source.write_text("curated prose")
    (home / "refresh-config.json").write_text(json.dumps({"template": str(source)}))
    child = successful_launcher(tmp_path)
    code = child.read_text().replace("sys.argv[3:] == ['refresh']", "sys.argv[3:] == " + repr(['refresh', '--template', str(source)]))
    child.write_text(code)
    worker = swimlane.SwimlaneRefresh(home=home, launcher=child)
    assert worker.refresh(threading.Event()) == 300, worker.status()
    assert worker.status()["state"] == "ok"


def test_malformed_or_missing_configured_template_retains_last_good(tmp_path):
    worker = swimlane.SwimlaneRefresh(home=tmp_path, launcher=tmp_path / "must-not-launch.py")
    (tmp_path / "swimlane.html").write_text("last good")
    for config in ('{bad', json.dumps({"template": ""}), json.dumps({"template": str(tmp_path / "missing.html")})):
        (tmp_path / "refresh-config.json").write_text(config)
        assert worker.refresh(threading.Event()) == 60
        assert worker.status()["state"] == "error"
        assert (tmp_path / "swimlane.html").read_text() == "last good"


def test_partial_commit_label_stays_bound_to_rendered_revision(tmp_path):
    worker = swimlane.SwimlaneRefresh(home=tmp_path)
    (tmp_path / "snapshot.json").write_text(json.dumps({"serverDate": format_datetime(now_utc())}))
    (tmp_path / "swimlane.html").write_text('<meta name="ado-snapshot-at" content="2026-10-25T13:52:00Z">')
    worker._set("error", "write failed")
    status = worker.status()
    label = worker.label(status)
    assert "Backlog stale · 2026-10-25T13:52:00Z" in label
    assert "Refresh failed" in label
    page = worker.decorate((tmp_path / "swimlane.html").read_bytes(), rendered=True).decode()
    assert "if(s.rendered_at===snapshot)el.textContent=s.label" in page


@pytest.mark.parametrize("storage_available", [True, False])
def test_embedded_board_preserves_details_and_scroll_on_refresh(tmp_path, storage_available):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the embedded refresh script")
    revision = "2026-10-29T12:00:00Z"
    content = (
        f'<html><head><meta name="ado-snapshot-at" content="{revision}"></head><body>'
        '<details id="tracked-snapshot-details"></details>'
        '<details id="relation-snapshot-details" open></details></body></html>'
    ).encode()
    page = swimlane.SwimlaneRefresh(home=tmp_path).decorate(content, rendered=True).decode()
    script = re.findall(r"<script>(.*?)</script>", page, re.S)[-1]
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const script = __SCRIPT__, storageAvailable = __STORAGE_AVAILABLE__;
const key = 'swimlane.view';
const tracked = {id:'tracked-snapshot-details', tagName:'DETAILS', open:false};
const relations = {id:'relation-snapshot-details', tagName:'DETAILS', open:true};
const banner = {textContent:''};
const elements = {[tracked.id]:tracked, [relations.id]:relations, 'ado-refresh-status':banner};
const saved = {y:0, open:[tracked.id], closed:[relations.id]};
const values = new Map([[key, JSON.stringify(saved)]]);
const sessionStorage = {};
for (const method of ['getItem','setItem','removeItem']) {
  sessionStorage[method] = function(k, value) {
    if (!storageAvailable) throw Error('storage unavailable');
    if (method === 'getItem') return values.get(k) || null;
    if (method === 'setItem') values.set(k, value);
    if (method === 'removeItem') values.delete(k);
  };
}
let timer, reloads = 0, scrolled = null;
const listeners = {};
const status = {rendered_at:__REVISION__, label:'current'};
const window = {
  scrollY:99,
  scrollTo:(x,y) => {scrolled=[x,y];},
  addEventListener:(name, callback) => {listeners[name]=callback;}
};
const context = {
  document:{getElementById:id => elements[id], querySelectorAll:selector => {
    assert.equal(selector, 'details[id]');
    return [tracked, relations];
  }},
  window, sessionStorage,
  fetch:() => Promise.resolve({ok:true, json:() => Promise.resolve({...status})}),
  setInterval:callback => {timer=callback;},
  location:{reload:() => {reloads++;}}
};
async function main() {
  vm.runInNewContext(script, context);
  await new Promise(setImmediate);
  assert.equal(banner.textContent, 'current');
  assert.equal(reloads, 0);
  if (storageAvailable) {
    assert.equal(tracked.open, true);
    assert.equal(relations.open, false);
    assert.deepEqual(scrolled, [0,0]);
    assert.equal(values.has(key), false);
  }
  // pagehide captures changes for the static page's meta refresh too.
  tracked.open=false; relations.open=true; window.scrollY=321;
  listeners.pagehide();
  if (storageAvailable) {
    assert.deepEqual(JSON.parse(values.get(key)),
      {y:321, open:[relations.id], closed:[tracked.id]});
    values.delete(key);
  }
  // A provider revision change captures the current view before reloading.
  tracked.open=true; relations.open=false; window.scrollY=654;
  status.rendered_at='next-revision';
  timer();
  await new Promise(setImmediate);
  assert.equal(reloads, 1);
  if (storageAvailable) assert.deepEqual(JSON.parse(values.get(key)),
    {y:654, open:[tracked.id], closed:[relations.id]});
}
main().catch(error => {console.error(error); process.exitCode=1;});
"""
    harness = (harness.replace("__SCRIPT__", json.dumps(script))
               .replace("__STORAGE_AVAILABLE__", json.dumps(storage_available))
               .replace("__REVISION__", json.dumps(revision)))
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
