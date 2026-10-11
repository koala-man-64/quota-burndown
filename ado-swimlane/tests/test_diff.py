from ado_swimlane import diff
from conftest import item, snapshot


def test_state_change_is_reported_with_both_sides():
    before = snapshot([item(1, "Feature", "New")], open_ef=[1])
    after = snapshot([item(1, "Feature", "Active")], open_ef=[1])
    result = diff.compare(before, after)
    assert [(c.id, c.kind, c.before, c.after) for c in result.changes] == [(1, "state", "New", "Active")]


def test_closing_is_both_a_state_change_and_a_closure():
    before = snapshot([item(1, "Bug", "Active")], open_bugs=[1])
    after = snapshot([item(1, "Bug", "Closed")], open_bugs=[])
    result = diff.compare(before, after)
    kinds = [(c.id, c.kind) for c in result.changes]
    assert kinds == [(1, "state")], "leaving the open set is the same event, reported once"
    assert [c.id for c in result.closed] == [1]


def test_items_falling_out_of_the_fetch_window_are_not_changes():
    """The regression this module exists for.

    Each pull fetches the union of several queries, and the "changed since"
    window shrinks as polling gets more frequent. An item that was fetched only
    because it fell inside a wide cold-start window, and is not fetched by the
    next pull, has not moved - reporting it as vanished invented ten changes out
    of forty-six seconds of quiet.
    """
    before = snapshot([item(1, "Feature", "New", parent=None), item(99, "Task", "Active")],
                      open_ef=[1], changed=[1, 99])
    after = snapshot([item(1, "Feature", "New", parent=None)], open_ef=[1], changed=[1])
    result = diff.compare(before, after)
    assert result.changes == []
    assert result.is_quiet()


def test_leaving_a_tracked_set_is_a_change():
    before = snapshot([item(7, "Bug", "Active")], open_bugs=[7])
    after = snapshot([], open_bugs=[])
    result = diff.compare(before, after)
    assert [(c.id, c.kind, c.before) for c in result.changes] == [(7, "vanished", "Active")]


def test_new_membership_is_a_change_but_a_first_run_is_not():
    before = snapshot([], open_bugs=[])
    after = snapshot([item(8, "Bug", "New")], open_bugs=[8])
    assert [(c.id, c.kind) for c in diff.compare(before, after).changes] == [(8, "appeared")]
    # Cold start: no previous tracked scope at all, so nothing is "new".
    assert diff.compare(None, after).changes == []


def test_children_movement_is_attributed_to_the_card():
    before = snapshot([item(2, "Feature", "Active"), item(20, parent=2, state="New")],
                      cards=(2,), children=[20])
    after = snapshot([item(2, "Feature", "Active"), item(21, parent=2, state="New")],
                     cards=(2,), children=[21])
    result = diff.compare(before, after)
    assert result.children_added == {2: [21]}
    assert result.children_gone == {2: [20]}


def test_standing_counts_and_pools():
    snap = snapshot(
        [item(1, "Feature", "Active"), item(5, "Bug", "Resolved"), item(6, "Bug", "New")],
        open_ef=[1], open_bugs=[5, 6],
    )
    result = diff.compare(snap, snap)
    assert result.counts["openEF"] == 1 and result.counts["openBugs"] == 2
    assert result.resolved_not_closed == [5]
    assert result.unparented == [5, 6]


def test_lines_render_without_a_previous_snapshot():
    text = "\n".join(diff.compare(None, snapshot([item(1, "Feature", "New")])).lines())
    assert "nothing moved" in text and "STANDING" in text


def test_a_single_transition_is_caught_even_when_the_item_falls_out_of_the_window():
    """The regression this fix exists for: a poll that caught ten real closures
    reported one, because each item had moved only once and so was absent from
    the previous pull's fetch window."""
    known = {1: item(1, "Task", "Active", parent=9)}
    before = snapshot([], open_ef=[], changed=[])
    after = snapshot([item(1, "Task", "Closed", parent=9)], changed=[1])
    result = diff.compare(before, after, known)
    assert [(c.id, c.kind, c.before, c.after) for c in result.changes] == [(1, "state", "Active", "Closed")]
    assert [c.id for c in result.closed] == [1]


def test_memory_does_not_resurrect_the_fetch_window_false_positives():
    known = {99: item(99, "Task", "Active")}
    before = snapshot([item(99, "Task", "Active")], changed=[99])
    after = snapshot([], changed=[])
    # Still fetched-nowhere, still unchanged: not a change, and never "vanished"
    # because a bare fetch union is not a tracked set.
    assert diff.compare(before, after, known).changes == []
