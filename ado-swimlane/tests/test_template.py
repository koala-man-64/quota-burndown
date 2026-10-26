from ado_swimlane import render, template
from conftest import item, snapshot

PROSE = "Curated prose that no refresh may touch"


def kids_for(*items):
    return {200: list(items)}


def test_reads_cards_children_and_known_ids(board):
    assert template.card_ids(board) == [100, 200, 300, 400]
    assert template.read_children(board) == {"c200": [201]}
    assert template.known_ids(board) == [100, 200, 201, 300, 400]


def test_effective_state_falls_back_to_styling(board):
    cards = {c.dom_id: c for c in template.read_cards(board)}
    assert cards["c100"].effective_state == "Closed"   # styled done, no data-state
    assert cards["c200"].effective_state == "New"      # neither
    assert cards["c300"].effective_state == "Active"   # explicit
    assert cards["c400"].state == template.AGGREGATE


def test_inject_writes_state_children_and_dates_but_not_prose(board):
    snap = snapshot(
        [item(100, "Feature", "Closed"), item(200, "Feature", "Active"),
         item(300, "Feature", "Active"), item(400, "Epic", "New"),
         item(201, parent=200, state="Active")],
        open_ef=[200, 300],
    )
    out, report = template.inject(
        board, snap, snapshot_date="2026-09-14", delta_from="09-13",
        refreshed="22:15 UTC", kids=kids_for(item(201, parent=200, state="Active")),
    )
    assert ("c200", "New", "Active") in report.states
    assert report.children_keys == 1 and report.children_total == 1
    cards = {c.dom_id: c for c in template.read_cards(out)}
    assert cards["c200"].state == "Active"
    assert cards["c400"].state == template.AGGREGATE, "aggregate cards are never restated"
    assert '"2026-09-14"' in out and '"09-13"' in out and '"22:15 UTC"' in out
    assert "&middot; 2 open epics/features" in out
    assert PROSE in out, "prose is not machine-writable"


def test_inject_is_idempotent(board):
    snap = snapshot([item(100, "Feature", "Closed"), item(200, "Feature", "Active"),
                     item(300, "Feature", "Active"), item(400, "Epic", "New")])
    kwargs = dict(snapshot_date="2026-09-14", delta_from="09-13", refreshed="22:15 UTC", kids={})
    once, _ = template.inject(board, snap, **kwargs)
    twice, report = template.inject(once, snap, **kwargs)
    assert once == twice
    assert report.states == [], "a second injection of the same snapshot changes nothing"


def test_styling_disagreement_is_reported_not_rewritten(board):
    """A green 'closed 09-10' card that is open again is a prose problem."""
    snap = snapshot([item(100, "Feature", "Active"), item(200, "Feature", "New"),
                     item(300, "Feature", "Active"), item(400, "Epic", "New")])
    out, report = template.inject(board, snap, snapshot_date="2026-09-14", delta_from="09-13",
                                  refreshed="22:15 UTC", kids={})
    assert ("c100", "done", "Active") in report.mismatches
    assert 'class="card done" id="c100"' in out, "styling is left for a person to settle"


def test_missing_live_detail_is_reported(board):
    out, report = template.inject(board, snapshot([]), snapshot_date="2026-09-14",
                                  delta_from="09-13", refreshed="22:15 UTC", kids={})
    assert set(report.missing) == {"c100", "c200", "c300"}
    assert out.count("data-state") == board.count("data-state")


def test_children_escaping_survives_quotes_and_script_ends(board):
    nasty = item(201, parent=200, title='He said "</script>" \\ then left')
    out, _ = template.inject(board, snapshot([item(200, "Feature", "New")]),
                             snapshot_date="2026-09-14", delta_from="09-13",
                             refreshed="22:15 UTC", kids=kids_for(nasty))
    assert "</script>" not in out.split("var CHILDREN")[1].split("};")[0]
    assert template.read_children(out) == {"c200": [201]}


def test_delta_window_only_moves_when_the_day_rolls_over(board):
    assert render._delta_window(board, "2026-09-01") == "08-31"   # same day: keep
    assert render._delta_window(board, "2026-09-14") == "09-01"   # rolled: previous snapshot


def test_a_true_zero_open_count_is_written(board):
    """An empty openEF is the query's real answer; leaving the template's number
    in place would claim work that is no longer open."""
    out, _ = template.inject(board, snapshot([item(200, "Feature", "Closed")], open_ef=[]),
                             snapshot_date="2026-09-14", delta_from="09-13", refreshed="22:15 UTC", kids={})
    assert "&middot; 0 open epics/features" in out


def test_a_snapshot_without_the_query_leaves_the_count_alone(board):
    snap = snapshot([item(200, "Feature", "New")])
    del snap["openEF"]
    out, _ = template.inject(board, snap, snapshot_date="2026-09-14", delta_from="09-13",
                             refreshed="22:15 UTC", kids={})
    assert "&middot; 7 open epics/features" in out
