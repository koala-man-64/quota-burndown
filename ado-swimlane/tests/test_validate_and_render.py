import json

from ado_swimlane import ado, render, template, validate
from conftest import item, snapshot


def test_walk_places_every_card_in_a_lane_and_phase(board):
    rows = {row["id"]: row for row in validate.walk(board)}
    assert set(rows) == {"c100", "c200", "c300", "c400"}
    assert rows["c100"]["lane"] == "Lane one" and rows["c100"]["phase"] == "P0"
    assert rows["c300"]["lane"] == "Lane two" and rows["c300"]["phase"] == "P6"


def test_check_counts_match_the_template(board):
    result = validate.check(board, expected_children=1)
    assert result.ok, result.notes
    assert result.totals["cards"] == 4
    assert result.totals["openChildren"] == 1
    assert result.totals["cardStates"]["Closed"] == 1


def test_check_fails_when_children_disagree_with_live(board):
    result = validate.check(board, expected_children=2)
    assert not result.ok
    assert any("!= live open children" in note for note in result.notes)


def test_check_fails_when_children_list_closed_work(board):
    stale = board.replace('[[201,"US","New"', '[[201,"US","Closed"')
    result = validate.check(stale)
    assert not result.ok
    assert any("lists closed work" in note for note in result.notes)


def test_check_fails_when_a_card_state_disagrees_with_live(board):
    snap = snapshot([item(100, "Feature", "Closed"), item(200, "Feature", "Active"),
                     item(300, "Feature", "Active")])
    result = validate.check(board, snap)
    assert not result.ok
    assert any("c200 page=New live=Active" in note for note in result.notes)


def test_check_fails_on_a_children_key_without_a_card(board):
    orphan = board.replace("c200: [[201", "c999: [[201")
    assert not validate.check(orphan).ok


def test_rendered_page_passes_its_own_checks(board):
    snap = snapshot(
        [item(100, "Feature", "Closed"), item(200, "Feature", "Active"),
         item(300, "Feature", "Active"), item(400, "Epic", "New"),
         item(201, parent=200, state="Active")],
        open_ef=[200, 300], children=[201],
    )
    body, _ = render.compose(board, snap, kids=ado.open_children_of(snap))
    result = validate.check(body, snap, expected_children=1)
    assert result.ok, result.notes


def test_live_page_carries_the_revision_and_static_page_reloads(board):
    live = render.live_page("<p>x</p>", "2026-09-14T22:15:00Z")
    assert "2026-09-14T22:15:00Z" in live and "EventSource" in live
    assert live.startswith("<!doctype html>") and live.rstrip().endswith("</body></html>")
    static = render.static_page("<p>x</p>", refresh_s=90)
    assert 'http-equiv="refresh" content="90"' in static and "EventSource" not in static
    # The artifact is the bare fragment: publishing supplies the document shell.
    assert render.artifact_page("<p>x</p>") == "<p>x</p>"


def test_server_moment_prefers_the_ado_clock():
    assert render.revision_of({"serverDate": "Mon, 14 Sep 2026 22:15:00 GMT"}) == "2026-09-14T22:15:00Z"
    # The PowerShell pipeline this replaced stored the whole header line.
    assert render.revision_of({"serverDate": "Date: Mon, 14 Sep 2026 22:15:00 GMT"}) == "2026-09-14T22:15:00Z"
    assert render.revision_of({}).endswith("Z")


def test_open_children_only_lists_open_work_of_known_cards():
    snap = snapshot(
        [item(10, parent=200, state="New"), item(11, parent=200, state="Closed"),
         item(12, parent=999, state="New")],
        cards=(200,),
    )
    kids = ado.open_children_of(snap)
    assert [k["id"] for k in kids[200]] == [10]
    assert 999 not in kids


def test_wiql_asks_for_time_precision():
    """Without it ADO runs in date precision and rejects any non-midnight bound."""
    seen = {}

    class FakeResponse:
        headers = {"Date": "Mon, 14 Sep 2026 22:15:00 GMT"}

        def read(self):
            return json.dumps({"workItems": [{"id": 5}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_open(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return FakeResponse()

    client = ado.Client(token="t", expiry=2 ** 40, _urlopen=fake_open)
    assert client.wiql("SELECT [System.Id] FROM WorkItems") == [5]
    assert "timePrecision=true" in seen["url"]
    assert client.server_date == "Mon, 14 Sep 2026 22:15:00 GMT"


def test_check_fails_when_a_card_has_no_live_record(board):
    """The collector asks for every card and ADO omits ids it cannot return, so a
    card missing from the snapshot was deleted, mistyped or is unreadable: its
    badge shows a state nobody checked."""
    snap = snapshot([item(100, "Feature", "Closed"), item(300, "Feature", "Active")])
    result = validate.check(board, snap)
    assert not result.ok
    assert any("no live record" in note and "c200" in note for note in result.notes)
