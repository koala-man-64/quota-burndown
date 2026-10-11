import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


BOARD = """<title>Board</title>
<div class="kfacts">
  <details class="kfact par"><summary><b>Changed</b></summary><span class="body">Curated prose that no refresh may touch: F2097 went Active with a gate open.</span></details>
</div>
<div class="boardwrap">
<div class="board" id="board">
  <div class="phasehead"></div>
  <div class="phasehead"><span class="pnum">P0</span><span class="pname">Unblock</span></div>
  <div class="phasehead"><span class="pnum">P1</span><span class="pname">Foundations</span></div>
  <div class="phasehead"><span class="pnum">P2</span><span class="pname">Evidence</span></div>
  <div class="phasehead"><span class="pnum">P3</span><span class="pname">Predecessors</span></div>
  <div class="phasehead"><span class="pnum">P4</span><span class="pname">Foundations2</span></div>
  <div class="phasehead"><span class="pnum">P5</span><span class="pname">Workstation</span></div>
  <div class="phasehead"><span class="pnum">P6</span><span class="pname">Engine</span></div>

  <div class="lanelabel">Lane one<small>Epic 1</small></div>
  <div class="cell">
    <div class="card done" id="c100"><span class="ids">F100</span><div class="t">Closed thing</div></div>
  </div>
  <div class="cell">
    <div class="card" id="c200"><span class="ids">F200</span><div class="t">Open thing</div></div>
  </div>
  <div class="cell"></div>
  <div class="cell"></div>
  <div class="cell"></div>
  <div class="cell"></div>
  <div class="cell"></div>

  <div class="lanelabel">Lane two<small>Epic 2</small></div>
  <div class="cell"></div>
  <div class="cell"></div>
  <div class="cell"></div>
  <div class="cell"></div>
  <div class="cell"></div>
  <div class="cell"></div>
  <div class="cell">
    <div class="card aa2" id="c300" data-state="Active"><span class="ids">F300</span><div class="t">Engine</div></div>
    <div class="card" id="c400" data-state="none"><span class="ids">E400</span><div class="t">Bucket</div></div>
  </div>
</div>
</div>
<p class="sub">Project, snapshot <span id="snap-date">...</span> &middot; 7 open epics/features.</p>
<script>
(function () {
  var SNAPSHOT = "2026-09-01";
  var DELTA_FROM = "08-31";
  var REFRESHED = "09:00 UTC";
  var CHILDREN = {
    c200: [[201,"US","New","Story two-oh-one"]],
  };
})();
</script>
"""


def item(wid, wtype="User Story", state="New", parent=None, title="t", changed="2026-09-01T00:00:00Z"):
    return {"id": wid, "type": wtype, "state": state, "title": title,
            "parent": parent, "changed": changed, "tags": "", "iter": ""}


def snapshot(items, *, cards=(100, 200, 300, 400), open_ef=(), open_bugs=(), children=(),
             server="Mon, 14 Sep 2026 22:15:00 GMT", changed=()):
    return {
        "serverDate": server,
        "since": "2026-09-01T00:00:00Z",
        "cardIds": list(cards),
        "openEF": list(open_ef),
        "openBugs": list(open_bugs),
        "changed": list(changed),
        "children": list(children),
        "items": list(items),
    }


@pytest.fixture
def board():
    return BOARD
