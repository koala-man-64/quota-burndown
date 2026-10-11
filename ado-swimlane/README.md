# ado-swimlane

The AdaptiveAssetAllocation backlog as execution swimlanes — lanes are programs,
columns are phases, arrows are recorded `Predecessor/Successor` links — kept
current by a local service that polls Azure DevOps and pushes updates to the
open page.

It grew out of a hand-run pipeline of PowerShell and Node scripts in a session
scratchpad. This repository is that pipeline made repeatable: same board, same
checks, no human in the loop for the parts that were never judgement calls.

```
py ado-swimlane.py refresh          # pull once, re-render, verify, report what moved
py ado-swimlane.py serve            # keep it current: poll, re-render, push, publish
py ado-swimlane.py install --apply  # start the service at logon, with a watchdog
py ado-swimlane.py open             # print/open the running URL
py ado-swimlane.py publish          # republish the Artifact if the page would differ
py ado-swimlane.py status           # where everything is, and what is holding
```

## The one idea worth keeping

**The board is a template with a few machine-managed regions.** Everything else
is prose a person or a Claude session wrote, and a refresh never touches it.

| Written by the machine | Written by a person |
| --- | --- |
| `data-state` on every card | key-fact panels, card notes, the delta panel |
| the `CHILDREN` map (open children only) | lane and phase names, card placement |
| `SNAPSHOT` / `DELTA_FROM` / `REFRESHED` | the green "closed 09-10" styling and pills |
| the open epics/features count | every judgement about what any of it means |

That split is why this can run unattended. Live state is mechanical; analysis is
not. The refresh reports disagreements with curated completion styling. In the
rendered UI, the refreshed state controls every card's badge and color: reopened
cards lose completion styling and hide old completion pills; Removed cards use
neutral styling. Dated notes remain curated history and do not establish current
delivery. An empty child list proves only that no direct children are open.

It caught exactly that on its first run: five cards (`1964`, `2095`, `2099`,
`2633`, `2710`) had been rendering as `New` since 2026-09-10, when a backlog
audit moved them to `Active`. The old by-hand check only compared *done styling*
against *Closed*, so New-versus-Active drift was invisible to it for four days.

## How "always current" works

Each phase header (P0–P6) has a keyboard-accessible **Hide/Show** button.
Hiding a phase collapses its cells into a narrow header with a **Show** control;
the remaining lanes stay aligned. Several or all phases can be hidden.
Choices are stored in this browser's local storage and survive manual refresh,
live SSE updates, and static-page reloads. If storage is unavailable, the controls
still work for the current page. Work breakdown counts always include all phases.
Dependency arrows omit hidden endpoints and redraw when the layout changes.

This standalone application lives in `ado-swimlane/` within the parent checkout.
Run its commands and tests from this directory:
`py -m pytest`. Run the parent application's suite separately from the checkout
root (`py -m pytest tests`) to keep the two test configurations isolated.

Two tiers, because they are genuinely different things:

**The loopback page is live.** `serve` runs a supervised process that polls ADO
every five minutes, re-renders, and pushes the new revision over server-sent
events to any open page, which reloads itself carrying scroll position and open
panels across. A chip in the corner shows the revision and goes amber, then red,
if the service stops answering. No Claude session is involved; this is the tier
that is actually always current.

**The published Artifact is republished on a gate.** An Artifact cannot poll your
Azure DevOps organization — it is a static page hosted elsewhere, it has no route
to your network, and it must never carry a credential — so only a Claude session
can update it, and the service spawns one (`claude -p`, haiku, `--allowed-tools
Artifact`) when it is worth doing. "Worth doing" is decided exactly, not
heuristically: the rendered page is fingerprinted with the refresh timestamps
normalised out, and a publish happens only when that fingerprint moves. A card
changed state, a child appeared, a count moved, or you edited the prose — all
publish. Nothing happened — no session is spent.

Two limits sit on top, because a bulk edit should not become a bulk bill: a
ten-minute debounce coalesces a burst, and a daily cap (24) bounds the worst
case. AB#3526 touched 302 work items in eighteen minutes; that is one publish,
not 302.

The page does not publish itself; **the prose does.** Edit `board/board.html`,
and the next poll fingerprints the change and ships it. That is the intended
workflow: you write the words, the service handles the data and the publishing.

### The one forced publish, and why

A headless `claude -p` session has a **reduced `Artifact` tool: no `read`
action.** Republishing to an existing URL normally requires the conversation to
have read the live version first, and a headless session structurally cannot —
so its first publish is refused.

The way through is that the requirement is per *conversation*, not per publish.
The publisher pins a session id, forces exactly one publish to establish that
session's baseline, and every publish afterwards resumes the same session and
forces nothing. `publisher.json` records `bootstrapped` so this happens once.

What that buys, and what it costs:

- an ordinary republish **refuses rather than overwrites** when the live version
  is newer, so a version published from a chat session is never silently
  discarded;
- the price is that such an out-of-band publish **jams** the service: it will
  keep refusing, log why, and show it in `status`. Clear it deliberately with
  `publish --force`, which starts a fresh baseline.

That jam is the safe failure, and it is avoidable entirely by keeping prose in
`board/board.html` rather than editing the published page.

`--force` is never passed on the service's own judgement — only to bootstrap a
new session, or when you ask for it.

### Surviving reboots

The service survives reboots the way quota-burndown does, with the same three
fallbacks, because the first one needs rights you may not have:

1. a scheduled task at logon plus a one-minute repeating trigger — starter and
   watchdog in one, since `MultipleInstances IgnoreNew` makes the repeat a no-op
   while the service is alive;
2. if task registration is denied, a separate watchdog task that checks
   `/v1/status` and relaunches on failure;
3. if that is denied too, a per-user Startup shortcut running `serve
   --supervise`, which supervises itself.

## Authentication

There is no PAT. `az account get-access-token --resource <the Azure DevOps
application id>` mints a short-lived credential from the Azure CLI's existing
sign-in, refreshed a minute before expiry. Two consequences worth knowing:

- when the CLI session lapses, the collector reports `state: error` with the
  `az` message and keeps retrying on a one-minute backoff — it does not die, and
  it does not prompt; you run `az login` and the next poll recovers;
- nothing secret can reach the page, because the page is built only from work
  item fields.

The server binds to loopback, rejects any request whose `Host` or `Origin` is
not the loopback endpoint, and exposes no write path.

## What runs on every refresh

The failure mode of this dashboard is not a crash. It is a page that renders
perfectly while saying something untrue, so each render is verified before it is
served or published:

- every `CHILDREN` key resolves to a card in the DOM, and every card sits in
  exactly one lane/phase cell;
- an **independent walk** of the board — string-based, not a second call into
  the rendering code — re-derives the totals the page's own script will compute,
  and they must agree with the live `openChildrenOfCards` count from WIQL;
- `CHILDREN` never lists closed work (the map is open children only, which is
  what makes an empty card mean "nothing open" rather than "not checked");
- no card state disagrees with the snapshot it was rendered from;
- every inline script parses (`node --check`, when node is on PATH).

A failed check marks the service `degraded` and says why, in `/v1/status` and in
`refresh`'s output.

## Reading what changed

`refresh` prints a change report and appends it to `changes.jsonl`; `changes`
replays it. The report is deliberately observational — "F2097 went Active" is a
change, "F2097 jumped its gate" is a conclusion, and conclusions belong in the
prose a person writes.

Membership changes are computed only over sets with fixed meaning (`openEF`,
`openBugs`, open children, cards). Each poll fetches the union of several
queries and the "changed since" window shrinks as polling gets more frequent, so
an item can leave the fetch union without anything happening to it — reading
that as "vanished" invented ten changes out of forty-six seconds of quiet during
development. `tests/test_diff.py` pins that behaviour.

## Endpoints

| Path | What it gives you |
| --- | --- |
| `/` | the live page |
| `/v1/status` | health, revision, card and open-work counts, why it is degraded |
| `/v1/events` | server-sent revisions, with a heartbeat every 15s |
| `/v1/changes` | the most recent change report |
| `/v1/snapshot` | the raw pull |

## Layout

```
ado-swimlane.py        launcher (run from the checkout, no install needed)
board/board.html       the curated board - prose lives here, and only here
ado_swimlane/
  ado.py               auth, WIQL, batched work-item detail
  template.py          the managed regions, and what refuses to be managed
  render.py            live / static / artifact flavours of the same body
  diff.py              what moved between two snapshots
  validate.py          the checks above, including the independent walk
  refresh.py           one refresh: pull, inject, verify, write
  publish.py           the fingerprint gate, the debounce, and the pinned session
  service.py           polling, the loopback server, the push channel
  install.py           the three-tier autostart
  cli.py               the commands
```

State lives in `~/.ado-swimlane` (override with `ADO_SWIMLANE_HOME`): the current
and previous snapshot, `changes.jsonl`, both rendered pages, the service
endpoint record, and a bounded log. The repository holds no state and no
credentials.

Configuration is by environment: `ADO_SWIMLANE_ORG`, `ADO_SWIMLANE_PROJECT`,
`ADO_SWIMLANE_TEMPLATE`, `ADO_SWIMLANE_HOME`, `ADO_SWIMLANE_ARTIFACT`. Run
`serve --no-publish` to keep the loopback page live without touching the
Artifact at all.

## Tests

```
py -3 -m pytest -q
```

They run entirely offline against a small synthetic board, and cover the parts
where a silent wrong answer is possible: injection idempotence, prose being left
alone, the escaping that keeps a work-item title from ending the inline script,
the diff's fetch-window regression, the publish gate (unchanged, debounced,
capped, forced), the bootstrap-once-then-never-force sequence, and each way a
check is supposed to fail.

## Prior art

The refresh design is lifted from
[quota-burndown](https://github.com/koala-man-64/quota-burndown): supervised
loopback service, collector thread on a fixed interval, page cached in memory,
SSE for push, meta-refresh for the static fallback, and the same Task Scheduler
fallback ladder. What changed is the collector and the render, and the addition
of a curated template the machine is not allowed to rewrite.
