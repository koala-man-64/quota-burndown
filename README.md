# quota-burndown

One local page for three coding agents. A burndown of your Claude and Codex subscription quotas: how much of each rate-limit window you have used versus a straight-line pace, how long is left, and when you would hit 100% at the current rate. Underneath it, a per-request token usage ledger for Claude Code, Codex and Antigravity: prompts, requests, model, reasoning effort, input / cache read / cache write / output tokens, with timestamps. Works as a Claude Code plugin, a Claude Code status line, a Codex skill, and a self-refreshing local HTML page. Standard library only, Python 3.10+. Nothing leaves the machine.

## How it works

| Provider | Source | Trigger |
| --- | --- | --- |
| Claude | The desktop app's own usage history, `plan-usage-history.json`, which the app appends to every 5 to 15 minutes while it runs (5h and 7-day percentages). No credential, no network call. The app is an MSIX package, so the file it writes to `%APPDATA%\Claude` really lives under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`; the collector checks both and reads the newer copy. The file has no reset times, so the 5h reset is inferred from the first non-zero reading after a zero plus five hours, and the 7-day reset from the last endpoint reading or the last drop to zero plus seven days. | Scheduled task every 5 min, and every page load in `serve` mode. |
| Claude | The OAuth usage endpoint Claude Code uses for `/usage` (adds the 7-day per-model window and exact reset times). Authorized with the CLI's OAuth session in `~/.claude/.credentials.json` (see "When Claude limit samples stop"); the value is read only to make the call and is never logged or stored. | Same schedule; only succeeds while the CLI session is fresh. |
| Codex | The `rate_limits` block Codex writes on every `token_count` event in its rollout files under `~/.codex/archived_sessions` and `~/.codex/sessions`. Scanned incrementally by byte offset. | Scheduled task and page loads. Full history import with `backfill`. |

Samples land in `~/.quota-burndown/samples.jsonl` (one JSON line each). `latest.json` holds the newest reading per window so the status line stays fast. The page is `~/.quota-burndown/burndown.html`.

One process generates everything: the Windows scheduled task `QuotaBurndownCollect` runs `collect --render --quiet` every 5 minutes, which samples both quotas, ingests new usage, and rewrites the page (the page reloads itself every 2 minutes). `install --task --apply` registers it to run on battery as well as mains, to catch up one missed run after the laptop wakes, and to give up after 10 minutes; `where` reports missed runs, so a stale page has a visible cause. It runs only while you are logged in. Nothing inside Claude Code runs on a schedule: the plugin ships only the on-demand `/quota-burndown:burndown` skill, and the status line just displays `latest.json`.

Pace is linear: 0% at window start, 100% at reset. Window start is `resets_at` minus the window length. "Over pace" means you are spending faster than that line; the projection extends your average rate since window start to show either the time you would hit 100% or your projected use at reset.

## Token usage ledger

`collect` also parses each tool's own local files into `~/.quota-burndown/usage.sqlite`, one row per model call (`request`) and one per typed turn (`prompt`). Prompt counts in the tables are human prompts on the main thread; instructions handed to subagents are stored too but reported separately as `agent_prompts` in the JSON. Files are re-read only when their size or mtime changes, and rows are keyed by the source's own identifiers, so re-scans are idempotent. The ledger outlives the sources: Claude Code deletes transcripts after `cleanupPeriodDays` (30 by default), the ledger does not.

| Provider | Source | What is exact | What is not |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` transcripts (main sessions, subagents, workflow agents). One API response spans several lines with the same `message.id`; they are collapsed to one request. | model, effort, input, cache read, cache write, output, thinking, prompts | — |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` (and `archived_sessions`). Each `token_count` event is a cumulative snapshot; the ledger stores the per-call delta. | model, effort, input, cached input, cache write, output, reasoning, prompts | Prompts injected by Codex Desktop as role=user text (`<hook_prompt>`, `<recommended_plugins>`, …) are excluded by prefix. |
| Antigravity | `~/.gemini/antigravity/brain/<conversation>/.system_generated/logs/transcript.jsonl` for prompts and timestamps; `conversations/<conversation>.db` (`gen_metadata` protobuf blobs) for one row per model call. | prompts, model, effort, timestamps, request count | **Token counts.** Antigravity writes none. An unlabeled protobuf field grows with every call beside a constant 256000, which is how a prompt token count and a context window behave, so it is stored as `input_tokens_inferred` and shown as `~input`. It is never added into any total. Output tokens are unknown. Google exposes no local limit signal. |

```
py quota-burndown.py usage                                  # last 7 days by provider, model x effort, day
py quota-burndown.py usage --since 2026-09-01 --until 2026-09-03 --by model_effort,session --raw
py quota-burndown.py usage --days 30 --csv usage.csv --json usage.json
py quota-burndown.py backfill --usage                       # import all history (reads the Codex archive, several GB)
py quota-burndown.py backfill --usage --provider claude --rescan   # re-parse everything after a parser change
py quota-burndown.py collect --no-usage                     # quota samples only
```

`--since/--until` are UTC calendar dates so they line up with the standalone audit scripts; the page's "today" is the local calendar day, its 7- and 30-day figures are rolling windows. `collect` from the scheduled task covers all three tools with a 60 s budget and defers anything it did not reach to the next run; `collect --provider claude` parses only changed Claude transcripts within 10 s. `serve` also answers `/usage.json`.

## Install

```
py quota-burndown.py install          # dry run: shows exactly what would change
py quota-burndown.py install --apply  # status line in ~/.claude/settings.json (backup taken), 5-minute scheduled task, Codex skill
```

Then, in Claude Code (desktop app or CLI):

```
/plugin marketplace add C:\Users\rdpro\Projects\quota-burndown
/plugin install quota-burndown@rudy-local
```

The plugin adds `/quota-burndown:burndown`, which runs only when you invoke it. For a one-off session use `claude --plugin-dir C:\Users\rdpro\Projects\quota-burndown`.

Optional one-time history import of every Codex rollout ever written (reads several GB, takes a few minutes):

```
py quota-burndown.py backfill
```

## Use

```
py quota-burndown.py status              # text summary per window, plus today's usage per tool
py quota-burndown.py usage               # token usage tables from the ledger
py quota-burndown.py collect --render    # sample quotas, ingest new usage, rewrite the page
py quota-burndown.py serve               # http://127.0.0.1:8787/ re-collects on load
py quota-burndown.py where               # data paths and scheduled task state
py quota-burndown.py prune --keep-days 90
py quota-burndown.py uninstall --apply
```

In Claude Code: `/quota-burndown:burndown`. In Codex: ask for the quota burndown; the installed skill runs the same CLI.

Status line format: `Claude 5h 44% p38 ▲6 · 7d 27% p45 ▼18 │ Codex 7d 99% p52 ▲47`. `p` is pace, the arrow is points over (▲, red) or under (▼, green) pace. The status line reads `latest.json` and writes nothing; its numbers are as fresh as the last scheduled run.

## Development

```
py -m pytest
```

## When Claude limit samples stop

The 5h and 7-day Claude cards stay fresh as long as the desktop app is running, because they read its usage history. The per-model 7-day card and exact reset times come from the usage endpoint, which accepts only a session that carries the profile scope. The Claude Code CLI keeps one in `~/.claude/.credentials.json` and refreshes it whenever it runs; it expires a few hours after the CLI last ran, and the desktop app keeps its own session encrypted elsewhere. So when the CLI has been idle, the collector logs `CLI OAuth session expired` (or `no OAuth session`) and only the per-model card goes stale. Codex limits and all usage numbers are unaffected.

The long-lived value from `claude setup-token` does **not** help: it is issued with the inference scope only, and the endpoint answers HTTP 403 (verified 2026-09-03). The `QUOTA_BURNDOWN_CLAUDE_OAUTH` variable and the `~/.quota-burndown/claude_oauth` file are still honoured ahead of the CLI credentials, but only for a session value that has the profile scope; a value that gets rejected blocks the CLI path, so delete it. Whatever the source, the value is only ever sent as a bearer header to the usage endpoint and is never logged.

## Limitations

- The Claude usage endpoint is the one Claude Code itself calls, but it is not publicly documented and may change. The `limits` list is parsed first with a fallback to the older `five_hour` / `seven_day` fields.
- Claude limit samples only flow while the CLI's OAuth session is fresh (above); Codex sampling and the usage ledger are unaffected either way.
- Which Codex windows appear depends on the plan; only windows Codex reports are shown.
- The Claude Code desktop app has no status line. There, use `/quota-burndown:burndown` to open the page in the Browser pane, or open `burndown.html` directly.
