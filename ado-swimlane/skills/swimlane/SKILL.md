---
name: swimlane
description: Refresh the Azure DevOps backlog swimlane dashboard and report what moved on the board, optionally republishing it as a Claude Artifact. Use when the user asks to refresh the dashboard, asks what changed on the backlog or board, asks about the swimlanes, or invokes /swimlane.
---

# Backlog swimlane dashboard

The board lives in the `ado-swimlane` repo (`C:\Users\rdpro\Projects\ado-swimlane`).
A local service keeps the loopback page current on its own; this skill is for
answering "what changed?" and for republishing the Artifact, which the service
cannot do.

## Refresh and report

If the service is running, the data is already current — read it rather than
re-pulling:

```bash
py C:/Users/rdpro/Projects/ado-swimlane/ado-swimlane.py status
py C:/Users/rdpro/Projects/ado-swimlane/ado-swimlane.py changes --limit 1
```

If it is not running, pull once:

```bash
py C:/Users/rdpro/Projects/ado-swimlane/ado-swimlane.py refresh
```

Either way you get: card states that moved, children added or gone, standing
counts, and any card whose curated styling disagrees with its live state.

## Before reporting anything as fact

The discipline this board was built on: **a bulk tool's own cited evidence is
not evidence.** Before writing that a Feature closed cleanly, that a gate
cleared, or that an audit was right, re-query the specific items through WIQL or
`$expand=relations` and check. This has caught a backlog audit miscounting
children, a Feature activating with a gate still New, and a story left behind by
its own Closed parent. Report the discrepancy plainly; do not adjudicate whether
it is wrong, and do not change ADO state to fix it.

## Writing the prose

`refresh` updates live state only. The key-fact panels, card notes and the
"Changed" delta panel are written by hand, in `board/board.html`:

- edit the delta panel to the current window; old wins become permanent green
  cards rather than accumulating in the panel;
- when a refresh reports a styling disagreement (card shown as closed, work item
  open again), settle it in prose — the refresh deliberately will not;
- never hand-edit `data-state`, the `CHILDREN` map, or the snapshot dates. The
  next refresh overwrites them, and the template's copies are only a fallback.

## The Artifact republishes itself — do not fight it

The service republishes the Artifact whenever the rendered page would look
different, using its own pinned `claude -p` session. **You normally do not
publish it by hand**, and publishing from a chat session is actively harmful:
the service's next publish will refuse rather than overwrite you, and it stays
jammed until someone runs `publish --force`.

So the workflow is: **put prose in `board/board.html` and let the service ship
it.** A committed prose edit is picked up on the next poll.

If `status` shows a held publish, that jam has already happened. Say so, and
clear it deliberately:

```bash
py C:/Users/rdpro/Projects/ado-swimlane/ado-swimlane.py publish --force
```

Publish from a session only when the service is not running and the user asks
for it. The Artifact URL is in memory under `ado-swimlane-dashboard`; from an
interactive session, read it first and publish to the same URL.

## Checks

`refresh` runs them automatically and `validate` re-runs them on demand. They
exist because this page can be wrong while looking perfect. If a check fails,
say so and stop; do not publish a page that failed its own oracle.
