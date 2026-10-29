"""What changed between two snapshots.

The rendered page shows current state; this shows movement. It is the input a
person (or a Claude session) turns into the delta panel's prose, and it is
deliberately conservative: it reports observations, never conclusions. "F2097
went Active" is here; "F2097 jumped its gate" is not, because that judgement
needs the gate list, and that lives in the curated prose.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ado import open_children_of
from .config import CLOSED_STATES, is_open


@dataclass
class Change:
    id: int
    type: str | None
    title: str
    kind: str              # state | appeared | vanished
    before: str | None = None
    after: str | None = None
    parent: int | None = None
    changed: str | None = None


@dataclass
class FieldChange:
    id: int
    field: str
    before: str | int | None
    after: str | int | None


@dataclass
class BoardDiff:
    since: str | None = None
    until: str | None = None
    changes: list[Change] = field(default_factory=list)
    closed: list[Change] = field(default_factory=list)
    field_changes: list[FieldChange] = field(default_factory=list)
    dependencies_added: list[tuple[int, int]] = field(default_factory=list)
    dependencies_gone: list[tuple[int, int]] = field(default_factory=list)
    dependencies_scope_added: list[tuple[int, int]] = field(default_factory=list)
    dependencies_scope_changed: list[tuple[int, int]] = field(default_factory=list)
    children_added: dict[int, list[int]] = field(default_factory=dict)
    children_gone: dict[int, list[int]] = field(default_factory=dict)
    unparented: list[int] = field(default_factory=list)
    resolved_not_closed: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["changes"] = [asdict(c) for c in self.changes]
        payload["closed"] = [asdict(c) for c in self.closed]
        payload["field_changes"] = [asdict(c) for c in self.field_changes]
        return payload

    def is_quiet(self) -> bool:
        return not (
            self.changes or self.field_changes or self.dependencies_added
            or self.dependencies_gone or self.dependencies_scope_added
            or self.dependencies_scope_changed or self.children_added or self.children_gone
        )

    def lines(self) -> list[str]:
        """Human-readable report, the shape the old analyze-board.js printed."""
        out = [f"=== CHANGES {self.since or '?'} -> {self.until or '?'}"]
        for change in self.changes:
            if change.kind == "state":
                out.append(f"  {change.id}\t{change.type}\t{change.before} -> {change.after}\t{change.title[:60]}")
            else:
                out.append(f"  {change.id}\t{change.type}\t{change.kind} ({change.after})\tparent={change.parent or '-'}\t{change.title[:60]}")
        if self.is_quiet():
            out.append("  (nothing moved)")
        if self.children_added or self.children_gone:
            out.append("=== CHILDREN")
            for card, ids in sorted(self.children_added.items()):
                out.append(f"  c{card} + {', '.join(str(i) for i in ids)}")
            for card, ids in sorted(self.children_gone.items()):
                out.append(f"  c{card} - {', '.join(str(i) for i in ids)}")
        if (self.dependencies_added or self.dependencies_gone or self.dependencies_scope_added
                or self.dependencies_scope_changed):
            out.append("=== RECORDED DEPENDENCIES")
            for before, after in self.dependencies_added:
                out.append(f"  + {before} -> {after}")
            for before, after in self.dependencies_gone:
                out.append(f"  - {before} -> {after}")
            for before, after in self.dependencies_scope_added:
                out.append(f"  ~ now captured {before} -> {after} (not evidence the relation was added)")
            for before, after in self.dependencies_scope_changed:
                out.append(f"  ? no longer comparable {before} -> {after} (relation source not captured this poll)")
        if self.field_changes:
            out.append("=== SCHEDULING / OWNERSHIP")
            for change in self.field_changes:
                out.append(f"  {change.id}\t{change.field}\t{change.before!r} -> {change.after!r}")
        out.append(
            "=== STANDING open epics/features={openEF} open bugs={openBugs} "
            "unparented={unparented} resolved-not-closed={resolved} open children={children}".format(
                openEF=self.counts.get("openEF", 0),
                openBugs=self.counts.get("openBugs", 0),
                unparented=len(self.unparented),
                resolved=len(self.resolved_not_closed),
                children=self.counts.get("openChildren", 0),
            )
        )
        return out


def _index(snapshot: dict) -> dict[int, dict]:
    return {item["id"]: item for item in snapshot.get("items", [])}


def _tracked(snapshot: dict) -> set[int]:
    """The sets whose membership is a defined query result rather than an
    artefact of how wide this pull's fetch window happened to be.

    This distinction is load-bearing. Each pull fetches the union of several
    queries, and the "changed since" window shrinks as polling gets more
    frequent, so an id can leave the union without anything happening to it.
    Membership signals are therefore read only off the fixed-meaning sets:
    leaving `openBugs` means the bug closed, leaving the fetch union means
    nothing at all.
    """
    return (
        set(snapshot.get("openEF") or [])
        | set(snapshot.get("openBugs") or [])
        | set(snapshot.get("children") or [])
        | set(snapshot.get("cardIds") or [])
    )


def compare(previous: dict | None, current: dict, known: dict[int, dict] | None = None) -> BoardDiff:
    """Diff two snapshots, using `known` as the memory of everything seen before.

    Comparing consecutive snapshots alone is not enough. Each pull fetches only
    what changed since the last one, so an item that moves exactly once is
    absent from the previous snapshot and its change is invisible: a poll that
    caught ten real closures reported one. `known` carries the last state seen
    for every item across all previous polls, which is what makes a single
    transition detectable.
    """
    now = _index(current)
    before = dict(known or {})
    before.update(_index(previous or {}))
    result = BoardDiff(
        since=(previous or {}).get("serverDate"),
        until=current.get("serverDate"),
    )
    now_tracked = _tracked(current)
    before_tracked = _tracked(previous or {})

    for item_id, item in sorted(now.items()):
        was = before.get(item_id)
        change = Change(
            id=item_id,
            type=item.get("type"),
            title=item.get("title") or "",
            kind="state",
            before=was.get("state") if was else None,
            after=item.get("state"),
            parent=item.get("parent"),
            changed=item.get("changed"),
        )
        if was is None:
            # New to a tracked set, not merely new to this pull's fetch window.
            if not before_tracked or item_id not in now_tracked or item_id in before_tracked:
                continue
            change.kind = "appeared"
            result.changes.append(change)
        elif was.get("state") != item.get("state"):
            result.changes.append(change)
            if item.get("state") in CLOSED_STATES:
                result.closed.append(change)
        if was is not None:
            for key, label in (("assignedTo", "owner"), ("priority", "priority"), ("iter", "iteration")):
                if key in was and was.get(key) != item.get(key):
                    result.field_changes.append(
                        FieldChange(item_id, label, was.get(key), item.get(key))
                    )

    for item_id in sorted(before_tracked - now_tracked):
        was = before.get(item_id, {})
        if item_id in now:
            # Still fetched, so its state change was already reported above;
            # leaving the set is the same event seen from the other side.
            continue
        result.changes.append(
            Change(
                id=item_id,
                type=was.get("type"),
                title=was.get("title") or "",
                kind="vanished",
                before=was.get("state"),
                after=None,
                parent=was.get("parent"),
            )
        )

    now_kids = {card: [k["id"] for k in kids] for card, kids in open_children_of(current).items()}
    prev_kids = {card: [k["id"] for k in kids] for card, kids in open_children_of(previous or {}).items()}
    for card in set(now_kids) | set(prev_kids):
        added = sorted(set(now_kids.get(card, [])) - set(prev_kids.get(card, [])))
        gone = sorted(set(prev_kids.get(card, [])) - set(now_kids.get(card, [])))
        if added and prev_kids:
            result.children_added[card] = added
        if gone and prev_kids:
            result.children_gone[card] = gone

    # A relation is only known to be added/removed when at least one card
    # relation set was captured in both snapshots. If its source left the
    # displayed-card scope, report a view change instead of inventing a link
    # transition. No linked endpoint is fetched to fill that gap.
    if previous is not None and "dependencies" in previous and "dependencies" in current:
        old_coverage = _relation_coverage(previous)
        new_coverage = _relation_coverage(current)
        old_sources = _dependency_sources(previous, old_coverage)
        new_sources = _dependency_sources(current, new_coverage)
        old_links, new_links = set(old_sources), set(new_sources)
        comparable = old_coverage & new_coverage
        result.dependencies_added = sorted(
            edge for edge in new_links - old_links if new_sources[edge] & comparable
        )
        result.dependencies_scope_added = sorted(
            edge for edge in new_links - old_links if not new_sources[edge] & comparable
        )
        result.dependencies_gone = sorted(
            edge for edge in old_links - new_links if old_sources[edge] & new_coverage
        )
        result.dependencies_scope_changed = sorted(
            edge for edge in old_links - new_links if not old_sources[edge] & new_coverage
        )

    open_bugs = set(current.get("openBugs") or [])
    result.unparented = sorted(
        item_id for item_id, item in now.items()
        if is_open(item.get("state")) and not item.get("parent")
        and (item_id in open_bugs or item.get("type") in ("Bug", "Task"))
    )
    result.resolved_not_closed = sorted(
        item_id for item_id, item in now.items() if item.get("state") == "Resolved"
    )
    result.counts = {
        "openEF": len(current.get("openEF") or []),
        "openBugs": len(open_bugs),
        "openChildren": sum(len(v) for v in now_kids.values()),
        "items": len(now),
    }
    return result


def _relation_coverage(snapshot: dict) -> set[int]:
    """Card IDs whose ADO Relations expansion returned in this snapshot."""
    scope = snapshot.get("queryScope") or {}
    return {int(item_id) for item_id in scope.get("cardRelationReturnedIds") or []}


def _dependency_sources(snapshot: dict, coverage: set[int]) -> dict[tuple[int, int], set[int]]:
    """Map each edge to the displayed records that reported it.

    Older relation-aware snapshots predate `observedBy`; infer the possible
    source cards from their recorded relation coverage, conservatively leaving
    an edge uncomparable if that provenance is unavailable.
    """
    out: dict[tuple[int, int], set[int]] = {}
    for row in snapshot.get("dependencies") or []:
        edge = (int(row["predecessor"]), int(row["successor"]))
        sources = {int(item_id) for item_id in row.get("observedBy") or []}
        if not sources:
            sources = set(edge) & coverage
        out[edge] = sources & coverage
    return out
