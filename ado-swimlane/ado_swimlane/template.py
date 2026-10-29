"""The curated board is a template with a few machine-managed regions.

The split is deliberate and load-bearing:

  * machine-written  - live card facts, open direct children, recorded ADO
                       dependency links, tracked-item readout, and timestamps
  * human-written    - narrative, dated notes, card placement, phase names,
                       and advisory ordering hints

A refresh therefore never rewrites analysis. The browser applies live ADO state
to card colors and hides old completion-date pills when a card reopens; this
module reports when the curated source styling disagrees so its historical note
can be reviewed. Missing item or relation coverage is kept visible to validation.
"""
from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass, field

from .config import DONE_STATES, org, project

# One card's opening tag. Attribute order in the board is class, id, then the
# optional data-state; the pattern tolerates any order and extra attributes.
CARD_TAG = re.compile(r'<div class="card(?P<classes>[^"]*)" id="(?P<id>c\d+)"(?P<rest>[^>]*)>')
DATA_STATE = re.compile(r'\s*data-state="(?P<state>[^"]*)"')
CARD_DATA_ATTRIBUTE = re.compile(
    r'\sdata-(?P<name>priority|assigned-to|iteration|changed|revision|priority-known|owner-known)="(?P<value>[^"]*)"'
)
CARD_LIVE_ATTRIBUTES = re.compile(
    r'\sdata-(?:state|priority|assigned-to|iteration|changed|revision|priority-known|owner-known)="[^"]*"'
)
CHILD_ROW = re.compile(r'\[(\d+),"([^"]+)","([^"]+)","')
CHILDREN_LINE = re.compile(r"^\s*(c\d+):\s*(\[\[.*)$", re.MULTILINE)

CHILDREN_OPEN = "var CHILDREN = {"
CHILDREN_CLOSE = "\n  };"
TRACKED_ITEMS_BLOCK = re.compile(r"\bvar TRACKED_ITEMS = (\[.*?\]);", re.DOTALL)
LINKS_BLOCK = re.compile(r"(?P<prefix>\bvar links = \[)(?P<body>.*?)(?P<suffix>\n\s*\];)", re.DOTALL)
LINK_ROW = re.compile(r'^\s*\["c(\d+)",\s*"c(\d+)",\s*(true|false)\],?\s*$', re.MULTILINE)
TRACKED_START = "<!-- ado-swimlane:tracked-snapshot:start -->"
TRACKED_END = "<!-- ado-swimlane:tracked-snapshot:end -->"
TRACKED_PANEL = re.compile(re.escape(TRACKED_START) + r".*?" + re.escape(TRACKED_END), re.DOTALL)
RELATION_START = "<!-- ado-swimlane:relation-snapshot:start -->"
RELATION_END = "<!-- ado-swimlane:relation-snapshot:end -->"
RELATION_PANEL = re.compile(re.escape(RELATION_START) + r".*?" + re.escape(RELATION_END), re.DOTALL)
FORMAL_RELATION_ROW = re.compile(
    r'<li data-predecessor="(\d+)" data-successor="(\d+)" data-observed-by="([0-9,]*)">'
)

VAR_SNAPSHOT = re.compile(r'(var SNAPSHOT = ")[^"]*(")')
VAR_DELTA = re.compile(r'(var DELTA_FROM = ")[^"]*(")')
VAR_REFRESHED = re.compile(r'(var REFRESHED = ")[^"]*(")')
# The separator is a middot, which the board may carry either literally or as an
# entity; matching only one spelling makes the count silently stop updating.
OPEN_EF_COUNT = re.compile(r"((?:·|&middot;)\s*)(?:\d+|unknown)(\s*open epics/features)")

TYPE_ABBR = {"User Story": "US", "Feature": "F", "Bug": "Bug", "Task": "Task", "Epic": "Epic"}

# Aggregate/bucket cards stand for a group, so they carry no single ADO state.
AGGREGATE = "none"


@dataclass
class Card:
    id: int
    dom_id: str
    classes: str
    state: str | None  # the data-state attribute as written, if any
    data: dict[str, str] = field(default_factory=dict)

    @property
    def styled_done(self) -> bool:
        return "done" in self.classes.split()

    @property
    def effective_state(self) -> str:
        """What the page actually reports for this card today."""
        if self.state:
            return self.state
        return "Closed" if self.styled_done else "New"


@dataclass
class Report:
    """What one injection changed, and what it refused to change."""

    states: list[tuple[str, str, str]] = field(default_factory=list)   # dom_id, from, to
    children_keys: int = 0
    children_total: int = 0
    mismatches: list[tuple[str, str, str]] = field(default_factory=list)  # dom_id, styling, live
    missing: list[str] = field(default_factory=list)  # cards with no live detail
    legacy_gate_links: int = 0  # curated amber links omitted without live relation proof
    live_gate_links: int = 0
    off_view_links: int = 0
    tracked_missing: list[int] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"states={len(self.states)} childrenKeys={self.children_keys} "
            f"children={self.children_total} mismatches={len(self.mismatches)} missing={len(self.missing)} "
            f"liveLinks={self.live_gate_links} offViewLinks={self.off_view_links} "
            f"legacyLinksOmitted={self.legacy_gate_links} "
            f"trackedMissing={len(self.tracked_missing)}"
        )


def read_cards(html: str) -> list[Card]:
    cards: list[Card] = []
    for match in CARD_TAG.finditer(html):
        state_match = DATA_STATE.search(match.group("rest"))
        cards.append(
            Card(
                id=int(match.group("id")[1:]),
                dom_id=match.group("id"),
                classes=match.group("classes").strip(),
                state=state_match.group("state") if state_match else None,
                data={name: html_mod.unescape(value) for name, value in CARD_DATA_ATTRIBUTE.findall(match.group("rest"))},
            )
        )
    return cards


def card_ids(html: str) -> list[int]:
    return sorted({card.id for card in read_cards(html)})


def read_children(html: str) -> dict[str, list[int]]:
    """Child ids per card as the template currently encodes them."""
    out: dict[str, list[int]] = {}
    for key, body in CHILDREN_LINE.findall(html):
        out[key] = [int(match.group(1)) for match in CHILD_ROW.finditer(body)]
    return out


def tracked_items(html: str) -> list[dict]:
    """Read the small, explicitly curated watch list from the board template."""
    match = TRACKED_ITEMS_BLOCK.search(html)
    if not match:
        return []
    items = json.loads(match.group(1))
    if not isinstance(items, list):
        raise ValueError("TRACKED_ITEMS must be a JSON array")
    seen: set[int] = set()
    normalized: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise ValueError("each TRACKED_ITEMS entry needs an integer id")
        item_id = item["id"]
        if item_id in seen:
            raise ValueError(f"duplicate tracked work item {item_id}")
        seen.add(item_id)
        normalized.append(item)
    return normalized


def known_ids(html: str) -> list[int]:
    """Every explicit id the template references: cards, children, and watch list."""
    ids = set(card_ids(html))
    for kids in read_children(html).values():
        ids.update(kids)
    ids.update(item["id"] for item in tracked_items(html))
    return sorted(ids)


def render_children_block(kids: dict[int, list[dict]], ordered_cards: list[int]) -> tuple[str, int, int]:
    """Build the CHILDREN literal. Cards with no open children are omitted, which
    is what makes an empty card mean 'nothing open' rather than 'not checked'."""
    lines = []
    total = 0
    for card_id in ordered_cards:
        listed = kids.get(card_id) or []
        if not listed:
            continue
        body = ",".join(
            json.dumps(
                [
                    item["id"],
                    TYPE_ABBR.get(item.get("type"), item.get("type") or "?"),
                    item.get("state") or "?",
                    item.get("title") or "",
                    item.get("priority"),
                    item.get("assignedTo"),
                    item.get("changed"),
                    item.get("revision"),
                    bool(item.get("priorityReturned")),
                    bool(item.get("assignedToReturned")),
                    item.get("iter"),
                    bool(item.get("iterationReturned")),
                ],
                ensure_ascii=True,
                separators=(",", ":"),
            ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            for item in listed
        )
        lines.append(f"    c{card_id}: [{body}],")
        total += len(listed)
    return "\n".join(lines), len(lines), total


def set_children(html: str, block: str) -> str:
    start = html.find(CHILDREN_OPEN)
    if start < 0:
        raise ValueError("CHILDREN block not found in template")
    end = html.find(CHILDREN_CLOSE, start)
    if end < 0:
        # The curated template may use the compact empty placeholder `{}`.
        empty_end = html.find("};", start + len(CHILDREN_OPEN))
        empty_body = html[start + len(CHILDREN_OPEN):empty_end] if empty_end >= 0 else "not empty"
        if empty_end < 0 or empty_body.strip():
            raise ValueError("CHILDREN terminator not found in template")
        return html[:start] + CHILDREN_OPEN + "\n" + block + CHILDREN_CLOSE + html[empty_end + 2:]
    return html[:start] + CHILDREN_OPEN + "\n" + block + html[end:]


def set_card_state(html: str, dom_id: str, state: str) -> str:
    """Write `data-state` on one card, adding the attribute when absent."""

    def replace(match: re.Match) -> str:
        if match.group("id") != dom_id:
            return match.group(0)
        rest = DATA_STATE.sub("", match.group("rest"))
        return f'<div class="card{match.group("classes")}" id="{dom_id}" data-state="{state}"{rest}>'

    return CARD_TAG.sub(replace, html)


def set_card_live_fields(html: str, dom_id: str, item: dict) -> str:
    """Write only machine-owned ADO facts to the card's opening tag."""
    values = {
        "state": item.get("state"),
        "priority": item.get("priority"),
        "assigned-to": item.get("assignedTo"),
        "iteration": item.get("iter"),
        "changed": item.get("changed"),
        "revision": item.get("revision"),
        "priority-known": "true" if item.get("priorityReturned") else "false",
        "owner-known": "true" if item.get("assignedToReturned") else "false",
    }

    def replace(match: re.Match) -> str:
        if match.group("id") != dom_id:
            return match.group(0)
        rest = CARD_LIVE_ATTRIBUTES.sub("", match.group("rest"))
        attrs = "".join(
            f' data-{name}="{html_mod.escape(str(value) if value is not None else "", quote=True)}"'
            for name, value in values.items()
        )
        return f'<div class="card{match.group("classes")}" id="{dom_id}"{attrs}{rest}>'

    return CARD_TAG.sub(replace, html)


def set_live_links(html: str, snapshot: dict) -> tuple[str, int, int]:
    """Draw captured relations only when both endpoint cards are displayed."""
    match = LINKS_BLOCK.search(html)
    dependencies = snapshot.get("dependencies")
    if not match:
        if dependencies:
            raise ValueError("live dependencies exist but the links array is missing")
        return html, 0, 0

    rows = _parse_links_body(match.group("body"))
    legacy = [(int(a), int(b)) for a, b, gate in rows if gate]
    hints = {(int(a), int(b)) for a, b, gate in rows if not gate}
    formal = {
        (int(edge["predecessor"]), int(edge["successor"]))
        for edge in (dependencies or [])
        if edge.get("predecessor") is not None and edge.get("successor") is not None
    }
    displayed = set(card_ids(html))
    live = {edge for edge in formal if edge[0] in displayed and edge[1] in displayed}
    hints.difference_update(formal)
    rendered = [
        f'    ["c{before}", "c{after}", true],'
        for before, after in sorted(live)
    ] + [
        f'    ["c{before}", "c{after}", false],'
        for before, after in sorted(hints)
    ]
    body = "\n".join(rendered)
    updated = html[:match.start()] + match.group("prefix") + "\n" + body + match.group("suffix") + html[match.end():]
    return updated, len(legacy), len(live)


def _parse_links_body(body: str) -> list[tuple[int, int, bool]]:
    rows = LINK_ROW.findall(body)
    if LINK_ROW.sub("", body).strip():
        raise ValueError("links array contains an unsupported row; refusing to rewrite it")
    return [(int(before), int(after), gate == "true") for before, after, gate in rows]


def read_links(html: str) -> list[tuple[int, int, bool]]:
    """Read rendered links as (predecessor, successor, is_live_relation)."""
    match = LINKS_BLOCK.search(html)
    return _parse_links_body(match.group("body")) if match else []


def read_formal_links(html: str) -> list[tuple[int, int, tuple[int, ...]]]:
    """Read every relation captured from displayed cards, including off-view IDs."""
    return [
        (int(before), int(after), tuple(int(item_id) for item_id in sources.split(",") if item_id))
        for before, after, sources in FORMAL_RELATION_ROW.findall(html)
    ]


def _relation_panel(snapshot: dict, displayed: set[int]) -> str:
    scope = snapshot.get("queryScope") or {}
    source_by_edge: dict[tuple[int, int], set[int]] = {}
    for edge in snapshot.get("dependencies") or []:
        key = (int(edge["predecessor"]), int(edge["successor"]))
        source_by_edge.setdefault(key, set()).update(
            int(item_id) for item_id in edge.get("observedBy") or []
        )
    dependencies = [
        (before, after, tuple(sorted(sources)))
        for (before, after), sources in sorted(source_by_edge.items())
    ]
    internal = sum(before in displayed and after in displayed for before, after, _sources in dependencies)
    off_view = len(dependencies) - internal
    returned = len(scope.get("cardRelationReturnedIds") or [])
    card_count = len(scope.get("cardIds") or snapshot.get("cardIds") or displayed)
    missing = sorted(int(item_id) for item_id in scope.get("cardRelationMissingIds") or [])
    coverage = f"ADO Relations expansion returned for {returned} of {card_count} displayed cards."
    if missing:
        coverage += " Missing relation responses for displayed card IDs: " + ", ".join(f"#{i}" for i in missing) + "."
    coverage += (
        " Linked endpoint IDs are shown below, but off-view work-item details are not fetched solely by following a relation."
    )
    rows: list[str] = []
    for before, after, sources in dependencies:
        off = [item_id for item_id in (before, after) if item_id not in displayed]
        if off:
            note = "off-view endpoint(s) " + ", ".join(f"#{item_id}" for item_id in off) + "; detail was not requested solely for this link"
        else:
            note = "both endpoints are displayed; the amber arrow is drawn"
        source_text = ",".join(str(item_id) for item_id in sources)
        rows.append(
            f'<li data-predecessor="{before}" data-successor="{after}" data-observed-by="{source_text}">'
            f'#{before} → #{after} <small>({escape(note)})</small></li>'
        )
    list_html = "<ul>" + "".join(rows) + "</ul>" if rows else "<p>No dependency relations were captured from displayed cards.</p>"
    teaser = f"{len(dependencies)} captured · {internal} arrows · {off_view} touch off-view IDs"
    return (
        f'{RELATION_START}<div class="kfacts relation-snapshot" id="relation-snapshot">'
        f'<details class="kfact par" id="relation-snapshot-details" open><summary><b>Recorded ADO dependency links</b>'
        f'<span class="teaser">{teaser}</span></summary>'
        f'<span class="body">{escape(coverage)} {list_html}'
        f"Recorded relations describe planning data and do not authorize or enforce a start gate.</span></details></div>{RELATION_END}"
    )


def _set_relation_panel(html: str, snapshot: dict) -> tuple[str, int]:
    match = RELATION_PANEL.search(html)
    dependencies = snapshot.get("dependencies") or []
    if not match:
        if dependencies:
            raise ValueError("captured ADO relations exist but the relation-snapshot panel is missing")
        return html, 0
    displayed = set(card_ids(html))
    panel = _relation_panel(snapshot, displayed)
    updated = html[:match.start()] + panel + html[match.end():]
    off_view = {
        (int(edge["predecessor"]), int(edge["successor"]))
        for edge in snapshot.get("dependencies") or []
        if int(edge["predecessor"]) not in displayed or int(edge["successor"]) not in displayed
    }
    return updated, len(off_view)


def _tracked_panel(snapshot: dict, specs: list[dict]) -> str:
    if not specs:
        return ""
    live = {int(item["id"]): item for item in snapshot.get("items", [])}
    observed = html_mod.escape(str(snapshot.get("serverDate") or "ADO response time unavailable"), quote=True)
    rows: list[str] = []
    for spec in specs:
        item_id = int(spec["id"])
        item = live.get(item_id)
        label = html_mod.escape(str(spec.get("label") or f"Work item {item_id}"), quote=True)
        scope = html_mod.escape(str(spec.get("scope") or "Explicitly tracked work item"), quote=True)
        expected_parent = spec.get("parent")
        href = html_mod.escape(f"{org()}/{project()}/_workitems/edit/{item_id}", quote=True)
        if item is None:
            facts = "ADO record: not returned in this snapshot; state, owner, priority, and parent are unknown."
        else:
            state = html_mod.escape(str(item.get("state") or "unknown"), quote=True)
            parent = item.get("parent")
            if parent is None:
                parent_text = "parent not returned"
            else:
                parent_text = f"parent {parent}"
            if expected_parent is not None:
                parent_text += f" (expected {int(expected_parent)})"
            owner = (html_mod.escape(str(item.get("assignedTo")), quote=True)
                     if item.get("assignedTo") else
                     ("Unassigned" if item.get("assignedToReturned") else "unknown (field not returned)"))
            priority = (f"P{int(item['priority'])}" if item.get("priority") is not None else
                        ("no priority" if item.get("priorityReturned") else "priority unknown"))
            iteration = (html_mod.escape(str(item.get("iter")), quote=True)
                         if item.get("iterationReturned") and item.get("iter") else
                         ("no iteration" if item.get("iterationReturned") else "iteration unknown"))
            revision = html_mod.escape(str(item.get("revision") or "unknown"), quote=True)
            changed = html_mod.escape(str(item.get("changed") or "unknown"), quote=True)
            facts = (
                f"ADO state {state} · {parent_text} · owner {owner} · {priority} · "
                f"iteration {iteration} · revision {revision} · changed {changed}"
            )
        rows.append(
            f'<li><a href="{href}" target="_blank" rel="noopener">#{item_id} {label}</a>'
            f'<br><span>{facts}</span><br><small>{scope}</small></li>'
        )
    panel = (
        f'{TRACKED_START}<div class="kfacts tracked-snapshot" id="tracked-snapshot">'
        f'<details class="kfact par" id="tracked-snapshot-details"><summary><b>Explicitly tracked work</b>'
        f'<span class="teaser">ADO response at {observed}</span></summary>'
        f'<span class="body"><ul>{"".join(rows)}</ul>'
        f'Snapshot fields describe the returned records only. ADO state, recorded links, and this read-only view '
        f'do not establish approval, deployment, qualification, or permission to execute.</span></details></div>{TRACKED_END}'
    )
    return panel


def _set_tracked_panel(html: str, snapshot: dict, specs: list[dict]) -> str:
    html = TRACKED_PANEL.sub("", html)
    panel = _tracked_panel(snapshot, specs)
    if not panel:
        return html
    marker = '<div class="boardwrap">'
    if marker not in html:
        raise ValueError("boardwrap not found; cannot place tracked-work snapshot")
    return html.replace(marker, panel + "\n" + marker, 1)


def inject(html: str, snapshot: dict, *, snapshot_date: str, delta_from: str, refreshed: str,
           kids: dict[int, list[dict]]) -> tuple[str, Report]:
    """Apply one live snapshot to the curated template."""
    report = Report()
    live = {item["id"]: item for item in snapshot.get("items", [])}
    ordered = snapshot.get("cardIds") or card_ids(html)

    for card in read_cards(html):
        if card.state == AGGREGATE:
            continue
        item = live.get(card.id)
        if not item or not item.get("state"):
            report.missing.append(card.dom_id)
            continue
        live_state = item["state"]
        if card.effective_state != live_state:
            report.states.append((card.dom_id, card.effective_state, live_state))
        html = set_card_live_fields(html, card.dom_id, item)
        # Styling is a curated claim; only flag the disagreement.
        if card.styled_done != (live_state in DONE_STATES):
            report.mismatches.append(
                (card.dom_id, "done" if card.styled_done else "open", live_state)
            )

    block, keys, total = render_children_block(kids, ordered)
    html = set_children(html, block)
    report.children_keys, report.children_total = keys, total

    html, report.legacy_gate_links, report.live_gate_links = set_live_links(html, snapshot)
    html, report.off_view_links = _set_relation_panel(html, snapshot)
    specs = tracked_items(html)
    present = {int(item["id"]) for item in snapshot.get("items", [])}
    report.tracked_missing = sorted(int(spec["id"]) for spec in specs if int(spec["id"]) not in present)
    html = _set_tracked_panel(html, snapshot, specs)

    html = VAR_SNAPSHOT.sub(lambda m: m.group(1) + snapshot_date + m.group(2), html, count=1)
    html = VAR_DELTA.sub(lambda m: m.group(1) + delta_from + m.group(2), html, count=1)
    html = VAR_REFRESHED.sub(lambda m: m.group(1) + refreshed + m.group(2), html, count=1)

    # An empty list is the query's real answer, so zero is written too; only a
    # snapshot without the query leaves the curated number alone.
    if snapshot.get("openEF") is not None:
        open_ef = len(snapshot["openEF"])
        html = OPEN_EF_COUNT.sub(lambda m: m.group(1) + str(open_ef) + m.group(2), html, count=1)

    return html, report


def escape(value: str) -> str:
    return html_mod.escape(value, quote=True)
