"""The curated board is a template with a few machine-managed regions.

The split is deliberate and load-bearing:

  * machine-written  - live state (`data-state`), the CHILDREN map, the snapshot
                       timestamps, the open epic/feature count
  * human-written    - every word of prose: key facts, card notes, the delta
                       panel, lane and phase names

A refresh therefore never rewrites analysis, and never has to. Where live state
and curated presentation disagree (a card now Closed that is still styled as
open work), this module *reports* the mismatch instead of restyling the card,
because the restyle carries a claim - "closed 09-10", a green pill - that only a
person or a Claude session with the evidence in hand should make.
"""
from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field

from .config import DONE_STATES

# One card's opening tag. Attribute order in the board is class, id, then the
# optional data-state; the pattern tolerates any order and extra attributes.
CARD_TAG = re.compile(r'<div class="card(?P<classes>[^"]*)" id="(?P<id>c\d+)"(?P<rest>[^>]*)>')
DATA_STATE = re.compile(r'\s*data-state="(?P<state>[^"]*)"')
CHILD_ROW = re.compile(r'\[(\d+),"([^"]+)","([^"]+)","')
CHILDREN_LINE = re.compile(r"^\s*(c\d+):\s*(\[\[.*)$", re.MULTILINE)

CHILDREN_OPEN = "var CHILDREN = {"
CHILDREN_CLOSE = "\n  };"

VAR_SNAPSHOT = re.compile(r'(var SNAPSHOT = ")[^"]*(")')
VAR_DELTA = re.compile(r'(var DELTA_FROM = ")[^"]*(")')
VAR_REFRESHED = re.compile(r'(var REFRESHED = ")[^"]*(")')
# The separator is a middot, which the board may carry either literally or as an
# entity; matching only one spelling makes the count silently stop updating.
OPEN_EF_COUNT = re.compile(r"((?:·|&middot;)\s*)\d+(\s*open epics/features)")

TYPE_ABBR = {"User Story": "US", "Feature": "F", "Bug": "Bug", "Task": "Task", "Epic": "Epic"}

# Aggregate/bucket cards stand for a group, so they carry no single ADO state.
AGGREGATE = "none"


@dataclass
class Card:
    id: int
    dom_id: str
    classes: str
    state: str | None  # the data-state attribute as written, if any

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

    def summary(self) -> str:
        return (
            f"states={len(self.states)} childrenKeys={self.children_keys} "
            f"children={self.children_total} mismatches={len(self.mismatches)} missing={len(self.missing)}"
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


def known_ids(html: str) -> list[int]:
    """Every id the template references: cards plus their listed children."""
    ids = set(card_ids(html))
    for kids in read_children(html).values():
        ids.update(kids)
    return sorted(ids)


def _js_string(value: str) -> str:
    """Escape for a double-quoted JS string inside an inline <script>."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("</script", "<\\/script")
        .replace("\r", " ")
        .replace("\n", " ")
    )


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
            '[{id},"{type}","{state}","{title}"]'.format(
                id=item["id"],
                type=TYPE_ABBR.get(item.get("type"), item.get("type") or "?"),
                state=item.get("state") or "?",
                title=_js_string(item.get("title") or ""),
            )
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
        raise ValueError("CHILDREN terminator not found in template")
    return html[:start] + CHILDREN_OPEN + "\n" + block + html[end:]


def set_card_state(html: str, dom_id: str, state: str) -> str:
    """Write `data-state` on one card, adding the attribute when absent."""

    def replace(match: re.Match) -> str:
        if match.group("id") != dom_id:
            return match.group(0)
        rest = DATA_STATE.sub("", match.group("rest"))
        return f'<div class="card{match.group("classes")}" id="{dom_id}" data-state="{state}"{rest}>'

    return CARD_TAG.sub(replace, html)


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
            html = set_card_state(html, card.dom_id, live_state)
        # Styling is a curated claim; only flag the disagreement.
        if card.styled_done != (live_state in DONE_STATES):
            report.mismatches.append(
                (card.dom_id, "done" if card.styled_done else "open", live_state)
            )

    block, keys, total = render_children_block(kids, ordered)
    html = set_children(html, block)
    report.children_keys, report.children_total = keys, total

    html = VAR_SNAPSHOT.sub(lambda m: m.group(1) + snapshot_date + m.group(2), html, count=1)
    html = VAR_DELTA.sub(lambda m: m.group(1) + delta_from + m.group(2), html, count=1)
    html = VAR_REFRESHED.sub(lambda m: m.group(1) + refreshed + m.group(2), html, count=1)

    open_ef = len(snapshot.get("openEF") or [])
    if open_ef:
        html = OPEN_EF_COUNT.sub(lambda m: m.group(1) + str(open_ef) + m.group(2), html, count=1)

    return html, report


def escape(value: str) -> str:
    return html_mod.escape(value, quote=True)
