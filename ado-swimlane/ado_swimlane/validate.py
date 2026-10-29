"""Checks that run on every render, before anything is served or published.

These exist because the failure mode of this dashboard is not a crash - it is a
page that renders perfectly while saying something untrue. So the render is
verified two ways: structurally (does every reference resolve), and numerically
(does an independent walk of the DOM reach the same totals the page's own
script will). The second is the important one; it is an oracle, not a re-run of
the rendering code.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

from . import template
from .config import DONE_STATES

PHASES = ["P0", "P1", "P2", "P3", "P4", "P5", "P6"]
LANE_LABEL = re.compile(r'<div class="lanelabel[^"]*">')
CELL = re.compile(r'<div class="cell[^"]*">')
LANE_NAME = re.compile(r'<div class="lanelabel[^"]*">([\s\S]*?)<small>')
TAGS = re.compile(r"<[^>]+>")
SCRIPT = re.compile(r"<script\b[^>]*>([\s\S]*?)</script>", re.IGNORECASE)


@dataclass
class Result:
    ok: bool = True
    notes: list[str] = field(default_factory=list)
    totals: dict = field(default_factory=dict)

    def fail(self, note: str) -> None:
        self.ok = False
        self.notes.append(note)

    def note(self, note: str) -> None:
        self.notes.append(note)

    def __str__(self) -> str:
        head = "OK" if self.ok else "FAILED"
        return "\n".join([head, *("  " + n for n in self.notes)])


def walk(html: str) -> list[dict]:
    """Independent walk of the board: lane -> seven phase cells -> cards.

    Deliberately string-based rather than a reuse of the render path, so a bug
    in the renderer cannot hide behind the same bug in the check.
    """
    start = html.find('<div class="board" id="board">')
    if start < 0:
        return []
    end = html.find("</div>\n</div>", start)
    board = html[start:end if end > 0 else len(html)]
    lane_names = [TAGS.sub("", name).strip() for name in LANE_NAME.findall(board)]
    chunks = LANE_LABEL.split(board)[1:]
    kids = template.read_children(html)
    states = _children_states(html)

    rows: list[dict] = []
    for lane_index, chunk in enumerate(chunks):
        cells = CELL.split(chunk)[1:]
        for phase_index, cell in enumerate(cells[:len(PHASES)]):
            for card in template.CARD_TAG.finditer(cell):
                dom_id = card.group("id")
                state_match = template.DATA_STATE.search(card.group("rest"))
                classes = card.group("classes").strip().split()
                if state_match:
                    state = state_match.group("state")
                else:
                    state = "Closed" if "done" in classes else "New"
                listed = kids.get(dom_id, [])
                rows.append({
                    "lane": lane_names[lane_index] if lane_index < len(lane_names) else "?",
                    "phase": PHASES[phase_index],
                    "id": dom_id,
                    "state": state,
                    "children": listed,
                    "childStates": [states.get(cid, "?") for cid in listed],
                })
    return rows


def _children_states(html: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for _key, body in template.CHILDREN_LINE.findall(html):
        for match in template.CHILD_ROW.finditer(body):
            out[int(match.group(1))] = match.group(3)
    return out


def check(html: str, snapshot: dict | None = None, *, expected_children: int | None = None) -> Result:
    result = Result()

    dom_ids = {match.group("id") for match in template.CARD_TAG.finditer(html)}
    children = template.read_children(html)
    for key in children:
        if key not in dom_ids:
            result.fail(f"CHILDREN key {key} has no card in the DOM")

    rows = walk(html)
    if not rows:
        result.fail("board grid not found; the lane/cell structure changed")
        return result

    walked = {row["id"] for row in rows}
    orphaned = dom_ids - walked
    if orphaned:
        # Cards outside the grid are legitimate only if there are none: every
        # card is supposed to live in exactly one lane/phase cell.
        result.fail(f"{len(orphaned)} card(s) outside the lane grid: {', '.join(sorted(orphaned))}")

    by_state: dict[str, int] = {}
    child_states: dict[str, int] = {}
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        for state in row["childStates"]:
            child_states[state] = child_states.get(state, 0) + 1

    total_children = sum(len(row["children"]) for row in rows)
    result.totals = {
        "cards": len(rows),
        "cardStates": by_state,
        "openChildren": total_children,
        "childStates": child_states,
    }

    if expected_children is not None and expected_children != total_children:
        result.fail(f"children walked ({total_children}) != live open children ({expected_children})")

    closed_children = [state for state in child_states if state in DONE_STATES]
    if closed_children:
        result.fail(f"CHILDREN lists closed work: {', '.join(sorted(closed_children))}")

    if snapshot:
        items = {int(item["id"]): item for item in snapshot.get("items", [])}
        live = {item_id: item.get("state") for item_id, item in items.items()}
        mismatched = []
        unknown = []
        card_data_mismatched = []
        card_by_id = {card.id: card for card in template.read_cards(html)}
        for row in rows:
            if row["state"] == template.AGGREGATE:
                continue
            item_id = int(row["id"][1:])
            actual = live.get(item_id)
            if not actual:
                unknown.append(row["id"])
            elif actual != row["state"]:
                mismatched.append(f"{row['id']} page={row['state']} live={actual}")
            item = items.get(item_id)
            card = card_by_id.get(item_id)
            if item and card and snapshot.get("queryScope"):
                expected_attrs = {
                    "priority": str(item.get("priority") if item.get("priority") is not None else ""),
                    "assigned-to": str(item.get("assignedTo") or ""),
                    "iteration": str(item.get("iter") or ""),
                    "changed": str(item.get("changed") or ""),
                    "revision": str(item.get("revision") if item.get("revision") is not None else ""),
                    "priority-known": "true" if item.get("priorityReturned") else "false",
                    "owner-known": "true" if item.get("assignedToReturned") else "false",
                }
                for name, expected in expected_attrs.items():
                    if card.data.get(name) != expected:
                        card_data_mismatched.append(
                            f"{row['id']} {name} page={card.data.get(name)!r} live={expected!r}"
                        )
        if mismatched:
            result.fail("card state disagrees with live ADO: " + "; ".join(mismatched))
        if unknown:
            result.fail("card(s) with no live record (deleted, mistyped or unreadable): " + ", ".join(unknown))
        if card_data_mismatched:
            result.fail("card scheduling/ownership data disagrees with live ADO: " + "; ".join(card_data_mismatched))

        if template.LINKS_BLOCK.search(html):
            scope = snapshot.get("queryScope") or {}
            if scope.get("relationExpand") != "Relations":
                result.fail("dependency relation evidence is absent; amber links cannot be verified")
            missing_relations = scope.get("cardRelationMissingIds")
            if missing_relations is None:
                result.fail("dependency relation coverage is unknown for displayed cards")
            elif missing_relations:
                result.fail("dependency relations not returned for card(s): " + ", ".join(map(str, missing_relations)))
            fallback = scope.get("cardRelationFallback") or {}
            if fallback.get("error"):
                result.fail("dependency fallback failed: " + str(fallback["error"]))
            requested_fallback = set(fallback.get("sourceIds") or [])
            if "cardRelationLiteralIds" in scope:
                literal_ids = set(scope["cardRelationLiteralIds"])
                completed_ids = {int(item_id) for item_id in (fallback.get("observations") or {})}
                recorded_ids = set(scope.get("cardRelationReturnedIds") or [])
                displayed_ids = {int(row["id"][1:]) for row in rows}
                if (recorded_ids != literal_ids | completed_ids or not literal_ids <= displayed_ids
                        or not requested_fallback <= displayed_ids & set(items)
                        or any(item_id not in items or not items[item_id].get("relationsFetched") for item_id in literal_ids)):
                    result.fail("dependency relation coverage disagrees with its recorded provenance")
            for source_id, observation in (fallback.get("observations") or {}).items():
                source_id = int(source_id)
                if (source_id not in requested_fallback or source_id not in items
                        or source_id not in {int(row["id"][1:]) for row in rows}
                        or observation.get("sourceId") != source_id
                        or observation.get("method") != "wiql-workitemlinks"
                        or observation.get("mode") != "MustContain"
                        or observation.get("complete") is not True or not observation.get("asOf")
                        or items[source_id].get("relationEvidence") != observation):
                    result.fail(f"dependency fallback provenance is invalid for card {source_id}")
            card_set = {int(row["id"][1:]) for row in rows}
            expected_links = {
                (int(edge["predecessor"]), int(edge["successor"]))
                for edge in snapshot.get("dependencies", [])
            }
            returned = {int(item_id) for item_id in scope.get("cardRelationReturnedIds") or []}
            for edge in snapshot.get("dependencies", []):
                sources = {int(item_id) for item_id in edge.get("observedBy") or []}
                if sources and (not sources <= card_set or not sources <= returned):
                    result.fail(
                        f"dependency {edge['predecessor']} -> {edge['successor']} has a source outside returned displayed-card relations"
                    )
                elif not sources and not ({int(edge["predecessor"]), int(edge["successor"])} & card_set & returned):
                    result.fail(
                        f"dependency {edge['predecessor']} -> {edge['successor']} has no captured displayed-card relation source"
                    )

            expected_arrows = {
                edge for edge in expected_links if edge[0] in card_set and edge[1] in card_set
            }
            rendered = template.read_links(html)
            live_links = {(before, after) for before, after, is_live in rendered if is_live}
            formal_links = {(before, after) for before, after, _sources in template.read_formal_links(html)}
            if live_links != expected_arrows:
                result.fail(
                    "rendered amber arrows disagree with displayed-endpoint ADO relations: "
                    f"missing={sorted(expected_arrows - live_links)} extra={sorted(live_links - expected_arrows)}"
                )
            if formal_links != expected_links:
                result.fail(
                    "visible relation list disagrees with captured ADO relations: "
                    f"missing={sorted(expected_links - formal_links)} extra={sorted(formal_links - expected_links)}"
                )
            if 'id="relation-snapshot"' not in html:
                result.fail("recorded ADO relation scope panel is missing")
            result.totals["recordedDependencies"] = len(expected_links)
            result.totals["visibleDependencyArrows"] = len(live_links)
            result.totals["offViewDependencyEdges"] = len(expected_links - expected_arrows)
            result.totals["advisoryOrderingHints"] = sum(not is_live for _before, _after, is_live in rendered)

        specs = template.tracked_items(html)
        tracked_missing = []
        tracked_misparented = []
        for spec in specs:
            item_id = int(spec["id"])
            item = items.get(item_id)
            if item is None:
                tracked_missing.append(item_id)
                continue
            expected_parent = spec.get("parent")
            if expected_parent is not None and item.get("parent") != int(expected_parent):
                tracked_misparented.append(f"{item_id} parent={item.get('parent')} expected={int(expected_parent)}")
        if tracked_missing:
            result.fail("explicitly tracked item(s) missing from snapshot: " + ", ".join(map(str, tracked_missing)))
        if tracked_misparented:
            result.fail("explicitly tracked parent mismatch: " + "; ".join(tracked_misparented))
        if specs:
            scope = snapshot.get("queryScope") or {}
            requested = set(scope.get("trackedIds") or [])
            returned = set(scope.get("trackedReturnedIds") or [])
            expected_ids = {int(spec["id"]) for spec in specs}
            if requested != expected_ids or returned != (expected_ids - set(tracked_missing)):
                result.fail("explicit tracked-item request/return coverage disagrees with the template")
            if 'id="tracked-snapshot"' not in html:
                result.fail("explicit tracked-item snapshot panel is missing")
        result.totals["trackedItems"] = len(specs)
        result.totals["trackedItemsMissing"] = len(tracked_missing)
    elif template.LINKS_BLOCK.search(html):
        if any(is_live for _before, _after, is_live in template.read_links(html)):
            result.fail("raw template contains amber links without a captured ADO relation snapshot")

    node = shutil.which("node")
    if node:
        note, bad = _syntax_check(html, node)
        (result.fail if bad else result.note)(note)
    else:
        result.note("inline script syntax not checked (node not on PATH)")
    return result


def _syntax_check(html: str, node: str) -> tuple[str, int]:
    """Parse every inline script without running it. `node --check` on a temp
    file is the same guarantee the old vm.Script check gave."""
    import tempfile
    from pathlib import Path

    bad = 0
    scripts = [body for body in SCRIPT.findall(html) if body.strip()]
    for index, body in enumerate(scripts, 1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(body)
            path = Path(handle.name)
        try:
            done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=30)
            if done.returncode:
                bad += 1
        finally:
            path.unlink(missing_ok=True)
    return f"inline scripts: {len(scripts)} checked, {bad} with syntax errors", bad
