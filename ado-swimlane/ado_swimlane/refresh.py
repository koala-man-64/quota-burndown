"""One refresh: pull, inject, verify, write. Shared by the CLI and the service.

Nothing here decides *what the board means*. It moves live state onto the page
and reports what moved; the interpretation stays in the curated prose.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from . import ado, diff, render, template, validate
from .config import Paths, template_path
from .util import atomic_write_text, iso, now_utc, read_json, write_json

# How far back the "what changed" query reaches on a cold start, when there is
# no previous snapshot to diff against.
COLD_START_DAYS = 2


@dataclass
class RefreshResult:
    revision: str
    body: str
    snapshot: dict
    board_diff: diff.BoardDiff
    report: template.Report
    checks: validate.Result

    @property
    def ok(self) -> bool:
        return self.checks.ok


def _since(previous: dict | None) -> str:
    """WIQL rejects sub-second precision outright, so seconds it is."""
    moment = render.server_moment(previous) if previous else now_utc() - timedelta(days=COLD_START_DAYS)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(paths: Paths, *, client: ado.Client | None = None, source: Path | None = None) -> RefreshResult:
    """Collect a snapshot and render the page from it.

    Reads the curated board from `source` (the repo template by default) and
    leaves every write atomic, so a reader never sees a partial page.
    """
    source = source or template_path()
    curated = source.read_text(encoding="utf-8-sig")

    previous = read_json(paths.snapshot)
    snapshot = ado.collect(
        template.card_ids(curated),
        template.known_ids(curated),
        since=_since(previous),
        client=client,
    )

    kids = ado.open_children_of(snapshot)
    previous_page = paths.artifact.read_text(encoding="utf-8-sig") if paths.artifact.exists() else None
    body, report = render.compose(curated, snapshot, kids=kids, previous_page=previous_page)
    checks = validate.check(body, snapshot, expected_children=len(snapshot.get("children") or []))
    known = {int(k): v for k, v in (read_json(paths.states, {}) or {}).items()}
    board_diff = diff.compare(previous, snapshot, known)
    revision = render.revision_of(snapshot)

    if previous is not None:
        write_json(paths.previous, previous)
    write_json(paths.snapshot, snapshot)
    # Remember every item's state, so a change is still detectable when the item
    # falls outside a later pull's fetch window.
    known.update({item["id"]: item for item in snapshot.get("items", [])})
    write_json(paths.states, known)
    # The template is the source of truth for prose and is never written back
    # to; the injected page is an output.
    atomic_write_text(paths.artifact, render.artifact_page(body))
    atomic_write_text(paths.html, render.static_page(body))
    with open(paths.changes, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"revision": revision, **board_diff.as_dict()}, separators=(",", ":")) + "\n")

    return RefreshResult(
        revision=revision,
        body=body,
        snapshot=snapshot,
        board_diff=board_diff,
        report=report,
        checks=checks,
    )
