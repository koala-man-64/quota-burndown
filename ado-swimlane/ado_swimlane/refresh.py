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
    revision: str  # attempted ADO response revision
    body: str  # accepted render, or last-good/raw fallback when checks fail
    snapshot: dict  # attempted pull; accepted state only when its render passes checks
    board_diff: diff.BoardDiff
    report: template.Report
    checks: validate.Result
    served_revision: str | None = None
    health_detail: str | None = None

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
    tracked_items = template.tracked_items(curated)
    snapshot = ado.collect(
        template.card_ids(curated),
        template.known_ids(curated),
        since=_since(previous),
        tracked_items=tracked_items,
        client=client,
    )

    kids = ado.open_children_of(snapshot)
    previous_page = paths.artifact.read_text(encoding="utf-8-sig") if paths.artifact.exists() else None
    body, report = render.compose(curated, snapshot, kids=kids, previous_page=previous_page)
    checks = validate.check(body, snapshot, expected_children=len(snapshot.get("children") or []))
    known = {int(k): v for k, v in (read_json(paths.states, {}) or {}).items()}
    board_diff = diff.compare(previous, snapshot, known)
    revision = render.revision_of(snapshot)

    served_revision: str | None
    health_detail: str | None = None
    if checks.ok:
        if previous is not None:
            write_json(paths.previous, previous)
        write_json(paths.snapshot, snapshot)
        # Remember states only from a render that passed its acceptance checks.
        known.update({item["id"]: item for item in snapshot.get("items", [])})
        write_json(paths.states, known)
        # The template is the source of truth for prose and is never written back.
        atomic_write_text(paths.artifact, render.artifact_page(body))
        atomic_write_text(paths.html, render.static_page(body))
        with open(paths.changes, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"revision": revision, **board_diff.as_dict()}, separators=(",", ":")) + "\n")
        served_revision = revision
    else:
        # Preserve rejected evidence separately from every accepted-state file.
        # One atomic latest file bounds retention; no route serves this diagnostic.
        try:
            write_json(paths.home / "failed-attempt.latest.json", {
                "schema": "ado-swimlane-rejected-normalized-attempt-v1",
                "accepted": False,
                "served": False,
                "capturedAt": iso(now_utc()),
                "attemptedRevision": revision,
                "snapshot": snapshot,
                "validation": {"ok": False, "notes": checks.notes, "totals": checks.totals},
            })
        except OSError:
            # Evidence storage failure must not hide the failed checks or retire
            # the last-good fallback path. No rejected state becomes accepted.
            checks.note("failed-attempt diagnostic write failed (OSError)")
        # Never replace accepted state with a pull whose page failed validation.
        # Re-render the retained snapshot against the current template and checks;
        # if it no longer qualifies, fail closed to the raw unverified template.
        served_body, served_revision, retained_problem = _verified_previous(
            curated, previous, previous_page
        )
        details = ["latest ADO snapshot failed validation"]
        if served_revision:
            details.append(f"showing last-good snapshot {served_revision}")
        else:
            details.append("no retained snapshot passed current validation; showing the unverified template")
        if retained_problem:
            details.append(retained_problem)
        details.extend(checks.notes)
        health_detail = "; ".join(details)
        atomic_write_text(
            paths.html,
            render.static_page(served_body, degraded=health_detail),
        )
        body = served_body

    return RefreshResult(
        revision=revision,
        body=body,
        snapshot=snapshot,
        board_diff=board_diff,
        report=report,
        checks=checks,
        served_revision=served_revision,
        health_detail=health_detail,
    )


def _verified_previous(curated: str, previous: dict | None,
                       previous_page: str | None) -> tuple[str, str | None, str | None]:
    """Return retained content only when it passes the current render checks."""
    if previous is None:
        return curated, None, None
    kids = ado.open_children_of(previous)
    body, _report = render.compose(curated, previous, kids=kids, previous_page=previous_page)
    checks = validate.check(body, previous, expected_children=len(previous.get("children") or []))
    if checks.ok:
        return body, render.revision_of(previous), None
    return curated, None, "retained snapshot is not valid under current checks: " + "; ".join(checks.notes)


def degraded_view(paths: Paths, reason: str) -> tuple[str, str | None, str]:
    """Build a visibly degraded page from a revalidated last-good snapshot.

    Collection exceptions happen before ``run`` can produce a RefreshResult.
    Keep the same last-good/raw-template fallback and validation boundary as a
    rejected render, without touching persisted snapshot or publication state.
    """
    curated = template_path().read_text(encoding="utf-8-sig")
    previous = read_json(paths.snapshot)
    previous_page = paths.artifact.read_text(encoding="utf-8-sig") if paths.artifact.exists() else None
    body, served_revision, retained_problem = _verified_previous(curated, previous, previous_page)
    details = [reason]
    if served_revision:
        details.append(f"showing last-good snapshot {served_revision}")
    else:
        details.append("no retained snapshot passed current validation; showing the unverified template")
    if retained_problem:
        details.append(retained_problem)
    return body, served_revision, "; ".join(details)
