import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ado_swimlane import ado, publish, render, template, validate


BOARD = Path(__file__).resolve().parent.parent / "board" / "board.html"


def _snapshot(board_html):
    cards = template.read_cards(board_html)
    card_ids = [card.id for card in cards]
    items = []
    for card in cards:
        state = card.state or ("Closed" if card.styled_done else "New")
        if state == "Unknown":
            state = "New"
        items.append({
            "id": card.id, "revision": 7,
            "type": "User Story" if card.id == 4049 else "Feature",
            "state": state, "title": f"Item {card.id}",
            "parent": 2439 if card.id == 4049 else None,
            "changed": "2026-10-29T06:18:03Z", "tags": "", "iter": "AA\\Current",
            "priority": 2, "priorityReturned": True,
            "assignedTo": "Owner Example", "assignedToReturned": True,
            "iterationReturned": True, "relations": [], "relationsFetched": True,
        })
    items.extend([
        {"id": 2439, "revision": 3, "type": "User Story", "state": "Active",
         "title": "workload/identity follow-up", "parent": 2095, "changed": "2026-10-29T06:00:00Z",
         "priority": 2, "priorityReturned": True, "assignedTo": "Owner Example",
         "assignedToReturned": True, "iterationReturned": True, "iter": "AA\\Current"},
        {"id": 4051, "revision": 2, "type": "Task", "state": "New", "title": "Canary preparation",
         "parent": 4049, "changed": "2026-10-29T06:00:00Z", "priority": 1,
         "priorityReturned": True, "assignedTo": None, "assignedToReturned": True,
         "iterationReturned": True, "iter": "AA\\Current"},
    ])
    tracked = template.tracked_items(board_html)
    return {
        "serverDate": "Thu, 29 Oct 2026 06:18:03 GMT",
        "since": "2026-10-29T06:00:00Z",
        "cardIds": card_ids,
        "openEF": [card.id for card in cards if card.state != "none"],
        "openBugs": [], "changed": [], "children": [2439, 4051],
        "trackedItems": tracked,
        "dependencies": [{"predecessor": 2095, "successor": 2097, "observedBy": [2095]}],
        "queryScope": {
            "details": "selected work-item IDs, not a project-wide detail census",
            "trackedIds": [4049], "trackedReturnedIds": [4049], "trackedMissingIds": [],
            "relationExpand": "Relations", "cardRelationReturnedIds": card_ids,
            "cardRelationMissingIds": [], "detailMissingIds": [],
        },
        "items": items,
    }


def test_raw_template_is_explicitly_unverified_and_has_no_amber_claims():
    board = BOARD.read_text(encoding="utf-8-sig")
    assert 'var SNAPSHOT = "Not refreshed"' in board
    assert 'var REFRESHED = "no ADO response"' in board
    assert 'id="snapshot-warning"' in board
    assert "No verified ADO relation snapshot" in board
    assert 'var CHILDREN = {};' in board
    assert not any(is_live for _before, _after, is_live in template.read_links(board))
    assert 4049 in template.card_ids(board)
    assert 4049 in template.known_ids(board)


def test_rendered_flow_tracks_4049_children_and_generates_only_live_amber_links():
    board = BOARD.read_text(encoding="utf-8-sig")
    # Simulate an old curated amber literal: it must be discarded unless the
    # captured snapshot independently contains that edge.
    board = board.replace(
        "  var links = [\n",
        '  var links = [\n    ["c1964", "c2098", true],\n',
        1,
    )
    snapshot = _snapshot(board)
    kids = ado.open_children_of(snapshot)
    body, report = render.compose(board, snapshot, kids=kids)
    result = validate.check(body, snapshot, expected_children=2)

    assert result.ok, result.notes
    assert report.legacy_gate_links == 1
    assert template.read_children(body)["c4049"] == [4051]
    live_links = {(before, after) for before, after, is_live in template.read_links(body) if is_live}
    assert live_links == {(2095, 2097)}
    assert (1964, 2098, True) not in template.read_links(body)
    assert 'id="tracked-snapshot"' in body
    assert 'id="tracked-snapshot-details"' in body
    assert 'id="relation-snapshot-details"' in body
    assert "parent 2439 (expected 2439)" in body
    assert "owner Owner Example" in body
    assert 'data-priority="2"' in body and 'data-revision="7"' in body
    assert "not a project-wide detail census" in snapshot["queryScope"]["details"]


def test_off_view_relations_remain_visible_without_drawing_or_fetching_endpoint_details():
    board = BOARD.read_text(encoding="utf-8-sig")
    snapshot = _snapshot(board)
    snapshot["dependencies"] = [
        {"predecessor": 2095, "successor": 2097, "observedBy": [2095]},
        {"predecessor": 2095, "successor": 77777, "observedBy": [2095]},
    ]
    body, report = render.compose(board, snapshot, kids=ado.open_children_of(snapshot))
    checks = validate.check(body, snapshot, expected_children=2)

    assert checks.ok, checks.notes
    assert report.live_gate_links == 1 and report.off_view_links == 1
    assert {(a, b) for a, b, live in template.read_links(body) if live} == {(2095, 2097)}
    assert {(a, b) for a, b, _sources in template.read_formal_links(body)} == {
        (2095, 2097), (2095, 77777)
    }
    assert "#77777" in body and "off-view endpoint(s) #77777" in body
    assert "not fetched solely" in body
    assert 77777 not in {item["id"] for item in snapshot["items"]}


def test_relation_changes_change_render_and_publish_fingerprint():
    board = BOARD.read_text(encoding="utf-8-sig")
    first = _snapshot(board)
    second = {**first, "dependencies": [
        {"predecessor": 2100, "successor": 2098, "observedBy": [2100]}
    ]}
    first_body, _ = render.compose(board, first, kids=ado.open_children_of(first))
    second_body, _ = render.compose(board, second, kids=ado.open_children_of(second))

    assert first_body != second_body
    assert publish.fingerprint(first_body) != publish.fingerprint(second_body)


def test_tracked_parent_mismatch_and_missing_relation_coverage_fail_closed():
    board = BOARD.read_text(encoding="utf-8-sig")
    snapshot = _snapshot(board)
    snapshot["items"] = [
        {**item, "parent": 999} if item["id"] == 4049 else item
        for item in snapshot["items"]
    ]
    body, _ = render.compose(board, snapshot, kids=ado.open_children_of(snapshot))
    result = validate.check(body, snapshot, expected_children=2)
    assert not result.ok
    assert any("4049 parent=999 expected=2439" in note for note in result.notes)

    snapshot = _snapshot(board)
    snapshot["queryScope"]["cardRelationMissingIds"] = [4049]
    body, _ = render.compose(board, snapshot, kids=ado.open_children_of(snapshot))
    result = validate.check(body, snapshot, expected_children=2)
    assert not result.ok
    assert any("dependency relations not returned" in note for note in result.notes)


def test_snapshot_warning_shows_unknown_stale_and_fresh_states():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the freshness indicator")
    board = BOARD.read_text(encoding="utf-8-sig")
    start = board.index("  var snapshotWarning = document.getElementById")
    end = board.index('  document.getElementById("snap-date")', start)
    snippet = board[start:end]
    harness = r"""
const fixedNow = Date.parse("2026-10-29T07:00:00Z");
Date.now = () => fixedNow;
const SNAPSHOT = "Not refreshed";
const REFRESHED = "no ADO response";
const warning = {hidden: true, textContent: ""};
const document = {getElementById: id => id === "snapshot-warning" ? warning : {textContent: ""}};
const timers = [];
const window = {setInterval: fn => timers.push(fn)};
SNIPPET
console.log(JSON.stringify({hidden: warning.hidden, text: warning.textContent, timers: timers.length}));
""".replace("SNIPPET", snippet)
    for snapshot, refreshed, expected_hidden, marker in [
        ('"Not refreshed"', '"no ADO response"', False, "No verified ADO response"),
        ('"2026-10-29"', '"06:00 UTC"', False, "60 minutes old"),
        ('"2026-10-29"', '"06:58 UTC"', True, ""),
    ]:
        source = harness.replace('const SNAPSHOT = "Not refreshed";', f"const SNAPSHOT = {snapshot};")
        source = source.replace('const REFRESHED = "no ADO response";', f"const REFRESHED = {refreshed};")
        run = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=10)
        assert run.returncode == 0, run.stderr
        payload = json.loads(run.stdout)
        assert bool(payload["hidden"]) is expected_hidden
        assert marker in payload["text"]
