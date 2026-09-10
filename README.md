# quota-burndown

A local capacity service and live dashboard for Codex and Claude, with a token activity ledger for Antigravity. Python 3.10+, standard library only. The service reports observations and guidance; an orchestrator owns scheduling.

## Start and install

```powershell
py -B quota-burndown.py serve                         # http://127.0.0.1:8787/
py -B quota-burndown.py capacity --json               # cached snapshot
py -B quota-burndown.py capacity --watch              # reconnecting JSON-line stream
py -B quota-burndown.py install --service             # inspect installation
py -B quota-burndown.py install --service --apply      # install and start at logon
py -B quota-burndown.py install --statusline --apply   # Claude quota handoff
```

On Windows, `QuotaBurndownService` starts at logon, runs on battery, and restarts after failure. Installation disables the old `QuotaBurndownCollect` five-minute task. The OS writer lease also makes overlapping periodic `collect` delegate to the service. `install --all --apply` installs the service, status line and Codex skill; `--task` retains the legacy periodic option. Other platforms can supervise `serve` with their user service manager.

If Task Scheduler denies registration, installation uses a per-user Startup shortcut and starts the same hidden process immediately. This mode starts at sign-in but has no OS crash-restart policy; provider reconnects still work. The old periodic task remains harmless under the writer lease. Uninstall removes the shortcut; stop its running process separately or sign out.

`--home PATH` before the subcommand selects the data directory; default is `~/.quota-burndown` or `QUOTA_BURNDOWN_HOME`. `capacity-service.json` records the URL. CLI `--url` overrides it. `uninstall --apply` removes autostart and the legacy task, stops a scheduled service, and removes only this tool's status-line setting. A running Startup process remains until stopped or sign-out; data stays on disk.

## Collection and freshness

| Provider | Source | Guarantees and limits |
| --- | --- | --- |
| Codex | Persistent signed-in `codex app-server`: `account/read`, `account/rateLimits/read`, `account/rateLimits/updated`; bounded tails of today's/yesterday's UTC rollouts add model memberships. | One quota request in flight; reads every 15 seconds with consumers, 60 seconds idle, with deadlines and reconnect backoff. No model tasks or inference calls. |
| Claude | Supported status-line `rate_limits.five_hour` and `seven_day`; bounded quota-only handoff. Desktop history is the fallback. | First-seen changes are timestamped once. Identical quota/redraws, context changes and restarts do not renew freshness. Desktop timestamps are preserved, inferred resets labeled. CLI 2.1.251 is the tested installation target; `claude install 2.1.251` works when the stable update channel is older. |
| Antigravity | Local ledger activity. | Quota allowance, remaining capacity and resets stay unknown. Inferred token activity stays separate from exact accounting. |

Direct/event observations have a **60-second** freshness deadline; desktop history has **20 minutes**. Windows expose TTL, observation time, receipt time, reset provenance and observation-time provenance. `source_age_s` is age at snapshot generation; use `observed_at` and `valid_until` to age it locally. Native read completion is labeled separately from unknown upstream reporting delay. Expired windows become unknown until another reading arrives.

Claude's documented status-line payload does not identify the subscription account or quota observation time. Its local scope is explicitly unverified; do not merge it with other machines/accounts. Identical cached quota may become stale while the allowance is unchanged: a redraw cannot prove another provider observation.

Sources: [Codex app-server](https://learn.chatgpt.com/docs/app-server), [Claude status-line contract](https://code.claude.com/docs/en/statusline).

## Orchestrator contract

| Interface | Response |
| --- | --- |
| `GET /v1/capacity` | Immediate cached snapshot; no network or ledger work on this path. |
| `GET /v1/capacity/events` | SSE: immediate full snapshot on every connection, then changed snapshots; comments keep idle connections alive. |
| `capacity --json` | Same snapshot, with an explicitly disconnected disk fallback. |
| `capacity --watch` | Full snapshots as JSON lines, reconnecting with bounded backoff. |
| `POST /v1/policy` | Same-origin setting: JSON `{"reserve_pct": 5}`, `10`, or `20`. Accepted writes publish through the single writer; a full queue returns 503 for retry. |

Schema version 1 includes `instance_id`, monotonic per-instance `revision`, `generated_at`, `collector_health`, `provider_states`, `policy`, `pools`, and `unreported_in_flight_usage=unknown`. Replace state on reconnect; a new instance starts a new revision sequence. Disconnected disk reads retain source freshness independently of connectivity and clear forecasts.

The additive `provider_groups` view presents the requested limit slots under Codex, Antigravity / Gemini, and Claude. Codex shows general weekly and Spark five-hour/weekly; Claude shows current-session five-hour, all-models weekly, and Fable weekly; Antigravity shows Gemini five-hour/weekly. Each observed slot references a real `pool_id` and window. Unsupported slots have null readings/timestamps and `display_only=true`; they create no independent allowance in `pools`. Additional provider-reported windows remain visible. Screenshots define labels and grouping, never current percentages. Configured Claude sources do not expose Fable weekly; configured Antigravity sources do not expose Gemini quota.

A pool is keyed by provider, account scope and limit ID. Model names are memberships, never independent allowances. `account_scope_confidence`, `limit_id_provenance` and `mapping_confidence` distinguish reported identifiers from adapter grouping and missing mappings. Every window constrains the pool: exhausted weekly allowance blocks unused five-hour allowance. Complete native readings reconcile omitted windows against persisted history; omitted windows stay unknown. Account switches retire the previous active account view.

Windows contain used/remaining percentages, `usable_pct = max(0, 100 - reserve_pct - used_pct)`, reset, separate `allowance_state` and `freshness`, whole-window/recent burn in percentage points/hour, sustainable rate, and runway to reserve. Missing percentages/resets produce null budgets. Model mapping can stay unknown until a model is observed in an active rollout.

Forecasts require three recent distinct readings spanning at least 15 minutes. Recent rate uses up to the last hour; conservative runway uses the higher supported whole-window/recent rate and charges the unobserved interval at that rate. Reset changes or downward corrections restart estimation. Stale sources have no forecast.

For routing, check connectivity, collector health, account/mapping confidence, source freshness and **all** applicable windows. Capacity is an observation, not a reservation. No guaranteed tokens, job counts or concurrency slots are inferred; other work may consume allowance before provider reporting catches up.

## Dashboard and persistence

The dashboard uses the same snapshot and SSE stream. Its capacity matrix comes first, followed by the weekly historic charts in a three-column grid with one chart per cell: Claude all models, Claude Fable, Codex, Codex Spark, and Antigravity Gemini, so five charts fill two rows of three. The Fable cell is a fixed slot beside the all-models chart; it renders as a labelled empty card until a Claude source reports a `7d:fable` reading. Model/effort and session efficiency drilldowns follow. Five-hour allowances remain visible in the live capacity matrix but do not render historical charts. Runway includes whole-window and last-hour burn rates in percentage points per hour. Live updates retain scroll, focus and expanded details. Reserve presets default to 10% and share `capacity-policy.json` with API consumers. Advice links to local session evidence; it never sends notifications or changes models.

Each weekly historical chart has a fixed two-cycle, 14-day x-axis. Active charts end at the next reset; idle, expired, or unknown-reset charts end at the current time. The range control is removed, so older saved preferences cannot override this domain. Clipping affects presentation only; stored history remains intact.

Daily stacked bars below the historic charts show recorded token totals by provider and model for today and the preceding six local calendar days. Effort levels are combined. Unknown totals are excluded with request coverage counts; missing model names appear as Unknown model. These tokens are local usage evidence, not subscription quota shares. `/v1/usage` adds `daily_models` (local dates, provider/model series, totals and coverage) and `daily_models_html` (the same escaped chart/table fragment used by the static fallback). Live polling preserves expanded tables, focus, and scroll.

Legacy `/latest.json`, `/status.json`, `/usage.json`, CLI `status`, and raw usage exports remain available. Historic model-grouped charts are labeled as history, not independent live pools. `render` produces a dated HTML fallback using persisted `capacity.json`.

`capacity-state.json` retains bounded observation histories and reset/correction baselines; `capacity.json` is the atomic published snapshot. An OS-held lease prevents simultaneous quota writers and releases on process death. Queues and file reads are bounded. History ingestion has its own worker and lease, so `backfill --usage` can run while quota collection and cached reads continue. The service imports changed history with a 15-second between-file budget; a single large file may take longer. Cached efficiency at `/v1/usage` is separate from raw `/usage.json`.

The server binds only to loopback, checks Host/Origin and provides no cross-origin API access. Capacity endpoints contain metadata without credentials or transcripts. The app-server uses the installed client's existing sign-in and can make provider network requests; quota-burndown does not read or persist those credentials.

## Token usage ledger

Request and prompt accounting lives in `usage.sqlite`. Re-scans are idempotent and raw provider accounting is preserved. Claude messages with the same API message ID collapse into one request; Codex cumulative token counts become request deltas. Antigravity's unlabeled context counter remains inferred and outside exact totals. Dashboard uncached input subtracts cache-inclusive Codex input once; reasoning is a share of output.

```powershell
py -B quota-burndown.py usage --days 7
py -B quota-burndown.py usage --since 2026-09-01 --until 2026-09-03 --by model_effort,session --raw
py -B quota-burndown.py usage --days 30 --csv usage.csv --json usage.json
py -B quota-burndown.py backfill --usage
py -B quota-burndown.py backfill --usage --provider claude --rescan
```

Date filters are inclusive UTC calendar dates; dashboard "today" uses the local date. The ledger survives source retention/deletion. Quota backfill/prune require the service stopped to acquire its writer lease. Usage backfill uses the independent ledger lease.

## Development and acceptance

```powershell
py -B -m pytest -q
py -B -m pytest tests/test_service.py -q -s
```

Regression coverage includes shared pools, weekly exhaustion, missing/stale windows, resets/corrections, duplicates/out-of-order events, persisted recovery, SSE reconnection, same-origin controls, bounded handoff, collector overlap, omitted five-hour history charts, and exact weekly chart domains. The benchmark runs 100 HTTP reads while the history worker is busy, targeting p95 below 100 ms. Adapter enqueue-to-publication latency is separate from unknown upstream delay; its target is two seconds. Browser acceptance exercises reserve updates, focus preservation, chart domains and drilldowns.
