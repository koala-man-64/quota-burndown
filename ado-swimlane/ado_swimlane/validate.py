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
        live = {item["id"]: item.get("state") for item in snapshot.get("items", [])}
        mismatched = []
        unknown = []
        for row in rows:
            if row["state"] == template.AGGREGATE:
                continue
            actual = live.get(int(row["id"][1:]))
            if not actual:
                unknown.append(row["id"])
            elif actual != row["state"]:
                mismatched.append(f"{row['id']} page={row['state']} live={actual}")
        if mismatched:
            result.fail("card state disagrees with live ADO: " + "; ".join(mismatched))
        if unknown:
            # The collector asks for every card and ADO omits the ids it cannot
            # return, so a card missing here shows a state nobody checked.
            result.fail("card(s) with no live record (deleted, mistyped or unreadable): " + ", ".join(unknown))

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
