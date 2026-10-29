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
class BoardDiff:
    since: str | None = None
    until: str | None = None
    changes: list[Change] = field(default_factory=list)
    closed: list[Change] = field(default_factory=list)
    children_added: dict[int, list[int]] = field(default_factory=dict)
    children_gone: dict[int, list[int]] = field(default_factory=dict)
    unparented: list[int] = field(default_factory=list)
    resolved_not_closed: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["changes"] = [asdict(c) for c in self.changes]
        payload["closed"] = [asdict(c) for c in self.closed]
        return payload

    def is_quiet(self) -> bool:
        return not (self.changes or self.children_added or self.children_gone)

    def lines(self) -> list[str]:
        """Human-readable report, the shape the old analyze-board.js printed."""
        out = [f"=== CHANGES {self.since or '?'} -> {self.until or '?'}"]
        for change in self.changes:
            if change.kind == "state":
                out.append(f"  {change.id}\t{change.type}\t{change.before} -> {change.after}\t{change.title[:60]}")
            else:
                out.append(f"  {change.id}\t{change.type}\t{change.kind} ({change.after})\tparent={change.parent or '-'}\t{change.title[:60]}")
        if not self.changes:
            out.append("  (nothing moved)")
        if self.children_added or self.children_gone:
            out.append("=== CHILDREN")
            for card, ids in sorted(self.children_added.items()):
                out.append(f"  c{card} + {', '.join(str(i) for i in ids)}")
            for card, ids in sorted(self.children_gone.items()):
                out.append(f"  c{card} - {', '.join(str(i) for i in ids)}")
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
