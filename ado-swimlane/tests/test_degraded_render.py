"""A render that failed its own checks preserves accepted state and shows health."""
import json
import argparse
import shutil
import subprocess

import pytest

from ado_swimlane import cli, diff, refresh, render, template, validate
from ado_swimlane.config import Paths, get_paths
from ado_swimlane.service import SwimlaneService

REV = "2026-09-14T22:15:00Z"
FAILURE = "CHILDREN key c999 has no card in the DOM"

HARNESS = r"""
const elements = [];
function element(tag) {
  return {tag, className: "", textContent: "", children: [], appendChild(child) { this.children.push(child); }};
}
globalThis.document = {createElement: element, body: {appendChild: child => elements.push(child)},
  getElementById: () => null, querySelectorAll: () => []};
globalThis.window = {scrollTo() {}, scrollY: 0};
globalThis.sessionStorage = {getItem: () => null, setItem() {}, removeItem() {}};
globalThis.setInterval = () => 0;
let reloaded = false;
globalThis.location = {reload() { reloaded = true; }};
let source;
globalThis.EventSource = function () { source = this; };
__LIVE_JS__
source.onmessage({data: JSON.stringify(__MESSAGE__)});
const chip = elements[0];
console.log(JSON.stringify({cls: chip.className, text: chip.children[1].textContent, reloaded}));
"""


@pytest.mark.parametrize("page_status,status,text,down,reloaded", [
    ("healthy", "degraded", "checks failed · 22:15:00Z", True, True),
    ("healthy", "healthy", "live · 22:15:00Z", False, False),
    ("degraded", "degraded", "checks failed · 22:15:00Z", True, False),
    ("degraded", "healthy", "live · 22:15:00Z", False, True),
])
def test_live_chip_reports_health_and_reloads_when_health_changes(page_status, status, text, down, reloaded):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the live script")
    script = render.LIVE_SCRIPT.replace("__REVISION__", REV).replace("__STATUS__", page_status)
    script = script.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    harness = HARNESS.replace("__LIVE_JS__", script).replace(
        "__MESSAGE__", json.dumps({"revision": REV, "status": status}))
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10,
                            encoding="utf-8")
    assert result.returncode == 0, result.stderr
    chip = json.loads(result.stdout)
    assert chip["text"] == text
    assert ("down" in chip["cls"].split()) == down
    assert chip["reloaded"] is reloaded


def test_collector_error_reloads_cached_page_as_degraded():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the live script")
    script = render.LIVE_SCRIPT.replace("__REVISION__", REV).replace("__STATUS__", "healthy")
    script = script.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    harness = HARNESS.replace("__LIVE_JS__", script).replace(
        "__MESSAGE__", json.dumps({"revision": "2026-09-14T22:16:00Z", "status": "error", "detail": "OSError"}))
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10,
                            encoding="utf-8")
    assert result.returncode == 0, result.stderr
    chip = json.loads(result.stdout)
    assert chip["text"] == "collector: OSError"
    assert "down" in chip["cls"].split()
    assert chip["reloaded"] is True


def _result(ok: bool) -> refresh.RefreshResult:
    notes = ([] if ok else [FAILURE]) + ["inline scripts: 1 checked, 0 with syntax errors"]
    return refresh.RefreshResult(revision=REV, body="<p>board</p>", snapshot={}, board_diff=diff.BoardDiff(),
                                 report=template.Report(), checks=validate.Result(ok=ok, notes=notes),
                                 served_revision=REV if ok else None)


def test_service_serves_a_failed_render_under_a_banner(tmp_path):
    service = SwimlaneService(Paths(tmp_path), collect=False, publish=False)
    service.publish(_result(ok=False))
    page = service.page.decode("utf-8")
    assert service.status["state"] == "degraded"
    assert "failed its own checks" in page and FAILURE in page
    assert page.index("render-alert") < page.index("<p>board</p>"), "the warning comes before the board"


def test_a_healthy_render_carries_no_banner(tmp_path):
    service = SwimlaneService(Paths(tmp_path), collect=False, publish=False)
    service.publish(_result(ok=True))
    assert "render-alert" not in service.page.decode("utf-8")


def test_live_refresh_persists_open_state_for_new_snapshot_panels():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the live script")
    script = render.LIVE_SCRIPT.replace("__REVISION__", REV).replace("__STATUS__", "healthy")
    script = script.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    harness = r"""
const details = [
  {id: "tracked-snapshot-details", tagName: "DETAILS", open: false},
  {id: "relation-snapshot-details", tagName: "DETAILS", open: true}
];
let saved = null, reloaded = false, source;
globalThis.document = {createElement: () => ({appendChild() {}}), body: {appendChild() {}},
  querySelectorAll: selector => selector === "details[id]" ? details : []};
globalThis.window = {scrollY: 0, scrollTo() {}};
globalThis.sessionStorage = {getItem: () => null, setItem: (_, value) => { saved = JSON.parse(value); }, removeItem() {}};
globalThis.EventSource = function () { source = this; };
globalThis.location = {reload() { reloaded = true; }};
globalThis.setInterval = () => 0;
SCRIPT
source.onmessage({data: JSON.stringify({revision: "2026-09-14T22:16:00Z", status: "healthy"})});
console.log(JSON.stringify({saved, reloaded}));
""".replace("SCRIPT", script)
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10,
                            encoding="utf-8")
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["reloaded"] is True
    assert state["saved"]["closed"] == ["tracked-snapshot-details"]
    assert state["saved"]["open"] == ["relation-snapshot-details"]


class OrphanChildClient:
    """ADO lists an open child (201) under a card but omits its detail, so the
    rendered children cannot match the live count and the checks fail."""

    server_date = "Tue, 15 Sep 2026 22:16:00 GMT"

    def wiql(self, query):
        return [201] if "[System.Parent] IN" in query else []

    def batch(self, ids):
        return [{"id": i, "fields": {"System.WorkItemType": "Feature", "System.State": "New",
                                     "System.Title": f"item {i}"}} for i in ids if i != 201]


class HealthyClient:
    server_date = "Mon, 14 Sep 2026 22:15:00 GMT"

    def wiql(self, query):
        return []

    def batch(self, ids):
        states = {100: "Closed", 200: "New", 300: "Active", 400: "New"}
        return [{
            "id": item_id,
            "fields": {
                "System.WorkItemType": "Feature",
                "System.State": states.get(item_id, "New"),
                "System.Title": f"accepted item {item_id}",
                "System.ChangedDate": "2026-09-14T22:00:00Z",
            },
        } for item_id in ids]


class FailedClient:
    def wiql(self, _query):
        raise OSError("connection reset")

    def batch(self, _ids):
        raise AssertionError("batch must not run after the WIQL error")


def test_refresh_writes_a_failed_static_page_under_a_banner(board, tmp_path):
    source = tmp_path / "board.html"
    source.write_text(board, encoding="utf-8")
    paths = get_paths(tmp_path / "home")
    result = refresh.run(paths, client=OrphanChildClient(), source=source)
    assert not result.ok
    static = paths.html.read_text(encoding="utf-8")
    assert "failed its own checks" in static and "!= live open children" in static
    assert "no retained snapshot passed current validation" in static
    assert 'var SNAPSHOT = "2026-09-01"' in static
    assert not paths.snapshot.exists()
    assert not paths.states.exists()
    assert not paths.artifact.exists()
    assert result.served_revision is None


def test_failed_refresh_preserves_and_serves_revalidated_last_good_state(board, tmp_path):
    source = tmp_path / "board.html"
    source.write_text(board, encoding="utf-8")
    paths = get_paths(tmp_path / "home")

    accepted = refresh.run(paths, client=HealthyClient(), source=source)
    assert accepted.ok and accepted.served_revision == REV
    snapshot_before = paths.snapshot.read_bytes()
    states_before = paths.states.read_bytes()
    artifact_before = paths.artifact.read_bytes()
    changes_before = paths.changes.read_bytes()

    rejected = refresh.run(paths, client=OrphanChildClient(), source=source)
    assert not rejected.ok
    assert rejected.revision != REV
    assert rejected.served_revision == REV
    assert "showing last-good snapshot " + REV in rejected.health_detail
    assert "latest ADO snapshot failed validation" in rejected.health_detail
    assert paths.snapshot.read_bytes() == snapshot_before
    assert paths.states.read_bytes() == states_before
    assert paths.artifact.read_bytes() == artifact_before
    assert paths.changes.read_bytes() == changes_before
    assert not paths.previous.exists(), "the failed pull must not become the diff baseline"

    static = paths.html.read_text(encoding="utf-8")
    assert 'var SNAPSHOT = "2026-09-14"' in static
    assert 'var REFRESHED = "22:15 UTC"' in static
    assert "showing last-good snapshot 2026-09-14T22:15:00Z" in static
    assert "failed its own checks" in static
    assert "2026-09-15" not in static

    service = SwimlaneService(paths, collect=False, publish=False)
    service.publish(accepted)
    last_good_diff = service.last_diff
    service.publish(rejected)
    live = service.page.decode("utf-8")
    assert 'var SNAPSHOT = "2026-09-14"' in live
    assert "showing last-good snapshot 2026-09-14T22:15:00Z" in live
    assert service.status["state"] == "degraded"
    assert service.status["revision"] == rejected.revision
    assert service.status["servedRevision"] == REV
    assert service.status["lastGoodRevision"] == REV
    assert service.last_diff == last_good_diff


def test_collector_exception_degrades_static_cache_and_recovers(board, tmp_path, monkeypatch):
    source = tmp_path / "board.html"
    source.write_text(board, encoding="utf-8")
    paths = get_paths(tmp_path / "home")
    monkeypatch.setattr(refresh, "template_path", lambda: source)

    accepted = refresh.run(paths, client=HealthyClient(), source=source)
    assert accepted.ok and accepted.served_revision == REV
    accepted_files = (paths.snapshot, paths.states, paths.artifact, paths.changes)
    preserved = {path: path.read_bytes() for path in accepted_files}

    service = SwimlaneService(paths, collect=False, publish=False)
    service.publish(accepted)
    service.client = FailedClient()
    original_fail = service.fail

    def fail_once(exc):
        original_fail(exc)
        service.stop()

    service.fail = fail_once
    monkeypatch.setattr("ado_swimlane.service.ERROR_BACKOFF_S", 0)
    service._collector()

    static = paths.html.read_text(encoding="utf-8")
    assert "This page is degraded." in static
    assert "latest ADO collection failed (OSError)" in static
    assert f"showing last-good snapshot {REV}" in static
    assert 'var SNAPSHOT = "2026-09-14"' in static
    assert 'var REFRESHED = "22:15 UTC"' in static
    assert all(path.read_bytes() == preserved[path] for path in accepted_files)
    assert service.status["state"] == "error"
    assert service.status["servedRevision"] == REV
    assert "latest ADO collection failed (OSError)" in service.page.decode("utf-8")

    recovered = refresh.run(paths, client=HealthyClient(), source=source)
    assert recovered.ok
    service.publish(recovered)
    assert "render-alert" not in paths.html.read_text(encoding="utf-8")
    assert service.status["state"] == "healthy"
    assert "render-alert" not in service.page.decode("utf-8")


def test_refresh_cli_labels_rejected_attempt_and_preserved_artifact(tmp_path, monkeypatch, capsys):
    paths = get_paths(tmp_path)
    paths.artifact.write_text("<p>accepted body</p>", encoding="utf-8")
    rejected = _result(ok=False)
    rejected.served_revision = REV
    rejected.health_detail = "latest ADO snapshot failed validation; showing last-good snapshot " + REV
    rejected.body = "<p>accepted body</p>"
    rejected.board_diff.changes.append(diff.Change(1, "Feature", "attempted", "state", "New", "Closed"))
    monkeypatch.setattr(cli.refresh_mod, "run", lambda *_args, **_kwargs: rejected)
    args = argparse.Namespace(home=tmp_path, template=None)

    assert cli._run_refresh(args) == 2
    output = capsys.readouterr().out
    assert "attempted render rejected" in output
    assert "attempted diff is not recorded or served" in output
    assert "served last-good snapshot: " + REV in output
    assert "preserved last-good Artifact body" in output
    assert "attempted\tNew -> Closed" not in output
