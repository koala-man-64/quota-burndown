"""A render that failed its own checks must not look healthy.

The page is still served, so a reader can see what the board said, but the live
chip and a banner say that what follows is unverified.
"""
import json
import shutil
import subprocess

import pytest

from ado_swimlane import diff, refresh, render, template, validate
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


@pytest.mark.parametrize("status,text,down", [
    ("degraded", "checks failed · 22:15:00Z", True),
    ("healthy", "live · 22:15:00Z", False),
])
def test_live_chip_reports_failed_checks_instead_of_live(status, text, down):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the live script")
    script = render.LIVE_SCRIPT.replace("__REVISION__", REV)
    script = script.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    harness = HARNESS.replace("__LIVE_JS__", script).replace(
        "__MESSAGE__", json.dumps({"revision": REV, "status": status}))
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10,
                            encoding="utf-8")
    assert result.returncode == 0, result.stderr
    chip = json.loads(result.stdout)
    assert chip["text"] == text
    assert ("down" in chip["cls"].split()) == down
    assert not chip["reloaded"], "the same revision never reloads"


def _result(ok: bool) -> refresh.RefreshResult:
    notes = ([] if ok else [FAILURE]) + ["inline scripts: 1 checked, 0 with syntax errors"]
    return refresh.RefreshResult(revision=REV, body="<p>board</p>", snapshot={}, board_diff=diff.BoardDiff(),
                                 report=template.Report(), checks=validate.Result(ok=ok, notes=notes))


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


class OrphanChildClient:
    """ADO lists an open child (201) under a card but omits its detail, so the
    rendered children cannot match the live count and the checks fail."""

    server_date = "Mon, 14 Sep 2026 22:15:00 GMT"

    def wiql(self, query):
        return [201] if "[System.Parent] IN" in query else []

    def batch(self, ids):
        return [{"id": i, "fields": {"System.WorkItemType": "Feature", "System.State": "New",
                                     "System.Title": f"item {i}"}} for i in ids if i != 201]


def test_refresh_writes_a_failed_static_page_under_a_banner(board, tmp_path):
    source = tmp_path / "board.html"
    source.write_text(board, encoding="utf-8")
    paths = get_paths(tmp_path / "home")
    result = refresh.run(paths, client=OrphanChildClient(), source=source)
    assert not result.ok
    static = paths.html.read_text(encoding="utf-8")
    assert "failed its own checks" in static and "!= live open children" in static
    # The artifact stays the bare body; publishing refuses a failed render instead.
    assert "render-alert" not in paths.artifact.read_text(encoding="utf-8")
