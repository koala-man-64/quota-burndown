import json
import threading
from datetime import timedelta
from email.utils import format_datetime

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
